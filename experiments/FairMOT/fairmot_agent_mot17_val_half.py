from pathlib import Path
from fairmot_exp_utils import *

AGENT_PATH = '/home/dchipping/project/dan-track/ahm-agent/results/ppo/checkpoint-50'
MODEL_PATH = '/home/dchipping/project/dan-track/ahm-agent/motgym/trackers/FairMOT/models/fairmot_dla34.pth'
DATA_DIR = '/home/dchipping/project/dan-track/ahm-agent/motgym/datasets/MOT17/val_half'
RESULTS_DIR = ''

conf_thres = 0.4
opt = opts().init(['mot', f'--load_model={MODEL_PATH}', f'--data_dir={DATA_DIR}',
                   f'--conf_thres={conf_thres}'])
seqs = sorted(filter(lambda x: not '.txt' in x, os.listdir(DATA_DIR)))

main(opt,
     data_root=DATA_DIR,
     seqs=seqs,
     exp_name=Path(__file__).stem,
     # results_dir=RESULTS_DIR,
     run_name='ppo_lookup_10',
     lookup_gallery=10,
     show_image=False,
     agent_path=AGENT_PATH)
