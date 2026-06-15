## Files
- `run_dqn_135.sh`
  Runs the current Double DQN `missing_focus` configuration.
- `train_dqn_135.py`
  Main training script. Runs Double DQN training, evaluation, and result export.
- `missing_focus_env_135.py`
  Shared missing-focus environment wrapper used by the current DQN variant.
- `routing_env_135.py`
  Environment layer. Defines state, action, reward, mask, and the behavior of one environment step.
- `routing_eval_135.py`
  Evaluation layer. Writes candidate `order_out.txt`, runs `f.out`, and collects routing statistics from `line.out`.
  Full `output.txt` generation is deferred to saved best artifacts to reduce search-run disk usage.
- `explain_135.py`
  Deterministic Markdown explanation generator for the selected best result.
- `rl_135_core.py`
  Shared utility layer. Handles input copying, parsing, statistics, and command execution.
- `search_runs/`
  Saved experiment results.

## Usage
Run from this directory:

```bash
./run_dqn_135.sh 500
```

Here `500` is the `eval_budget`, which means at most 500 real candidate evaluations.

If you want to override parameters manually, run:

```bash
python train_dqn_135.py --eval-budget 500 --device cuda
```

### Initial order input

By default, RL starts from the C++ baseline `order_out.txt`. Use
`--initial-order <path>` to start from a complete manually supplied
`order_out.txt`-style layer/order file instead:

```bash
python train_dqn_135.py \
  --eval-budget 20 \
  --device cpu \
  --initial-order ../order_out.txt \
  --tag manual_initial_135
```

Input format is one route per line:

```text
<net> <TOP|BOTTOM> <order>
```

The file must contain the same net set as the baseline order file, and each
`(layer, order)` slot must be unique. Order gaps are accepted and normalized per
layer before evaluation. `--fixed-csv` can be combined with `--initial-order`;
fixed placements are applied on top of the chosen initial order.

### Best result outputs

Each run writes outputs under:

```text
search_runs/<tag>_missing_focus_seed<seed>_budget<eval_budget>/
```

The primary best layer/order assignment is:

```text
best_layer_order.txt
```

This file is copied from `best_full` when a full-route candidate exists;
otherwise it is copied from `best_partial`.

Saved best directories include:

- `best_partial/`: best completion-rate candidate.
- `best_full/`: best full-route candidate, if any.
- `best_wire_full/`: full-route candidate with shortest wire length, if any.
- `best_via_full/`: full-route candidate with lowest via count, if any.

Each populated best directory contains the route order/layer files:

- `order_out.txt`: native 135 route order file.
- `layer_order.txt`: standalone copy of the same best layer/order assignment.

Each populated best directory also contains the corresponding routing result and
metrics:

- `line.out` and `output.txt`: routed result files.
- `candidate_meta.json`: metrics for that best candidate, including routed net
  count, completion rate, total wire length, via count, and missing nets.
- `f.log` / `turn.log`: command logs when present.

The run root also contains `summary.json`, which records all best candidates and
the selected primary output via `primary_best_kind` and
`primary_best_order_txt`. By default it also contains `explanation.md`, a
non-LLM Markdown report comparing the initial candidate with the selected best
candidate and summarizing why the layer/order changes helped. Use
`--no-explain` to skip this report.

### Fixed CSV constraints

Use `--fixed-csv <path>` to lock selected single nets to explicit target
`layer/order` values while RL searches over the remaining nets:

```csv
net,layer,order
N34028220,BOTTOM,4
N34029110,TOP,1
```

```bash
python train_dqn_135.py \
  --eval-budget 4 \
  --n-envs 1 \
  --max-episode-steps 2 \
  --warmup 2 \
  --batch-size 2 \
  --device cpu \
  --fixed-csv fixed_nets.csv \
  --tag smoke_fixed_135
```

The CSV is the requested fixed placement, not a baseline-matching check. Unknown
nets, invalid layers, duplicate fixed nets, non-positive orders, or two fixed nets
using the same `layer/order` fail before training starts. If a fixed target slot is
currently occupied by an unfixed net, the fixed net takes that slot and the mutable
net is automatically reassigned to another available order. Fixed nets get an
all-zero action mask, and every generated candidate keeps their CSV layer/order
unchanged.
