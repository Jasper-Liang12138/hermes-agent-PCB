# 135 exe rule optimizer

Run from `bk_routing/135度走线`:

```bash
./rule/run_rule_135.sh 50
./rule/run_rule_135.sh 50 --stop-completion-rate 0
```

The first argument is the real evaluation budget. By default the search stops when the best candidate reaches `completion_rate >= 1.0`; use `--stop-completion-rate 0` to disable early stopping and spend the full budget.

Outputs are written under `rule/search_runs/rule/` with `summary.json`, `best_layer_order.txt`, `best_partial/`, `best_full/`, `best_wire_full/`, and `rule_trace.jsonl`.
