#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    import torch
    import torch.nn.functional as F
    from torch import nn
except ModuleNotFoundError:
    torch = None
    F = None
    nn = None

import routing_eval_arc as eval_arc
import rl_arc_core as core
from routing_env_arc import ArcPairRoutingEnv


@dataclass
class Summary:
    tag: str
    variant: str
    seed: int
    eval_budget: int
    eval_used: int
    seed_swap_fraction: float
    seed_swap_evals: int
    seed_swap_mode: str
    env_steps: int
    n_envs: int
    max_episode_steps: int
    reached_full: bool
    first_full_eval: int | None
    best_partial: dict[str, Any]
    best_full: dict[str, Any] | None
    best_wire_full: dict[str, Any] | None
    fixed_csv: str | None
    fixed_pair_count: int
    initial_order: str | None
    initial_source: str
    primary_best_kind: str
    primary_best_order_txt: str | None


if nn is not None:
    class QNetwork(nn.Module):
        def __init__(self, feature_dim: int, pair_count: int, n_ops: int, hidden_dim: int = 256) -> None:
            super().__init__()
            self.n_actions = pair_count * n_ops
            input_dim = feature_dim * pair_count
            self.encoder = nn.Sequential(
                nn.Linear(input_dim, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.ReLU(),
            )
            self.q_head = nn.Linear(hidden_dim, self.n_actions)

        def forward(self, obs: torch.Tensor) -> torch.Tensor:
            if obs.dim() == 2:
                obs = obs.unsqueeze(0)
            return self.q_head(self.encoder(obs.reshape(obs.shape[0], -1)))


class ReplayBuffer:
    def __init__(self, capacity: int) -> None:
        self.buffer = deque(maxlen=capacity)

    def add(self, obs, mask, action, reward, next_obs, next_mask, done) -> None:
        self.buffer.append((obs, mask, action, reward, next_obs, next_mask, done))

    def sample(self, batch_size: int):
        batch = random.sample(self.buffer, batch_size)
        obs, mask, action, reward, next_obs, next_mask, done = zip(*batch)
        return (
            np.stack(obs),
            np.stack(mask),
            np.asarray(action, dtype=np.int64),
            np.asarray(reward, dtype=np.float32),
            np.stack(next_obs),
            np.stack(next_mask),
            np.asarray(done, dtype=np.float32),
        )

    def __len__(self) -> int:
        return len(self.buffer)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pair-level layer/order Double DQN for the Windows arc routing flow.")
    parser.add_argument("--eval-budget", type=int, default=200)
    parser.add_argument("--n-envs", type=int, default=4)
    parser.add_argument("--max-episode-steps", type=int, default=12)
    parser.add_argument("--hidden-dim", type=int, default=256)
    parser.add_argument("--lr", type=float, default=4e-4)
    parser.add_argument("--gamma", type=float, default=0.975)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--replay-size", type=int, default=6000)
    parser.add_argument("--warmup", type=int, default=40)
    parser.add_argument("--train-every", type=int, default=4)
    parser.add_argument("--target-update", type=int, default=40)
    parser.add_argument("--eps-start", type=float, default=0.9)
    parser.add_argument("--eps-end", type=float, default=0.04)
    parser.add_argument("--prune-strength", type=float, default=0.75)
    parser.add_argument("--missing-weight", type=float, default=10000.0)
    parser.add_argument("--wire-weight", type=float, default=0.02)
    parser.add_argument("--via-weight", type=float, default=0.0)
    parser.add_argument("--full-bonus", type=float, default=1000.0)
    parser.add_argument("--failure-penalty", type=float, default=5000.0)
    parser.add_argument("--seed", type=int, default=20260425)
    parser.add_argument("--seed-swap-fraction", type=float, default=0.10)
    parser.add_argument("--seed-swap-probes", type=int, default=None)
    parser.add_argument("--seed-swap-mode", choices=("random", "ordered"), default="random")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--tag", default="dqn_arc_windows")
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parent / "search_runs")
    parser.add_argument(
        "--fixed-csv",
        type=Path,
        default=None,
        help="Optional CSV with net,layer,order rows. Either net locks its whole adjacent pair.",
    )
    parser.add_argument(
        "--initial-order",
        type=Path,
        default=None,
        help="Optional complete order_input.txt-style file to use as the RL initial pair layer/order state.",
    )
    return parser.parse_args()


