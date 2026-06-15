## Arc GA Search

This directory contains the genetic/memetic search entrypoint for the Linux arc
routing flow.

## Files

- `run_ga_arc.sh`
  Runs the default GA configuration.
- `train_ga_arc.py`
  Main GA script: seed-swap probes, deterministic action probes, randomized
  initialization, tournament selection, elitism, crossover, mutation, diversity
  restart, and local search.
- `explain_arc.py`
  GA-local deterministic Markdown explanation generator for the selected best
  result.

The GA script reuses shared parsing, pair-level action legality, candidate
evaluation, and artifact export code from `../rl`.

## Usage

Run from this directory:

```bash
./run_ga_arc.sh 200
```

Here `200` is the real candidate evaluation budget. The baseline/initial order is
recorded in the summary but does not consume this budget.

Manual invocation:

```bash
python train_ga_arc.py --eval-budget 200 --tag ga_arc
```

Optional inputs match the RL wrapper:

```bash
python train_ga_arc.py \
  --eval-budget 100 \
  --initial-order ../order_input.txt \
  --fixed-csv fixed_pairs.csv
```

## Outputs

Runs are written under:

```text
search_runs/<tag>_ga_pair_layer_order_seed<seed>_budget<eval_budget>/
```

The run root contains `summary.json`, `best_layer_order.txt`, and, by default,
`explanation.md`. The Markdown report is generated without an LLM and compares
the initial candidate with the selected best candidate; use `--no-explain` to
skip it. Best artifact directories include `best_partial/`, `best_full/`, and
`best_wire_full/`.
