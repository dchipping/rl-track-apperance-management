# ------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# Modified by: Daniel Chipping
# ------------------------------------------------------------------------------

import numpy as np
import torch
import torch.nn.functional as F
from .models import *
from .models.decode import mot_decode
from .models.utils import _tranpose_and_gather_feat
from .tracker import matching
from .tracking_utils.log import logger
from .tracking_utils.utils import *

import random
from scipy import spatial

from .tracker.basetrack import TrackState
from .tracker.multitracker import STrack, JDETracker, joint_stracks, sub_stracks, remove_duplicate_stracks


class ModifiedSTrack(STrack):
    def __init__(self, tlwh, score, temp_feat, buffer_size=30, freeze_gallery=False):
        super().__init__(tlwh, score, temp_feat, buffer_size)
        self.agent = lambda x: random.randint(0,1) #### REPLACE WITH AGENT ####
        self.freeze_gallery = freeze_gallery
        self.obs = None
        
        temp_feat /= np.linalg.norm(temp_feat)
        self.features.append(temp_feat)
 
    def min_gallery_similarity(self, feat):
        feature_idx = None
        min_cosine_similarity = 1
        for idx, gallery_feat in enumerate(self.features):
            similarity = 1. - spatial.distance.cosine(feat, gallery_feat)
            if similarity < min_cosine_similarity:
                min_cosine_similarity = similarity
                feature_idx = idx
        return min_cosine_similarity, feature_idx

    def get_observation(self, new_track):
        self.curr_feat /= np.linalg.norm(self.curr_feat)
        similarity, _ = self.min_gallery_similarity(new_track.curr_feat)
        return np.array([new_track.score, similarity], dtype=float)

    def update_gallery(self, action, feat):
        '''Translate action to change in gallery'''
        if action == 1:
            self.features.append(feat)
        else:
            pass
        # self.smooth_feat = HOW??? Pick Matrix of Min from gallery, see line 111 matching.py
        self.smooth_feat = np.average(self.features, axis=0) ## TEMPORARY FIX #### NEED TO REMOVE ARGH
        self.smooth_feat /= np.linalg.norm(self.smooth_feat)
        1 == 1

    def agent_update_features(self, feat, obs):
        '''New method added for RL agent to manage gallery'''
        if not self.freeze_gallery:
            action = self.agent.compute_single_action(obs)
            self.update_gallery(action, feat)
    
    def re_activate(self, new_track, frame_id, new_id=False):
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_track.tlwh)
        )

        self.obs = self.get_observation(new_track)
        self.agent_update_features(new_track.curr_feat, self.obs)
        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id()

    def update(self, new_track, frame_id):
        """
        Update a matched track
        :type new_track: STrack
        :type frame_id: int
        :type update_feature: bool
        :return:
        """
        self.frame_id = frame_id
        self.tracklet_len += 1

        new_tlwh = new_track.tlwh
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance, self.tlwh_to_xyah(new_tlwh))
        self.state = TrackState.Tracked
        self.is_activated = True

        self.score = new_track.score
        self.obs = self.get_observation(new_track)
        self.agent_update_features(new_track.curr_feat, self.obs)