def resolve_device(name: str) -> torch.device:
    if torch is None:
        raise RuntimeError("PyTorch is not available")
    if name == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(name)


def random_valid_action(mask: np.ndarray) -> int:
    require_valid_actions(mask)
    valid = np.flatnonzero(mask > 0)
    return int(np.random.choice(valid))


def masked_argmax(q_values: np.ndarray, mask: np.ndarray) -> int:
    masked = np.where(mask > 0, q_values, -1e18)
    return int(masked.argmax())


def require_valid_actions(mask: np.ndarray, context: str = "action selection") -> None:
    if not np.any(mask > 0):
        raise RuntimeError(
            f"No legal mutable actions remain during {context}. "
            "Reduce --fixed-csv coverage or leave at least one mutable pair."
        )


def load_fixed_pair_targets(
    csv_path: Path | None,
    baseline_pairs: list[core.PairEntry],
    layer_names: list[str],
) -> dict[str, tuple[str, int]]:
    if csv_path is None:
        return {}

    net_to_pair: dict[str, core.PairEntry] = {}
    for pair in baseline_pairs:
        net_to_pair[pair.neg_net] = pair
        net_to_pair[pair.pos_net] = pair

    required_fields = {"net", "layer", "order"}
    valid_layers = set(layer_names)
    fixed_pair_targets: dict[str, tuple[str, int]] = {}
    fixed_slots: dict[tuple[str, int], str] = {}
    with csv_path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path} is empty; expected CSV header: net,layer,order")
        missing_fields = required_fields - set(reader.fieldnames)
        if missing_fields:
            raise ValueError(f"{csv_path} is missing required columns: {', '.join(sorted(missing_fields))}")
        for line_no, row in enumerate(reader, start=2):
            net = (row.get("net") or "").strip()
            layer = (row.get("layer") or "").strip()
            order_raw = (row.get("order") or "").strip()
            if not net and not layer and not order_raw:
                continue
            if not net or not layer or not order_raw:
                raise ValueError(f"{csv_path}:{line_no} must provide net, layer, and order")
            if net not in net_to_pair:
                raise ValueError(f"{csv_path}:{line_no} references unknown net {net}")
            if layer not in valid_layers:
                raise ValueError(
                    f"{csv_path}:{line_no} uses unknown layer {layer!r}; "
                    f"expected one of {', '.join(layer_names)}"
                )
            try:
                order = int(order_raw)
            except ValueError as exc:
                raise ValueError(f"{csv_path}:{line_no} has non-integer order {order_raw!r}") from exc
            if order <= 0:
                raise ValueError(f"{csv_path}:{line_no} order must be positive, got {order}")

            pair = net_to_pair[net]
            if pair.key in fixed_pair_targets:
                raise ValueError(
                    f"{csv_path}:{line_no} duplicates fixed pair {pair.key}; "
                    "use either the P net or the N net, not both"
                )
            slot = (layer, order)
            if slot in fixed_slots:
                raise ValueError(
                    f"{csv_path}:{line_no} conflicts with fixed pair {fixed_slots[slot]} "
                    f"at {layer},{order}"
                )
            fixed_pair_targets[pair.key] = slot
            fixed_slots[slot] = pair.key
    return fixed_pair_targets


