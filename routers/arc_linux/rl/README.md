## Arc routing pair-level RL

This directory runs reinforcement-learning search for the arc-routing flow in the parent directory.

The action unit is one differential P/N pair. This is intentional: single-net cross-layer edits can make
`c.out.bin` segfault, while pair-level layer swaps have been verified to run and affect routing results.

## Files

- `run_dqn_arc.sh`
  Runs the default Double DQN search.
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

For a quick smoke test:

```bash
python train_dqn_arc.py \
  --eval-budget 4 \
  --n-envs 1 \
  --max-episode-steps 2 \
  --warmup 2 \
  --batch-size 2 \
  --device cpu \
  --tag smoke_arc_pair
```

By default, 10% of the evaluation budget is used for randomized seed-swap probes before DQN starts.
Set this explicitly with `--seed-swap-fraction 0.20`, or disable it with `--seed-swap-fraction 0`.
Use `--seed-swap-mode ordered` to take the first N probes in deterministic layer/slot order instead of
random sampling. Each probe swaps one same-slot differential pair between two layer groups.

### Initial order input

By default, RL starts from the C++ baseline `order_input.txt`. Use
`--initial-order <path>` to start from a complete manually supplied
`order_input.txt`-style layer/order file instead:

```bash
python train_dqn_arc.py \
  --eval-budget 20 \
  --device cpu \
  --initial-order ../order_input.txt \
  --tag manual_initial_arc
```

Input format is one net per line:

```text
<net> <layer> <order>
```

P/N nets must form complete differential pairs on the same layer. The file must
contain the same differential pair set as the baseline order file, and each
`(layer, pair-order)` slot must be unique. Order gaps are accepted and
normalized per layer before evaluation. `--fixed-csv` can be combined with
`--initial-order`; fixed placements are applied on top of the chosen initial
order.

### Best result outputs

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

Each populated best directory contains the route order/layer files:

- `order_input.txt`: native arc route order file.
- `layer_order.txt`: standalone copy of the same best layer/order assignment.

Each populated best directory also contains the corresponding routing result and
metrics:

- `ARC_output.txt` and `1234_1_arc_output.txt`: routed result files.
- `candidate_meta.json`: metrics for that best candidate, including routed net
  count, completion rate, total wire length, via count, and missing nets.
- `c.log` / `turn.log`: command logs when present.

The run root also contains `summary.json`, which records all best candidates and
the selected primary output via `primary_best_kind` and
`primary_best_order_txt`. By default it also contains `explanation.md`, a
non-LLM Markdown report comparing the initial candidate with the selected best
candidate and summarizing why the pair layer/order changes helped. Use
`--no-explain` to skip this report.

### Fixed CSV constraints

Use `--fixed-csv <path>` to lock selected differential pairs to explicit target
`layer/order` values. The CSV names either the P or N net; the whole pair is
locked.

```csv
net,layer,order
QSFPDD0_RX0_N_FPGA1,SIG05,4
```

The `order` column is always the pair slot order used by the pair-level RL model,
not the raw single-line P/N order in `order_input.txt`.

```bash
python train_dqn_arc.py \
  --eval-budget 4 \
  --n-envs 1 \
  --max-episode-steps 2 \
  --warmup 2 \
  --batch-size 2 \
  --device cpu \
  --fixed-csv fixed_pairs.csv \
  --tag smoke_fixed_arc
```

Unknown nets, invalid layers, duplicate fixed pairs, non-positive orders, or two
fixed pairs using the same `layer/order` fail before training starts. If a fixed
target slot is currently occupied by an unfixed pair, the fixed pair takes that
slot and the mutable pair is automatically reassigned to another available order.
Fixed pairs are excluded from DQN actions and from seed-swap probes, and generated
candidates keep their CSV pair layer/order unchanged.


## Optimization target

Candidates are ranked by routed net count first, then by lower total wire length. Via count is recorded but
not optimized because this arc flow currently emits `LINE` and `ARC` records, not generated via records.