class ModifiedJDETracker(JDETracker):
    def __init__(self, opt, frame_rate=30, train_mode=False):
        super().__init__(opt, frame_rate)
        self.train_mode = train_mode

    def update(self, im_blob, img0):
        self.frame_id += 1

        width = img0.shape[1]
        height = img0.shape[0]
        inp_height = im_blob.shape[2]
        inp_width = im_blob.shape[3]
        c = np.array([width / 2., height / 2.], dtype=np.float32)
        s = max(float(inp_width) / float(inp_height) * height, width) * 1.0
        meta = {'c': c, 's': s,
                'out_height': inp_height // self.opt.down_ratio,
                'out_width': inp_width // self.opt.down_ratio}

        ''' Step 1: Network forward, get detections & embeddings'''
        with torch.no_grad():
            output = self.model(im_blob)[-1]
            hm = output['hm'].sigmoid_()
            wh = output['wh'] 
            id_feature = output['id']
            id_feature = F.normalize(id_feature, dim=1)

            reg = output['reg'] if self.opt.reg_offset else None
            dets, inds = mot_decode(hm, wh, reg=reg, cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)
            id_feature = _tranpose_and_gather_feat(id_feature, inds)
            id_feature = id_feature.squeeze(0)
            id_feature = id_feature.cpu().numpy()

        dets = self.post_process(dets, meta)
        dets = self.merge_outputs([dets])[1]

        remain_inds = dets[:, 4] > self.opt.conf_thres
        dets = dets[remain_inds]
        id_feature = id_feature[remain_inds]

        # vis
        # for i in range(0, dets.shape[0]):
        #     bbox = dets[i][0:4]
        #     bbox = np.array(bbox, dtype=np.int)
        #     cv2.rectangle(img0, (bbox[0], bbox[1]), 
        #                     (bbox[2], bbox[3]),
        #                     (0, 255, 0), 2)
        # cv2.imshow('dets', img0)
        # cv2.waitKey(0)
        # id0 = id0-1

        if len(dets) > 0:
            '''Detections'''
            detections = [ModifiedSTrack(ModifiedSTrack.tlbr_to_tlwh(tlbrs[:4]), tlbrs[4], f, 30, 
            self.train_mode) for (tlbrs, f) in zip(dets[:, :5], id_feature)]
        else:
            detections = []
            
        activated_starcks = []
        refind_stracks = []
        lost_stracks = []
        removed_stracks = []

        ''' Add newly detected tracklets to tracked_stracks'''
        unconfirmed = []
        tracked_stracks = []  # type: list[STrack]
        for track in self.tracked_stracks:
            if not track.is_activated:
                unconfirmed.append(track)
            else:
                tracked_stracks.append(track)

        ''' Step 2: First association, with embedding'''
        strack_pool = joint_stracks(tracked_stracks, self.lost_stracks)
        # Predict the current location with KF
        #for strack in strack_pool:
            #strack.predict()
        STrack.multi_predict(strack_pool)
        dists = matching.embedding_distance(strack_pool, detections)
        #dists = matching.gate_cost_matrix(self.kalman_filter, dists, strack_pool, detections)
        dists = matching.fuse_motion(self.kalman_filter, dists, strack_pool, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.7)

        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(detections[idet], self.frame_id)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        ''' Step 3: Second association, with IOU'''
        detections = [detections[i] for i in u_detection]
        r_tracked_stracks = [strack_pool[i] for i in u_track if strack_pool[i].state == TrackState.Tracked]
        dists = matching.iou_distance(r_tracked_stracks, detections)
        matches, u_track, u_detection = matching.linear_assignment(dists, thresh=0.5)

        for itracked, idet in matches:
            track = r_tracked_stracks[itracked]
            det = detections[idet]
            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id, self.opt.update_feature)
                activated_starcks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

        for it in u_track:
            track = r_tracked_stracks[it]
            if not track.state == TrackState.Lost:
                track.mark_lost()
                lost_stracks.append(track)

        '''Deal with unconfirmed tracks, usually tracks with only one beginning frame'''
        detections = [detections[i] for i in u_detection]
        dists = matching.iou_distance(unconfirmed, detections)
        matches, u_unconfirmed, u_detection = matching.linear_assignment(dists, thresh=0.7)
        for itracked, idet in matches:
            unconfirmed[itracked].update(detections[idet], self.frame_id)
            activated_starcks.append(unconfirmed[itracked])
        for it in u_unconfirmed:
            track = unconfirmed[it]
            track.mark_removed()
            removed_stracks.append(track)

        """ Step 4: Init new stracks"""
        for inew in u_detection:
            track = detections[inew]
            if track.score < self.det_thresh:
                continue
            track.activate(self.kalman_filter, self.frame_id)
            activated_starcks.append(track)
        """ Step 5: Update state"""
        for track in self.lost_stracks:
            if self.frame_id - track.end_frame > self.max_time_lost:
                track.mark_removed()
                removed_stracks.append(track)

        # print('Ramained match {} s'.format(t4-t3))

        self.tracked_stracks = [t for t in self.tracked_stracks if t.state == TrackState.Tracked]
        self.tracked_stracks = joint_stracks(self.tracked_stracks, activated_starcks)
        self.tracked_stracks = joint_stracks(self.tracked_stracks, refind_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.tracked_stracks)
        self.lost_stracks.extend(lost_stracks)
        self.lost_stracks = sub_stracks(self.lost_stracks, self.removed_stracks)
        self.removed_stracks.extend(removed_stracks)
        self.tracked_stracks, self.lost_stracks = remove_duplicate_stracks(self.tracked_stracks, self.lost_stracks)
        # get scores of lost tracks
        output_stracks = [track for track in self.tracked_stracks if track.is_activated]

        logger.debug('===========Frame {}=========='.format(self.frame_id))
        logger.debug('Activated: {}'.format([track.track_id for track in activated_starcks]))
        logger.debug('Refind: {}'.format([track.track_id for track in refind_stracks]))
        logger.debug('Lost: {}'.format([track.track_id for track in lost_stracks]))
        logger.debug('Removed: {}'.format([track.track_id for track in removed_stracks]))

        return output_stracks