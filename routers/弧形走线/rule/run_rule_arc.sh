#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <eval_budget> [extra args...]" >&2
  exit 1
fi

EVAL_BUDGET="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python "$SCRIPT_DIR/rule_search_arc.py" \
  --tag rule_arc_exe \
  --eval-budget "$EVAL_BUDGET" \
  "$@"