def load_initial_pairs(
    order_path: Path | None,
    baseline_pairs: list[core.PairEntry],
    layer_names: list[str],
) -> list[core.PairEntry]:
    if order_path is None:
        return core.clone_pairs(baseline_pairs)

    pairs, _footer_lines, _input_layer_names = core.read_order_pairs(order_path.expanduser())
    if not pairs:
        raise ValueError(f"{order_path} does not contain any order rows")

    baseline_keys = {pair.key for pair in baseline_pairs}
    valid_layers = set(layer_names)
    seen_keys: set[str] = set()
    seen_slots: dict[tuple[str, int], str] = {}
    for pair in pairs:
        if pair.key in seen_keys:
            raise ValueError(f"{order_path} duplicates pair {pair.key}")
        if pair.key not in baseline_keys:
            raise ValueError(f"{order_path} references unknown pair {pair.key}")
        if pair.layer not in valid_layers:
            raise ValueError(
                f"{order_path} uses unknown layer {pair.layer!r}; "
                f"expected one of {', '.join(layer_names)}"
            )
        if pair.order <= 0:
            raise ValueError(f"{order_path} has non-positive pair order {pair.order} for {pair.key}")
        slot = (pair.layer, pair.order)
        if slot in seen_slots:
            raise ValueError(
                f"{order_path} assigns both {seen_slots[slot]} and {pair.key} to "
                f"{pair.layer},{pair.order}"
            )
        seen_keys.add(pair.key)
        seen_slots[slot] = pair.key

    missing = sorted(baseline_keys - seen_keys)
    if missing:
        raise ValueError(f"{order_path} is missing {len(missing)} baseline pairs, first missing: {missing[0]}")
    return core.canonicalize_pairs(pairs, layer_names)


def choose_primary_best(
    best_full: eval_arc.RouteCandidate | None,
    best_partial: eval_arc.RouteCandidate,
) -> tuple[str, eval_arc.RouteCandidate]:
    if best_full is not None:
        return "best_full", best_full
    return "best_partial", best_partial


def export_order_txt(candidate: eval_arc.RouteCandidate | None, dest: Path) -> str | None:
    if candidate is None or candidate.run_dir is None:
        return None
    src = Path(candidate.run_dir) / core.BASE_ORDER
    if not src.exists():
        return None
    shutil.copy2(src, dest)
    return dest.name


def build_env(
    env_id: int,
    output_root: Path,
    board_data,
    baseline_pairs,
    footer_lines,
    layer_names,
    baseline_stats,
    args: argparse.Namespace,
    fixed_pair_targets: dict[str, tuple[str, int]],
) -> ArcPairRoutingEnv:
    return ArcPairRoutingEnv(
        env_id=env_id,
        output_root=output_root,
        board_data=board_data,
        baseline_pairs=baseline_pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
        baseline_stats=baseline_stats,
        max_episode_steps=args.max_episode_steps,
        missing_weight=args.missing_weight,
        wire_weight=args.wire_weight,
        via_weight=args.via_weight,
        full_bonus=args.full_bonus,
        failure_penalty=args.failure_penalty,
        fixed_pair_targets=fixed_pair_targets,
    )


def seed_swap_candidates(
    run_dir: Path,
    baseline_pairs,
    footer_lines,
    layer_names,
    limit: int,
    rng: random.Random,
    mode: str,
    fixed_pair_keys: set[str],
) -> list[eval_arc.RouteCandidate]:
    if limit <= 0:
        return []
    candidates: list[eval_arc.RouteCandidate] = []
    max_order = max((pair.order for pair in baseline_pairs), default=0)
    swap_specs: list[tuple[int, str, str]] = []
    for order in range(1, max_order + 1):
        for left_idx, left_layer in enumerate(layer_names):
            for right_layer in layer_names[left_idx + 1 :]:
                swap_specs.append((order, left_layer, right_layer))
    if mode == "random":
        rng.shuffle(swap_specs)

    probe_index = 0
    for order, left_layer, right_layer in swap_specs:
        if len(candidates) >= limit:
            return candidates
        pairs = [type(pair)(pair.key, pair.neg_net, pair.pos_net, pair.layer, pair.order) for pair in baseline_pairs]
        left = [pair for pair in pairs if pair.layer == left_layer and pair.order == order]
        right = [pair for pair in pairs if pair.layer == right_layer and pair.order == order]
        if len(left) != 1 or len(right) != 1:
            continue
        if left[0].key in fixed_pair_keys or right[0].key in fixed_pair_keys:
            continue
        left[0].layer, right[0].layer = right[0].layer, left[0].layer
        candidate = eval_arc.evaluate_pairs(
            run_dir,
            footer_lines,
            layer_names,
            pairs,
            f"seed_swap_{probe_index:04d}",
            f"seed_swap:{left_layer}<->{right_layer}:slot{order}",
        )
        probe_index += 1
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def seed_swap_limit(args: argparse.Namespace) -> int:
    if args.seed_swap_probes is not None:
        return max(0, min(args.eval_budget, args.seed_swap_probes))
    fraction = max(0.0, min(1.0, args.seed_swap_fraction))
    return int(args.eval_budget * fraction)


