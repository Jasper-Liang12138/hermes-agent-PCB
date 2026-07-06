## Windows Arc RL

This directory runs the current pair-level layer/order RL search for the Windows
arc-routing package in the parent directory. Candidate evaluation writes
`order_input.txt`, runs `arc_main.exe`, and collects metrics from `ARC_output.txt`.

The action unit is one adjacent two-net differential pair from `order_input.txt`.
Pair-level edits keep the arc flow's expected routing unit intact while RL changes
the pair layer and pair order.

## Files

- `run_dqn_arc.sh`
  Runs the default Double DQN search from a shell environment.
- `run_dqn_arc.bat`
  Runs the same search from Windows Command Prompt.
- `train_dqn_arc.py`
  Main training script.
- `routing_env_arc.py`
  Pair-level layer/order environment and action masks.
- `routing_eval_arc.py`
  Candidate evaluation, artifact export, and ranking helpers.
- `explain_arc.py`
  Deterministic Markdown explanation generator for the selected best result.
- `rl_arc_core.py`
  Parsing, order writing, command execution, and route statistics.
- `search_runs/`
  Generated experiment results.

## Usage

Run from this directory:

```bash
./run_dqn_arc.sh 200
```

Here `200` is the `eval_budget`, which means at most 200 real candidate
evaluations. You can also run the Python entry directly:

```bat
run_dqn_arc.bat 200
```

```bash
python train_dqn_arc.py --eval-budget 200 --device cpu
```

By default, 10% of the evaluation budget is used for randomized seed-swap probes
before DQN starts. Set this explicitly with `--seed-swap-fraction 0.20`, or
disable it with `--seed-swap-fraction 0`. Use `--seed-swap-mode ordered` to take
the first N probes in deterministic layer/slot order.

## Initial Order Input

By default, RL starts from the C++ baseline `order_input.txt` in the parent
directory. Use `--initial-order <path>` to start from a complete manually supplied
`order_input.txt`-style layer/order file instead:

```bash
python train_dqn_arc.py \
  --eval-budget 20 \
  --device cpu \
  --initial-order ../order_input.txt \
  --tag manual_initial_arc_windows
```

The expected format matches the Windows arc baseline file:

```text
<component>
<layer-block-count>
<row-count-for-layer-1>
<net-1> <layer> <raw-order>
<net-2> <layer> <raw-order>
...
<row-count-for-layer-2>
<net-1> <layer> <raw-order>
<net-2> <layer> <raw-order>
...
```

Within each layer block, every adjacent two rows are treated as one pair. The
file must contain the same adjacent pair set as the baseline order file, and each
`(layer, pair-order)` slot must be unique. Raw single-net orders are converted to
pair orders internally; order gaps are accepted and normalized per layer before
evaluation. `--fixed-csv` can be combined with `--initial-order`; fixed placements
are applied on top of the chosen initial order.

## Best Result Outputs

Each run writes outputs under:

```text
search_runs/<tag>_pair_layer_order_seed<seed>_budget<eval_budget>/
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

- `order_input.txt`: native Windows arc route order file.
- `layer_order.txt`: standalone copy of the same best layer/order assignment.

Each populated best directory also contains the corresponding routing result and
metrics:

- `ARC_output.txt`: routed result file from `arc_main.exe`.
- `candidate_meta.json`: metrics for that best candidate, including routed net
  count, completion rate, total wire length, via count, and missing nets.
- `main.log`: `arc_main.exe` command log when present.

The run root also contains `summary.json`, which records all best candidates and
the selected primary output via `primary_best_kind` and
`primary_best_order_txt`. By default it also contains `explanation.md`, a
non-LLM Markdown report comparing the initial candidate with the selected best
candidate and summarizing why the pair layer/order changes helped. Use
`--no-explain` to skip this report.

## Fixed CSV Constraints

Use `--fixed-csv <path>` to lock selected differential pairs to explicit target
`layer/order` values. The CSV names either net in the adjacent pair; the whole
pair is locked.

```csv
net,layer,order
QSFPDD0_RX0_N_FPGA1,Sig28,2
```

The `order` column is the pair slot order used by the pair-level RL model, not
the raw single-line net order in `order_input.txt`.

```bash
python train_dqn_arc.py \
  --eval-budget 4 \
  --n-envs 1 \
  --max-episode-steps 2 \
  --warmup 2 \
  --batch-size 2 \
  --device cpu \
  --fixed-csv fixed_pairs.csv \
  --tag smoke_fixed_arc_windows
```

Unknown nets, invalid layers, duplicate fixed pairs, non-positive orders, or two
fixed pairs using the same `layer/order` fail before training starts. If a fixed
target slot is currently occupied by an unfixed pair, the fixed pair takes that
slot and the mutable pair is automatically reassigned to another available order.
Fixed pairs are excluded from DQN actions and from seed-swap probes, and generated
candidates keep their CSV pair layer/order unchanged.

## Optimization Target

Candidates are ranked by routed net count first, then by lower total wire length.
Via count is recorded but not optimized because this flow currently emits `LINE`
and `ARC` records, not generated via records.

## Early stop by completion rate

All runs support `--stop-completion-rate FLOAT`. The default is `1.0`, so the search stops as soon as the best candidate reaches 100% routed completion. Use a lower threshold such as `--stop-completion-rate 0.99` to stop earlier, or `--stop-completion-rate 0` to disable this early stop and spend the full evaluation budget for additional wire/via optimization.

The summary JSON records `stop_completion_rate`, `early_stop_reached`, and `early_stop_eval`.
