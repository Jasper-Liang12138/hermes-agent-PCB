## 135 GA Search

This directory contains the genetic/memetic search entrypoint for the Linux
135-degree routing flow.

## Files

- `run_ga_135.sh`
  Runs the default GA configuration.
- `train_ga_135.py`
  Main GA script: deterministic probes, randomized initialization, tournament
  selection, elitism, crossover, mutation, diversity restart, and local search.
- `explain_135.py`
  GA-local deterministic Markdown explanation generator for the selected best
  result.

The GA script reuses shared parsing, action legality, candidate evaluation, and
artifact export code from `../rl`.

## Usage

Run from this directory:

```bash
./run_ga_135.sh 500
```

Here `500` is the real candidate evaluation budget. The baseline/initial order is
recorded in the summary but does not consume this budget.

Manual invocation:

```bash
python train_ga_135.py --eval-budget 500 --tag ga_135
```

Optional inputs match the RL wrapper:

```bash
python train_ga_135.py \
  --eval-budget 100 \
  --initial-order ../order_out.txt \
  --fixed-csv fixed_nets.csv
```

## Outputs

Runs are written under:

```text
search_runs/<tag>_ga_layer_order_seed<seed>_budget<eval_budget>/
```

The run root contains `summary.json`, `best_layer_order.txt`, and, by default,
`explanation.md`. The Markdown report is generated without an LLM and compares
the initial candidate with the selected best candidate; use `--no-explain` to
skip it. Best artifact directories include `best_partial/`, `best_full/`,
`best_wire_full/`, and `best_via_full/`.
