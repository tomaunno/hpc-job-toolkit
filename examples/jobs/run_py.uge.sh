#!/bin/bash
# Grid Engine version of run_py.  Resource and walltime come from `hpcjob submit`.
#   hpcjob submit run_py.uge.sh -r gpu_1=1 -t 2:00:00 -- train.py --epochs 10
#$ -cwd
#$ -j y
set -euo pipefail
module load cuda 2>/dev/null || true
source "$HOME/miniconda3/etc/profile.d/conda.sh" && conda activate myenv
export PYTHONUNBUFFERED=1
python "$@"
