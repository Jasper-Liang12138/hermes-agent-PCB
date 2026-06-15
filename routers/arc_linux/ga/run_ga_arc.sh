#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <eval_budget> [extra args...]" >&2
  exit 1
fi

EVAL_BUDGET="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python "$SCRIPT_DIR/train_ga_arc.py" \
  --algorithm ga \
  --tag ga_arc \
  --population-size 32 \
  --elite-size 4 \
  --mutation-rate 0.35 \
  --crossover-rate 0.60 \
  --local-search-rate 0.25 \
  --eval-budget "$EVAL_BUDGET" \
  "$@"
