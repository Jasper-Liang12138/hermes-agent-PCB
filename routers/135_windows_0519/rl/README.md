## Windows 135 RL

This directory runs the current layer/order RL search for the Windows 135-degree
routing package in the parent directory. Candidate evaluation writes
`order_input.txt`, runs `135_main.exe`, and collects metrics from `line.out` and
`statistical.out`.

## Files

- `run_dqn_135.sh`
  Runs the default Double DQN search from a shell environment.
- `run_dqn_135.bat`
  Runs the same search from Windows Command Prompt.
- `train_dqn_135.py`
  Main training script. Runs Double DQN training, evaluation, and result export.
- `routing_env_135.py`
  Net-level layer/order environment and action masks.
- `routing_eval_135.py`
  Candidate evaluation, artifact export, and ranking helpers.
- `rl_135_core.py`
  Shared parsing, order writing, input copying, statistics, and command helpers.
- `search_runs/`
  Generated experiment results.

## Usage

Run from this directory:

```bash
./run_dqn_135.sh 500
```

Here `500` is the `eval_budget`, which means at most 500 real candidate
evaluations. You can also run the Python entry directly:

```bat
run_dqn_135.bat 500
```

```bash
python train_dqn_135.py --eval-budget 500 --device cpu
```

## Initial Order Input

By default, RL starts from the C++ baseline `order_input.txt` in the parent
directory. Use `--initial-order <path>` to start from a complete manually supplied
`order_input.txt`-style layer/order file instead:

```bash
python train_dqn_135.py \
  --eval-budget 20 \
  --device cpu \
  --initial-order ../order_input.txt \
  --tag manual_initial_135_windows
```

The expected format matches the Windows 135 baseline file:

```text
<component>
<layer-block-count>
<row-count-for-layer-1>
<net> <layer> <order>
...
<row-count-for-layer-2>
<net> <layer> <order>
...
```

The file must contain the same net set as the baseline order file, and each
`(layer, order)` slot must be unique. Order gaps are accepted and normalized per
layer before evaluation. `--fixed-csv` can be combined with `--initial-order`;
fixed placements are applied on top of the chosen initial order.

## Best Result Outputs

Each run writes outputs under:

```text
search_runs/<tag>_entry_layer_order_seed<seed>_budget<eval_budget>/
```

The primary best layer/order assignment is:

```text
best_layer_order.txt
```

This file is copied from `best_full` when a full-route candidate exists;
otherwise it is copied from `best_partial`.

Saved best directories include:

- `best_partial/`: best routed-net-count candidate.
- `best_full/`: best full-route candidate, if any.
- `best_wire_full/`: full-route candidate with shortest wire length, if any.

Each populated best directory contains the order/layer files:

- `order_input.txt`: native Windows 135 route order file.
- `layer_order.txt`: standalone copy of the same best layer/order assignment.

Each populated best directory also contains the corresponding routing result and
metrics:

- `line.out` and `statistical.out`: routed result and native statistics files.
- `candidate_meta.json`: metrics for that best candidate, including routed net
  count, completion rate, total wire length, via count, and missing nets.
- `main.log`: `135_main.exe` command log when present.

The run root also contains `summary.json`, which records all best candidates and
the selected primary output via `primary_best_kind` and
`primary_best_order_txt`.

## Fixed CSV Constraints

Use `--fixed-csv <path>` to lock selected single nets to explicit target
`layer/order` values while RL searches over the remaining nets:

```csv
net,layer,order
FLASH_D3,Power03,1
LCD_CLK,Gnd02,14
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
  --tag smoke_fixed_135_windows
```

Unknown nets, invalid layers, duplicate fixed nets, non-positive orders, or two
fixed nets using the same `layer/order` fail before training starts. If a fixed
target slot is currently occupied by an unfixed net, the fixed net takes that
slot and the mutable net is automatically reassigned to another available order.
Fixed nets are excluded from DQN actions and from seed-swap probes, and generated
candidates keep their CSV layer/order unchanged.

## Optimization Target

Candidates are ranked by routed net count first, then by lower total wire length.
Via count is recorded but not optimized by default.
