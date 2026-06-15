#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <eval_budget> [extra args...]" >&2
  exit 1
fi

EVAL_BUDGET="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python "$SCRIPT_DIR/train_dqn_135.py" \
  --device cuda \
  --tag dqn_mf_best \
  --n-envs 4 \
  --max-episode-steps 16 \
  --hidden-dim 256 \
  --lr 4e-4 \
  --gamma 0.975 \
  --batch-size 64 \
  --replay-size 6000 \
  --warmup 80 \
  --train-every 4 \
  --target-update 40 \
  --eps-start 0.9 \
  --eps-end 0.04 \
  --neighbor-k 44 \
  --delta-order-threshold 2.5 \
  --prune-strength 0.85 \
  --eval-budget "$EVAL_BUDGET" \
  "$@"
