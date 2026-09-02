#!/bin/bash
# Run one Python script on a compute node.
#   hpcjob submit run_py.slurm.sh -r "--gres=gpu:1 --nodes=1" -t 2:00:00 -- train.py --epochs 10
#SBATCH --nodes=1
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
module load cuda 2>/dev/null || true
source "$HOME/miniconda3/etc/profile.d/conda.sh" && conda activate myenv
export PYTHONUNBUFFERED=1
python "$@"
