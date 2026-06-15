## Basic Flow

```bash
./d.out 402Pin_08BGA_8L_S_01141700.txt component_input.txt
./e.out net_list.txt 402Pin_08BGA_8L_S_01141700.txt
./f.out order_out.txt 402Pin_08BGA_8L_S_01141700.txt
python3 Turn_135_QYF.py 402Pin_08BGA_8L_S_01141700.txt line.out output.txt
```

## Search Wrappers

The reinforcement-learning version is stored in:

- `rl/run_dqn_135.sh`
- `rl/train_dqn_135.py`

See `rl/README.md` for details.

The genetic/memetic search version is stored separately in:

- `ga/run_ga_135.sh`
- `ga/train_ga_135.py`

Example:

```bash
cd ga
./run_ga_135.sh 500
```

Here `500` is the `eval_budget`, which means at most 500 real candidate evaluations.
See `ga/README.md` for GA details.
