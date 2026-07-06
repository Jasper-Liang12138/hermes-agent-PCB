## Windows Arc GA Search

This directory contains the genetic/memetic search entrypoint for the Windows
arc-routing package.

## Files

- `run_ga_arc.sh`
  Runs the default GA configuration from a shell environment.
- `run_ga_arc.bat`
  Runs the same GA search from Windows Command Prompt.
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

or from Command Prompt:

```bat
run_ga_arc.bat 200
```

Here `200` is the real candidate evaluation budget. The baseline/initial order is
recorded in the summary but does not consume this budget.

Manual invocation:

```bash
python train_ga_arc.py --eval-budget 200 --tag ga_arc_windows
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

## Early stop by completion rate

All runs support `--stop-completion-rate FLOAT`. The default is `1.0`, so the search stops as soon as the best candidate reaches 100% routed completion. Use a lower threshold such as `--stop-completion-rate 0.99` to stop earlier, or `--stop-completion-rate 0` to disable this early stop and spend the full evaluation budget for additional wire/via optimization.

The summary JSON records `stop_completion_rate`, `early_stop_reached`, and `early_stop_eval`.
