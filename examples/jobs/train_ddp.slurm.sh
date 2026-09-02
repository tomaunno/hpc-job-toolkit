#!/bin/bash
# Multi-GPU PyTorch training on one node; the GPU count is detected at run time.
#   hpcjob submit train_ddp.slurm.sh -r "--gres=gpu:4 --nodes=1" -t 12:00:00 -- train.py
#SBATCH --nodes=1
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
source "$HOME/miniconda3/etc/profile.d/conda.sh" && conda activate myenv
NGPU=$(nvidia-smi -L | wc -l)
echo "GPUs: $NGPU"
torchrun --standalone --nproc_per_node="$NGPU" "$@"