def make_fixed_initial_env(
    env_id: int,
    output_root: Path,
    board_data,
    baseline_pairs,
    footer_lines,
    layer_names,
    baseline_stats,
    args: argparse.Namespace,
    fixed_pair_targets: dict[str, tuple[str, int]],
) -> ArcPairRoutingEnv:
    return build_env(
        env_id,
        output_root,
        board_data,
        baseline_pairs,
        footer_lines,
        layer_names,
        baseline_stats,
        args,
        fixed_pair_targets,
    )


def prepare_fixed_baseline(
    run_dir: Path,
    args: argparse.Namespace,
) -> tuple[
    list[core.PairEntry],
    list[str],
    list[str],
    eval_arc.RouteCandidate,
    core.BoardData,
    dict[str, tuple[str, int]],
    str,
]:
    baseline_output_root = None if args.fixed_csv is not None else run_dir
    baseline_pairs, footer_lines, layer_names, baseline_candidate, _board_data = eval_arc.prepare_baseline(baseline_output_root)
    source_pairs = load_initial_pairs(args.initial_order, baseline_pairs, layer_names)
    source_candidate = baseline_candidate
    initial_source = "baseline"
    if args.initial_order is not None:
        initial_source = "initial_order"
        if eval_arc.signature(source_pairs) != eval_arc.signature(baseline_pairs):
            evaluated = eval_arc.evaluate_pairs(
                run_dir,
                footer_lines,
                layer_names,
                source_pairs,
                "initial_order",
                f"initial_order:{args.initial_order}",
            )
            if evaluated is None:
                raise RuntimeError("Initial arc order evaluation failed")
            source_candidate = evaluated

    source_board_data = core.load_board_data(
        core.PROJECT_DIR,
        pairs=source_pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    fixed_pair_targets = load_fixed_pair_targets(args.fixed_csv, source_pairs, layer_names)
    if fixed_pair_targets:
        initial_source = f"{initial_source}+fixed_csv"
        print(f"Loaded {len(fixed_pair_targets)} fixed arc pairs from {args.fixed_csv}")
    setup_env = make_fixed_initial_env(
        0,
        run_dir,
        source_board_data,
        source_pairs,
        footer_lines,
        layer_names,
        source_candidate.stats,
        args,
        fixed_pair_targets,
    )
    initial_pairs = [core.PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in setup_env.pairs]
    initial_candidate = source_candidate
    if eval_arc.signature(initial_pairs) != eval_arc.signature(source_pairs):
        evaluated = eval_arc.evaluate_pairs(
            run_dir,
            footer_lines,
            layer_names,
            initial_pairs,
            "fixed_initial",
            "fixed_csv_initial",
        )
        if evaluated is None:
            raise RuntimeError("Fixed CSV initial arc routing evaluation failed")
        initial_candidate = evaluated
    initial_board_data = core.load_board_data(
        core.PROJECT_DIR,
        pairs=initial_pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    return initial_pairs, footer_lines, layer_names, initial_candidate, initial_board_data, fixed_pair_targets, initial_source


def write_summary(run_dir: Path, summary: Summary) -> Summary:
    (run_dir / "summary.json").write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    print(json.dumps(asdict(summary), indent=2))
    print(f"Arc pair layer/order run written to: {run_dir}")
    return summary


def train_bandit(args: argparse.Namespace, run_dir: Path) -> Summary:
    (
        baseline_pairs,
        footer_lines,
        layer_names,
        baseline_candidate,
        board_data,
        fixed_pair_targets,
        initial_source,
    ) = prepare_fixed_baseline(run_dir, args)
    fixed_pair_keys = set(fixed_pair_targets)
    env = build_env(
        0,
        run_dir,
        board_data,
        baseline_pairs,
        footer_lines,
        layer_names,
        baseline_candidate.stats,
        args,
        fixed_pair_targets,
    )
    state, _ = env.reset(0)
    require_valid_actions(env.get_action_mask(), "initial arc fixed-pair validation")
    q_values = np.zeros(env.pair_count * env.n_ops, dtype=np.float64)
    action_counts = np.zeros_like(q_values)
    eval_used = 0
    env_steps = 0
    episode_counter = 1
    first_full_eval: int | None = None
    best_partial = baseline_candidate
    best_full = baseline_candidate if baseline_candidate.stats.routed_nets == baseline_candidate.stats.total_nets else None
    best_wire = best_full
    seed_limit = seed_swap_limit(args)
    seed_rng = random.Random(args.seed + 13579)
    seed_evals = 0
    for candidate in seed_swap_candidates(
        run_dir,
        baseline_pairs,
        footer_lines,
        layer_names,
        seed_limit,
        seed_rng,
        args.seed_swap_mode,
        fixed_pair_keys,
    ):
        seed_evals += 1
        eval_used += 1
        if eval_arc.partial_key(candidate) > eval_arc.partial_key(best_partial):
            best_partial = candidate
        if candidate.stats.routed_nets == candidate.stats.total_nets:
            if first_full_eval is None:
                first_full_eval = eval_used
            if best_full is None or eval_arc.full_key(candidate) > eval_arc.full_key(best_full):
                best_full = candidate
            if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                best_wire = candidate

    while eval_used < args.eval_budget:
        epsilon = args.eps_end + (args.eps_start - args.eps_end) * max(
            0.0, (args.eval_budget - eval_used) / max(1, args.eval_budget)
        )
        mask = env.focus_action_mask(epsilon=epsilon, prune_strength=args.prune_strength)
        require_valid_actions(mask)
        if random.random() < epsilon:
            action = random_valid_action(mask)
        else:
            action = masked_argmax(q_values, mask)
        next_state, _next_mask, reward, done, info = env.step(action, eval_used + 1)
        del state
        state = next_state
        env_steps += 1
        if info.get("evaluated", False):
            eval_used += 1
            action_counts[action] += 1.0
            alpha = 1.0 / action_counts[action]
            q_values[action] += alpha * (reward - q_values[action])
            candidate = info.get("candidate")
            if candidate is not None:
                if eval_arc.partial_key(candidate) > eval_arc.partial_key(best_partial):
                    best_partial = candidate
                if candidate.stats.routed_nets == candidate.stats.total_nets:
                    if first_full_eval is None:
                        first_full_eval = eval_used
                    if best_full is None or eval_arc.full_key(candidate) > eval_arc.full_key(best_full):
                        best_full = candidate
                    if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                        best_wire = candidate
        if done:
            state, _ = env.reset(episode_counter)
            episode_counter += 1

    eval_arc.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval_arc.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval_arc.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")
    return write_summary(
        run_dir,
        Summary(
            tag=args.tag,
            variant="pair_layer_order_bandit_fallback",
            seed=args.seed,
            eval_budget=args.eval_budget,
            eval_used=eval_used,
            seed_swap_fraction=args.seed_swap_fraction,
            seed_swap_evals=seed_evals,
            seed_swap_mode=args.seed_swap_mode,
            env_steps=env_steps,
            n_envs=1,
            max_episode_steps=args.max_episode_steps,
            reached_full=best_full is not None,
            first_full_eval=first_full_eval,
            best_partial=eval_arc.candidate_to_json(best_partial),
            best_full=eval_arc.candidate_to_json(best_full),
            best_wire_full=eval_arc.candidate_to_json(best_wire),
            fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
            fixed_pair_count=len(fixed_pair_targets),
            initial_order=str(args.initial_order) if args.initial_order is not None else None,
            initial_source=initial_source,
            primary_best_kind=primary_best_kind,
            primary_best_order_txt=primary_best_order_txt,
        ),
    )


def train(args: argparse.Namespace) -> Summary:
    random.seed(args.seed)
    np.random.seed(args.seed)
    if torch is not None:
        torch.manual_seed(args.seed)

    run_dir = args.output_root / f"{args.tag}_pair_layer_order_seed{args.seed}_budget{args.eval_budget}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if torch is None:
        print("PyTorch is not available; using numpy bandit fallback search.")
        return train_bandit(args, run_dir)

    (
        baseline_pairs,
        footer_lines,
        layer_names,
        baseline_candidate,
        board_data,
        fixed_pair_targets,
        initial_source,
    ) = prepare_fixed_baseline(run_dir, args)
    baseline_stats = baseline_candidate.stats
    fixed_pair_keys = set(fixed_pair_targets)

    probe_env = build_env(0, run_dir, board_data, baseline_pairs, footer_lines, layer_names, baseline_stats, args, fixed_pair_targets)
    require_valid_actions(probe_env.get_action_mask(), "initial arc fixed-pair validation")
    feature_dim = probe_env.feature_dim
    pair_count = probe_env.pair_count
    n_ops = probe_env.n_ops

    device = resolve_device(args.device)
    q_net = QNetwork(feature_dim, pair_count, n_ops, args.hidden_dim).to(device)
    target_net = QNetwork(feature_dim, pair_count, n_ops, args.hidden_dim).to(device)
    target_net.load_state_dict(q_net.state_dict())
    target_net.eval()
    optimizer = torch.optim.Adam(q_net.parameters(), lr=args.lr)
    replay = ReplayBuffer(args.replay_size)

    envs = [
        build_env(i, run_dir, board_data, baseline_pairs, footer_lines, layer_names, baseline_stats, args, fixed_pair_targets)
        for i in range(args.n_envs)
    ]

    states = []
    for env in envs:
        obs, _ = env.reset(0)
        require_valid_actions(env.get_action_mask(), "initial arc fixed-pair validation")
        states.append(obs)

    eval_used = 0
    env_steps = 0
    episode_counter = args.n_envs
    train_steps = 0
    first_full_eval: int | None = None
    best_partial = baseline_candidate
    best_full = baseline_candidate if baseline_stats.routed_nets == baseline_stats.total_nets else None
    best_wire = best_full
    seed_limit = seed_swap_limit(args)
    seed_rng = random.Random(args.seed + 13579)
    seed_evals = 0
    for candidate in seed_swap_candidates(
        run_dir,
        baseline_pairs,
        footer_lines,
        layer_names,
        seed_limit,
        seed_rng,
        args.seed_swap_mode,
        fixed_pair_keys,
    ):
        seed_evals += 1
        eval_used += 1
        if eval_arc.partial_key(candidate) > eval_arc.partial_key(best_partial):
            best_partial = candidate
        if candidate.stats.routed_nets == candidate.stats.total_nets:
            if first_full_eval is None:
                first_full_eval = eval_used
            if best_full is None or eval_arc.full_key(candidate) > eval_arc.full_key(best_full):
                best_full = candidate
            if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                best_wire = candidate

    while eval_used < args.eval_budget:
        epsilon = args.eps_end + (args.eps_start - args.eps_end) * max(
            0.0, (args.eval_budget - eval_used) / max(1, args.eval_budget)
        )
        state_tensor = torch.tensor(np.stack(states), dtype=torch.float32, device=device)
        with torch.no_grad():
            q_values = q_net(state_tensor).cpu().numpy()

        next_states = []
        for i, env in enumerate(envs):
            if eval_used >= args.eval_budget:
                next_states.append(states[i])
                continue
            action_mask = env.focus_action_mask(epsilon=epsilon, prune_strength=args.prune_strength)
            require_valid_actions(action_mask)
            if random.random() < epsilon:
                action = random_valid_action(action_mask)
            else:
                action = masked_argmax(q_values[i], action_mask)

            next_obs, _legal_next_mask, reward, done, info = env.step(action, eval_used + 1)
            env_steps += 1
            next_action_mask = env.focus_action_mask(epsilon=epsilon, prune_strength=args.prune_strength)
            if info.get("evaluated", False):
                eval_used += 1
                candidate = info.get("candidate")
                if candidate is not None:
                    if eval_arc.partial_key(candidate) > eval_arc.partial_key(best_partial):
                        best_partial = candidate
                    if candidate.stats.routed_nets == candidate.stats.total_nets:
                        if first_full_eval is None:
                            first_full_eval = eval_used
                        if best_full is None or eval_arc.full_key(candidate) > eval_arc.full_key(best_full):
                            best_full = candidate
                        if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                            best_wire = candidate

            replay.add(states[i], action_mask, action, reward, next_obs, next_action_mask, done)
            if done:
                next_obs, _ = env.reset(episode_counter)
                episode_counter += 1
            next_states.append(next_obs)
        states = next_states

        if len(replay) >= max(args.warmup, args.batch_size) and env_steps % args.train_every == 0:
            obs_b, mask_b, act_b, rew_b, next_obs_b, next_mask_b, done_b = replay.sample(args.batch_size)
            obs_t = torch.tensor(obs_b, dtype=torch.float32, device=device)
            mask_t = torch.tensor(mask_b, dtype=torch.float32, device=device)
            act_t = torch.tensor(act_b, dtype=torch.int64, device=device)
            rew_t = torch.tensor(rew_b, dtype=torch.float32, device=device)
            next_obs_t = torch.tensor(next_obs_b, dtype=torch.float32, device=device)
            next_mask_t = torch.tensor(next_mask_b, dtype=torch.float32, device=device)
            done_t = torch.tensor(done_b, dtype=torch.float32, device=device)

            q_pred_all = q_net(obs_t).masked_fill(mask_t <= 0, -1e9)
            q_pred = q_pred_all.gather(1, act_t.unsqueeze(1)).squeeze(1)
            with torch.no_grad():
                next_q_online = q_net(next_obs_t).masked_fill(next_mask_t <= 0, -1e9)
                next_actions = torch.argmax(next_q_online, dim=1)
                next_q_target = target_net(next_obs_t).masked_fill(next_mask_t <= 0, -1e9)
                next_q = next_q_target.gather(1, next_actions.unsqueeze(1)).squeeze(1)
                targets = rew_t + args.gamma * next_q * (1.0 - done_t)
            loss = F.smooth_l1_loss(q_pred, targets)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(q_net.parameters(), 1.0)
            optimizer.step()
            train_steps += 1
            if train_steps % args.target_update == 0:
                target_net.load_state_dict(q_net.state_dict())

    eval_arc.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval_arc.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval_arc.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = Summary(
        tag=args.tag,
        variant="pair_layer_order_dqn",
        seed=args.seed,
        eval_budget=args.eval_budget,
        eval_used=eval_used,
        seed_swap_fraction=args.seed_swap_fraction,
        seed_swap_evals=seed_evals,
        seed_swap_mode=args.seed_swap_mode,
        env_steps=env_steps,
        n_envs=args.n_envs,
        max_episode_steps=args.max_episode_steps,
        reached_full=best_full is not None,
        first_full_eval=first_full_eval,
        best_partial=eval_arc.candidate_to_json(best_partial),
        best_full=eval_arc.candidate_to_json(best_full),
        best_wire_full=eval_arc.candidate_to_json(best_wire),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_pair_count=len(fixed_pair_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
    )
    return write_summary(run_dir, summary)


if __name__ == "__main__":
    train(parse_args())
