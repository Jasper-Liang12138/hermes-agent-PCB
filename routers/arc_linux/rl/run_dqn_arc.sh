#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <eval_budget> [extra args...]" >&2
  exit 1
fi

EVAL_BUDGET="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python "$SCRIPT_DIR/train_dqn_arc.py" \
  --device auto \
  --tag dqn_arc_pair \
  --n-envs 4 \
  --max-episode-steps 12 \
  --hidden-dim 256 \
  --lr 4e-4 \
  --gamma 0.975 \
  --batch-size 64 \
  --replay-size 6000 \
  --warmup 40 \
  --train-every 4 \
  --target-update 40 \
  --eps-start 0.9 \
  --eps-end 0.04 \
  --prune-strength 0.75 \
  --eval-budget "$EVAL_BUDGET" \
  "$@"
