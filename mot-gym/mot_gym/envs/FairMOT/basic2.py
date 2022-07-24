import copy
import datetime as dt
import os
import os.path as osp
import time
from pathlib import Path

import cv2
import datasets.dataset.jde as datasets
import gym
import motmetrics as mm
import numpy as np
import torch
import FairMOT.src._init_paths
from gym import spaces
from opts import opts
from tracker.basetrack import BaseTrack
from tracking_utils.evaluation import Evaluator
from tracking_utils.io import unzip_objs

from ..bbox_colors import _COLORS
from .modified2_FairMOT import ModifiedJDETracker as Tracker


class BasicMotEnv(gym.Env):
    def __init__(self):
        '''
        Action Space: {0, 1}
        0 - Ignore encoding
        1 - Add encoding to gallery
        '''
        self.action_space = spaces.Discrete(2)
        
        '''
        Observation Space: [1., 1., 1.]
        0->1. - Percentage Overlap IOU of other detections
        0->1. - Detection confidence
        0.->1. - Min cosine similarity
        0->100, - Gallery size
        '''
        self.observation_space = spaces.Box(np.array([0.,0.,0.]), np.array([1.,1.,100.]),
                                             shape=(3,), dtype=float)

        # Find gym path
        self.gym_path = self._get_gym_path()
        
        # Load seq data and gt
        # self._load_dataset('short-seq/last-100')
        # self._load_detections('FairMOT/short-seq/last-100')
        self._load_dataset('MOT17/train/MOT17-05')
        self._load_detections('FairMOT/MOT17/MOT17-05')
        # self._load_dataset('short-seq/50-frames')
        # self._load_detections('FairMOT/short-seq/50-frames')

        # Initialise FairMOT tracker
        # model_path = self._get_model_path('fairmot_dla34.pth')
        model_path = '/home/dchipping/project/dan-track/mot-gym/mot_gym/trackers/FairMOT/models/fairmot_dla34.pth'
        exp_name = 'BasicMotEnv-{}'.format(dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))
        self.opt = opts().init(['mot', f'--load_model={model_path}', f'--exp_name={exp_name}'])
        self.tracker = Tracker(self.opt, self.frame_rate)

        # Additional variables
        self.first_render = True

    def reset(self):
        self.ep_reward = 0
        self.track_idx = 0
        self.frame_id = 1
        self.results = []
        self.tracker.reset()

        self.online_targets = self._track_update(self.frame_id)
        # Only release once first track(s) confirmed
        while not self.online_targets:
            done = self._step_frame()
            if done: 
                raise Exception('Sequence too short')

        track = self.online_targets[self.track_idx]
        obs = self._get_obs(track)
        info = self._get_info(track)
        return obs#, info

    def step(self, action):
        '''
        See env-data-flow.png for data flow
        '''
        # Take action
        track = self.online_targets[self.track_idx]
        track.update_gallery(action, track.curr_feat)
        reward = -100 if len(track.features) > 30 else 0
        # if self.frame_id == 1:
        #     track.features.clear()

        # Look to future to evaluate if successful action
        reward = 0
        if self.frame_id < self.seq_len:
            mm_types = self._evaluate(track.track_id, 1)
            if 'SWITCH' in mm_types:
                reward += -100
            else:
                reward += 1
            # reward = self._generate_reward(mm_type)
            # print(mm_type, reward)
        self.ep_reward += reward

        # Move to next frame and generate detections
        done = False
        if self.track_idx + 1 < len(self.online_targets):
            self.track_idx += 1
        else:
            done = self._step_frame()
            self.track_idx = 0

        # Generate observation and info for new track
        track = self.online_targets[self.track_idx]
        obs = self._get_obs(track)
        info = self._get_info(track)

        return obs, reward, done, info

    def _evaluate(self, track_id, eval_step=1): # TODO: Curr limited to k -> k+1
        # Get next targets but freeze track states before so
        # tracker can be restored for all tracks in frame k
        eval_frame_id = self.frame_id + eval_step
        next_targets = self._track_eval(eval_frame_id) 

        # Compare gt and results for frame k and k+1
        results = {}
        self._add_results(results, self.frame_id, self.online_targets)
        self._add_results(results, self.frame_id + 1, next_targets)
        events = self._evalute(results)#.loc[1]
        # self.events = events
        events = events.loc[1]
        
        # Calculate reward
        track_event = events[events['HId'] == track_id]
        self.track_event = track_event
        # for event in track_event['Type'].values:
        #     if event in {'SWITCH', 'TRANSFER', 'ASCEND', 'MIGRATE'}:
        #         l = 102 % 23
        #         l +=1
        # mm_type = track_event['Type'].values[0] if track_event.size else 'LOST'
        return track_event['Type'].values

    def _step_frame(self):
        done = False
        if self.frame_id < self.seq_len:
            self.frame_id += 1
            self.online_targets = self._track_update(self.frame_id)
            self._save_results(self.frame_id, self.online_targets)
            return done
        else:
            done = True
            self._write_results(self.results, 'mot')
            self._get_summary()
            return done

    def _track_update(self, frame_id):
        dets = self.detections[str(self.frame_id)]
        feats = self.features[str(self.frame_id)]
        return self.tracker.update(dets, feats, frame_id)

    def _track_eval(self, eval_frame_id):
        frozen_count = BaseTrack._count
        forzen_tracks = self.tracker.tracked_stracks
        frozen_lost = self.tracker.lost_stracks
        frozen_removed = self.tracker.removed_stracks
        frozen_kf = self.tracker.kalman_filter

        self.tracker.tracked_stracks = copy.deepcopy(forzen_tracks)
        self.tracker.lost_stracks = copy.deepcopy(frozen_lost)
        self.tracker.removed_stracks = copy.deepcopy(frozen_removed)
        self.tracker.kalman_filter = copy.deepcopy(frozen_kf)

        frame_id = self.frame_id
        while frame_id < eval_frame_id:
            frame_id += 1
            dets = self.detections[str(self.frame_id)]
            feats = self.features[str(self.frame_id)]
            online_targets = self.tracker.update(dets, feats, frame_id)

        BaseTrack._count = frozen_count
        self.tracker.tracked_stracks = forzen_tracks
        self.tracker.lost_stracks = frozen_lost
        self.tracker.removed_stracks = frozen_removed
        self.tracker.kalman_filter = frozen_kf

        return online_targets

    def _add_results(self, results_dict, frame_id, online_targets):
        results_dict.setdefault(frame_id, [])
        for t in online_targets:
            tlwh = t.tlwh
            tid = t.track_id
            ts = t.score
            vertical = tlwh[2] / tlwh[3] > 1.6
            if tlwh[2] * tlwh[3] > self.opt.min_box_area and not vertical:
                track_result = (tuple(tlwh), tid, ts)
                results_dict[frame_id].append(track_result)
    
    def _save_results(self, frame_id, online_targets):
        # Filter to only save active tracks
        active_targets = [t for t in online_targets if t.is_activated]
        online_ids = []
        online_tlwhs = []
        for t in active_targets:
            tlwh = t.tlwh
            tid = t.track_id
            ts = t.score
            vertical = tlwh[2] / tlwh[3] > 1.6
            if tlwh[2] * tlwh[3] > self.opt.min_box_area and not vertical:
                online_tlwhs.append(tlwh)
                online_ids.append(tid)
        self.results.append((frame_id, online_tlwhs, online_ids))

    def _evalute(self, results): 
        self.evaluator.reset_accumulator()

        frames = sorted(list(set(self.evaluator.gt_frame_dict.keys()) & set(results.keys())))
        for frame_id in frames:
            trk_objs = results.get(frame_id, [])
            trk_tlwhs, trk_ids = unzip_objs(trk_objs)[:2]
            self.evaluator.eval_frame(frame_id, trk_tlwhs, trk_ids, rtn_events=False)

        events = self.evaluator.acc.mot_events
        return events

    def _generate_reward(self, mm_type):
        '''
        Each event type is one of the following
        - `'MATCH'` a match between a object and hypothesis was found
        - `'SWITCH'` a match but differs from previous assignment (hypothesisid != previous) (relative to Hypo)
        - `'MISS'` no match for an object was found
        - `'FP'` no match for an hypothesis was found (spurious detections)
        - `'RAW'` events corresponding to raw input
        - `'TRANSFER'` a match but differs from previous assignment (objectid != previous) (relative to Obj)
        - `'ASCEND'` a match but differs from previous assignment  (hypothesisid is new) (relative to Obj)
        - `'MIGRATE'` a match but differs from previous assignment  (objectid is new) (relative to Hypo)
        '''
        if mm_type == 'MATCH':
            return 1
        elif mm_type == 'SWITCH':
            return -1
        elif mm_type == 'TRANSFER':
            return -1
        elif mm_type == 'ASCEND':
            return -1
        elif mm_type == 'MIGRATE':
            return -1
        elif mm_type == 'FP':
            return -1
        elif mm_type == 'LOST':
            return 0
        else:
            raise Exception('Unkown track type')

    def render(self, mode="human"):
        img_dir = osp.join(self.data_dir, 'img1')
        img_files = os.listdir(img_dir)
        img_path = osp.join(img_dir, img_files[self.frame_id-1])
        img0 = cv2.imread(img_path)

        if self.first_render:
            black_img = np.zeros(img0.shape, dtype=img0.dtype)
            cv2.imshow('env snapshot', black_img)
            cv2.waitKey(1)
            time.sleep(1)
            self.first_render = False
          
        for i in range(len(self.online_targets)):
            track = self.online_targets[i]
            text = str(track.track_id)
            bbox = track.tlwh
            curr_track = (i == self.track_idx)
            self._visualize_box(img0, text, bbox, i, curr_track)

        curr_tid = self.online_targets[self.track_idx].track_id
        text = f'TrackID {curr_tid}, Frame {self.frame_id}, {self.frame_rate} fps'
        cv2.putText(img0, text, (4,16), cv2.FONT_HERSHEY_PLAIN, 1, (0,0,255), 1, cv2.LINE_AA)
        cv2.imshow('env snapshot', img0)
        cv2.waitKey(1)

    def close(self):
        cv2.destroyAllWindows()
        self.first_render = True

    def _get_obs(self, track):
        return track.obs

    def _get_info(self, track):
        tids = {t.track_id for t in self.online_targets}
        track_info = { "track_id": track.track_id, "gallery_size": len(track.features),
         "track_idx": self.track_idx }
        seq_info = { "seq_len": self.seq_len, "frame_rate": self.frame_rate }
        return { "curr_frame": self.frame_id, "ep_reward": self.ep_reward, 
        "tracks_ids": tids, "curr_track": track_info, "seq_info": seq_info }

    def _get_model_path(self, model_name):
        model_path = osp.join(self.gym_path, 'pretrained', model_name)
        return model_path

    def _load_dataset(self, seq_path='MOT17/train/MOT17-05'):
        '''
        MOT submission format:
        <frame>, <id>, <bb_left>, <bb_top>, <bb_width>, <bb_height>, <conf>, <x>, <y>, <z>
        '''
        self.data_dir = osp.join(self.gym_path, 'data', seq_path)
        print(f'Loading data from: {self.data_dir}')
        
        meta_info = open(osp.join(self.data_dir, 'seqinfo.ini')).read()
        self.frame_rate = int(meta_info[meta_info.find('frameRate') + 10:meta_info.find('\nseqLength')])
        self.seq_len = int(meta_info[meta_info.find('seqLen') + 10:meta_info.find('\nimWidth')])
        meta_info.close()

        self.evaluator = Evaluator(self.data_dir, '', 'mot')

    def _load_detections(self, seq_path):
        seq_dir = osp.join(self.gym_path, 'detections', seq_path)
        self.detections = np.load(osp.join(seq_dir, 'dets.npz'))
        self.features = np.load(osp.join(seq_dir, 'feats.npz'))

    def _write_results(self, results, data_type):
        timestamp = dt.datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
        results_dir = osp.join(self.data_dir, 'results', f'{timestamp}')
        if not osp.exists(results_dir):
            os.makedirs(results_dir)
        self.results_file = osp.join(results_dir, 'results.txt')
        
        if data_type == 'mot':
            save_format = '{frame},{id},{x1},{y1},{w},{h},1,-1,-1,-1\n'
        elif data_type == 'kitti':
            save_format = '{frame} {id} pedestrian 0 0 -10 {x1} {y1} {x2} {y2} -10 -10 -10 -1000 -1000 -1000 -10\n'
        else:
            raise ValueError(data_type)

        with open(self.results_file, 'w') as f:
            for frame_id, tlwhs, track_ids in results:
                if data_type == 'kitti':
                    frame_id -= 1
                for tlwh, track_id in zip(tlwhs, track_ids):
                    if track_id < 0:
                        continue
                    x1, y1, w, h = tlwh
                    x2, y2 = x1 + w, y1 + h
                    line = save_format.format(frame=frame_id, id=track_id, x1=x1, y1=y1, x2=x2, y2=y2, w=w, h=h)
                    f.write(line)
        print('save results to {}'.format(self.results_file))

    def _get_summary(self):
        name = Path(self.data_dir).name
        evaluator = Evaluator(self.data_dir, '', 'mot')
        acc = evaluator.eval_file(self.results_file)
        metrics = mm.metrics.motchallenge_metrics
        mh = mm.metrics.create()
        summary = mh.compute(
            acc, 
            metrics=metrics,
            name=name
        )

        strsummary = mm.io.render_summary(
            summary,
            formatters=mh.formatters,
            namemap=mm.io.motchallenge_metric_names,
        )

        print(strsummary)

    @staticmethod
    def _get_gym_path():
        gym_dir = osp.dirname(__file__)
        while osp.basename(gym_dir) != 'mot_gym':
            if gym_dir == '/':
                raise Exception('Could not find mot_gym path')
            parent = osp.join(gym_dir, os.pardir)
            gym_dir = os.path.abspath(parent)
        return gym_dir

    @staticmethod
    def _visualize_box(img, text, box, color_index, emphasis=False):
        x0, y0, width, height = box 
        x0, y0, width, height = int(x0), int(y0), int(width), int(height)
        color = (_COLORS[color_index%80] * 255).astype(np.uint8).tolist()
        txt_color = (0, 0, 0) if np.mean(_COLORS[color_index%80]) > 0.5 else (255, 255, 255)
        font = cv2.FONT_HERSHEY_SIMPLEX
        txt_size = cv2.getTextSize(text, font, 0.6, 1)[0]
        cv2.rectangle(img, (x0, y0), (x0+width, y0+height), color, 3 if emphasis else 1)

        txt_bk_color = (_COLORS[color_index%80] * 255 * 0.7).astype(np.uint8).tolist()
        cv2.rectangle(
            img,
            (x0, y0 + 1),
            (x0 + txt_size[0] + 1, y0 + int(1.5*txt_size[1])),
            txt_bk_color,
            -1
        )
        cv2.putText(img, text, (x0, y0 + txt_size[1]), font, 0.6, txt_color, thickness=1)
        return img
