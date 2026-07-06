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

from explain_135 import generate_explanation
import routing_eval_135 as eval135
import rl_135_core as core
from routing_env_135 import Windows135RoutingEnv


@dataclass
class Summary:
    tag: str
    variant: str
    seed: int
    eval_budget: int
    eval_used: int
    stop_completion_rate: float
    early_stop_reached: bool
    early_stop_eval: int | None
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
    fixed_net_count: int
    initial_order: str | None
    initial_source: str
    primary_best_kind: str
    primary_best_order_txt: str | None


if nn is not None:
    class QNetwork(nn.Module):
        def __init__(self, feature_dim: int, net_count: int, n_ops: int, hidden_dim: int = 256) -> None:
            super().__init__()
            self.n_actions = net_count * n_ops
            input_dim = feature_dim * net_count
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
    parser = argparse.ArgumentParser(description="Net-level layer/order Double DQN for the Windows 135 routing flow.")
    parser.add_argument("--stop-completion-rate", type=float, default=1.0, help="Stop once best completion_rate reaches this value; use 0 to disable early stop.")
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
    parser.add_argument("--tag", default="dqn_135_windows")
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parent / "search_runs")
    parser.add_argument("--no-explain", action="store_true", help="Skip deterministic explanation.md generation.")
    parser.add_argument(
        "--fixed-csv",
        type=Path,
        default=None,
        help="Optional CSV with net,layer,order rows. Each row locks one net.",
    )
    parser.add_argument(
        "--initial-order",
        type=Path,
        default=None,
        help="Optional complete order_input.txt-style file to use as the RL initial entry layer/order state.",
    )
    return parser.parse_args()


def validate_stop_completion_rate(args: argparse.Namespace) -> None:
    if args.stop_completion_rate < 0.0 or args.stop_completion_rate > 1.0:
        raise ValueError("--stop-completion-rate must be between 0 and 1")


def completion_target_reached(candidate: eval135.RouteCandidate | None, args: argparse.Namespace) -> bool:
    return (
        args.stop_completion_rate > 0.0
        and candidate is not None
        and candidate.stats.completion_rate >= args.stop_completion_rate
    )


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
            "Reduce --fixed-csv coverage or leave at least one mutable entry."
        )


def load_fixed_targets(
    csv_path: Path | None,
    baseline_entries: list[core.OrderEntry],
    layer_names: list[str],
) -> dict[str, tuple[str, int]]:
    if csv_path is None:
        return {}

    net_to_entry: dict[str, core.OrderEntry] = {}
    for entry in baseline_entries:
        net_to_entry[entry.net] = entry

    required_fields = {"net", "layer", "order"}
    valid_layers = set(layer_names)
    fixed_targets: dict[str, tuple[str, int]] = {}
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
            if net not in net_to_entry:
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

            entry = net_to_entry[net]
            if entry.net in fixed_targets:
                raise ValueError(f"{csv_path}:{line_no} duplicates fixed net {entry.net}")
            slot = (layer, order)
            if slot in fixed_slots:
                raise ValueError(
                    f"{csv_path}:{line_no} conflicts with fixed entry {fixed_slots[slot]} "
                    f"at {layer},{order}"
                )
            fixed_targets[entry.net] = slot
            fixed_slots[slot] = entry.net
    return fixed_targets


def load_initial_entries(
    order_path: Path | None,
    baseline_entries: list[core.OrderEntry],
    layer_names: list[str],
) -> list[core.OrderEntry]:
    if order_path is None:
        return core.clone_entries(baseline_entries)

    entries, _footer_lines, _input_layer_names = core.read_order_entries(order_path.expanduser())
    if not entries:
        raise ValueError(f"{order_path} does not contain any order rows")

    baseline_nets = {entry.net for entry in baseline_entries}
    valid_layers = set(layer_names)
    seen_nets: set[str] = set()
    seen_slots: dict[tuple[str, int], str] = {}
    for entry in entries:
        if entry.net in seen_nets:
            raise ValueError(f"{order_path} duplicates net {entry.net}")
        if entry.net not in baseline_nets:
            raise ValueError(f"{order_path} references unknown net {entry.net}")
        if entry.layer not in valid_layers:
            raise ValueError(
                f"{order_path} uses unknown layer {entry.layer!r}; "
                f"expected one of {', '.join(layer_names)}"
            )
        if entry.order <= 0:
            raise ValueError(f"{order_path} has non-positive order {entry.order} for {entry.net}")
        slot = (entry.layer, entry.order)
        if slot in seen_slots:
            raise ValueError(
                f"{order_path} assigns both {seen_slots[slot]} and {entry.net} to "
                f"{entry.layer},{entry.order}"
            )
        seen_nets.add(entry.net)
        seen_slots[slot] = entry.net

    missing = sorted(baseline_nets - seen_nets)
    if missing:
        raise ValueError(f"{order_path} is missing {len(missing)} baseline nets, first missing: {missing[0]}")
    return core.canonicalize_entries(entries, layer_names)


def choose_primary_best(
    best_full: eval135.RouteCandidate | None,
    best_partial: eval135.RouteCandidate,
) -> tuple[str, eval135.RouteCandidate]:
    if best_full is not None:
        return "best_full", best_full
    return "best_partial", best_partial


def export_order_txt(candidate: eval135.RouteCandidate | None, dest: Path) -> str | None:
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
    baseline_entries,
    footer_lines,
    layer_names,
    baseline_stats,
    args: argparse.Namespace,
    fixed_targets: dict[str, tuple[str, int]],
) -> Windows135RoutingEnv:
    return Windows135RoutingEnv(
        env_id=env_id,
        output_root=output_root,
        board_data=board_data,
        baseline_entries=baseline_entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
        baseline_stats=baseline_stats,
        max_episode_steps=args.max_episode_steps,
        missing_weight=args.missing_weight,
        wire_weight=args.wire_weight,
        via_weight=args.via_weight,
        full_bonus=args.full_bonus,
        failure_penalty=args.failure_penalty,
        fixed_targets=fixed_targets,
    )


def seed_swap_candidates(
    run_dir: Path,
    baseline_entries,
    footer_lines,
    layer_names,
    limit: int,
    rng: random.Random,
    mode: str,
    fixed_nets: set[str],
) -> list[eval135.RouteCandidate]:
    if limit <= 0:
        return []
    candidates: list[eval135.RouteCandidate] = []
    max_order = max((entry.order for entry in baseline_entries), default=0)
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
        entries = [type(entry)(entry.net, entry.layer, entry.order) for entry in baseline_entries]
        left = [entry for entry in entries if entry.layer == left_layer and entry.order == order]
        right = [entry for entry in entries if entry.layer == right_layer and entry.order == order]
        if len(left) != 1 or len(right) != 1:
            continue
        if left[0].net in fixed_nets or right[0].net in fixed_nets:
            continue
        left[0].layer, right[0].layer = right[0].layer, left[0].layer
        candidate = eval135.evaluate_entries(
            run_dir,
            footer_lines,
            layer_names,
            entries,
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
    baseline_entries,
    footer_lines,
    layer_names,
    baseline_stats,
    args: argparse.Namespace,
    fixed_targets: dict[str, tuple[str, int]],
) -> Windows135RoutingEnv:
    return build_env(
        env_id,
        output_root,
        board_data,
        baseline_entries,
        footer_lines,
        layer_names,
        baseline_stats,
        args,
        fixed_targets,
    )


def prepare_fixed_baseline(
    run_dir: Path,
    args: argparse.Namespace,
) -> tuple[
    list[core.OrderEntry],
    list[str],
    list[str],
    eval135.RouteCandidate,
    core.BoardData,
    dict[str, tuple[str, int]],
    str,
]:
    baseline_output_root = None if args.fixed_csv is not None else run_dir
    baseline_entries, footer_lines, layer_names, baseline_candidate, _board_data = eval135.prepare_baseline(baseline_output_root)
    source_entries = load_initial_entries(args.initial_order, baseline_entries, layer_names)
    source_candidate = baseline_candidate
    initial_source = "baseline"
    if args.initial_order is not None:
        initial_source = "initial_order"
        if eval135.signature(source_entries) != eval135.signature(baseline_entries):
            evaluated = eval135.evaluate_entries(
                run_dir,
                footer_lines,
                layer_names,
                source_entries,
                "initial_order",
                f"initial_order:{args.initial_order}",
            )
            if evaluated is None:
                raise RuntimeError("Initial 135 Windows order evaluation failed")
            source_candidate = evaluated

    source_board_data = core.load_board_data(
        core.PROJECT_DIR,
        entries=source_entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    fixed_targets = load_fixed_targets(args.fixed_csv, source_entries, layer_names)
    if fixed_targets:
        initial_source = f"{initial_source}+fixed_csv"
        print(f"Loaded {len(fixed_targets)} fixed 135 Windows nets from {args.fixed_csv}")
    setup_env = make_fixed_initial_env(
        0,
        run_dir,
        source_board_data,
        source_entries,
        footer_lines,
        layer_names,
        source_candidate.stats,
        args,
        fixed_targets,
    )
    initial_entries = [core.OrderEntry(p.net, p.layer, p.order) for p in setup_env.entries]
    initial_candidate = source_candidate
    if eval135.signature(initial_entries) != eval135.signature(source_entries):
        evaluated = eval135.evaluate_entries(
            run_dir,
            footer_lines,
            layer_names,
            initial_entries,
            "fixed_initial",
            "fixed_csv_initial",
        )
        if evaluated is None:
            raise RuntimeError("Fixed CSV initial 135 Windows routing evaluation failed")
        initial_candidate = evaluated
    initial_board_data = core.load_board_data(
        core.PROJECT_DIR,
        entries=initial_entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    return initial_entries, footer_lines, layer_names, initial_candidate, initial_board_data, fixed_targets, initial_source


def write_summary(run_dir: Path, summary: Summary) -> Summary:
    (run_dir / "summary.json").write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    print(json.dumps(asdict(summary), indent=2))
    print(f"135 Windows layer/order run written to: {run_dir}")
    return summary


def train_bandit(args: argparse.Namespace, run_dir: Path) -> Summary:
    (
        baseline_entries,
        footer_lines,
        layer_names,
        baseline_candidate,
        board_data,
        fixed_targets,
        initial_source,
    ) = prepare_fixed_baseline(run_dir, args)
    fixed_nets = set(fixed_targets)
    env = build_env(
        0,
        run_dir,
        board_data,
        baseline_entries,
        footer_lines,
        layer_names,
        baseline_candidate.stats,
        args,
        fixed_targets,
    )
    state, _ = env.reset(0)
    require_valid_actions(env.get_action_mask(), "initial 135 Windows fixed-net validation")
    q_values = np.zeros(env.net_count * env.n_ops, dtype=np.float64)
    action_counts = np.zeros_like(q_values)
    eval_used = 0
    env_steps = 0
    episode_counter = 1
    best_partial = baseline_candidate
    best_full = baseline_candidate if baseline_candidate.stats.routed_nets == baseline_candidate.stats.total_nets else None
    best_wire = best_full
    first_full_eval: int | None = 0 if best_full is not None else None
    early_stop_eval = 0 if completion_target_reached(best_partial, args) else None
    seed_limit = seed_swap_limit(args)
    seed_rng = random.Random(args.seed + 13579)
    seed_evals = 0
    for candidate in seed_swap_candidates(
        run_dir,
        baseline_entries,
        footer_lines,
        layer_names,
        seed_limit,
        seed_rng,
        args.seed_swap_mode,
        fixed_nets,
    ):
        if eval_used >= args.eval_budget or early_stop_eval is not None:
            break
        seed_evals += 1
        eval_used += 1
        if eval135.partial_key(candidate) > eval135.partial_key(best_partial):
            best_partial = candidate
        if candidate.stats.routed_nets == candidate.stats.total_nets:
            if first_full_eval is None:
                first_full_eval = eval_used
            if best_full is None or eval135.full_key(candidate) > eval135.full_key(best_full):
                best_full = candidate
            if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                best_wire = candidate
            if early_stop_eval is None and completion_target_reached(best_partial, args):
                early_stop_eval = eval_used

    while eval_used < args.eval_budget and early_stop_eval is None:
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
                if eval135.partial_key(candidate) > eval135.partial_key(best_partial):
                    best_partial = candidate
                if candidate.stats.routed_nets == candidate.stats.total_nets:
                    if first_full_eval is None:
                        first_full_eval = eval_used
                    if best_full is None or eval135.full_key(candidate) > eval135.full_key(best_full):
                        best_full = candidate
                    if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                        best_wire = candidate
                    if early_stop_eval is None and completion_target_reached(best_partial, args):
                        early_stop_eval = eval_used
        if done:
            state, _ = env.reset(episode_counter)
            episode_counter += 1

    eval135.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval135.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval135.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")
    summary = Summary(
        tag=args.tag,
        variant="entry_layer_order_bandit_fallback",
        seed=args.seed,
        eval_budget=args.eval_budget,
        eval_used=eval_used,
        stop_completion_rate=args.stop_completion_rate,
        early_stop_reached=early_stop_eval is not None,
        early_stop_eval=early_stop_eval,
        seed_swap_fraction=args.seed_swap_fraction,
        seed_swap_evals=seed_evals,
        seed_swap_mode=args.seed_swap_mode,
        env_steps=env_steps,
        n_envs=1,
        max_episode_steps=args.max_episode_steps,
        reached_full=best_full is not None,
        first_full_eval=first_full_eval,
        best_partial=eval135.candidate_to_json(best_partial),
        best_full=eval135.candidate_to_json(best_full),
        best_wire_full=eval135.candidate_to_json(best_wire),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_net_count=len(fixed_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
    )
    write_summary(run_dir, summary)
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, baseline_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    return summary


def train(args: argparse.Namespace) -> Summary:
    validate_stop_completion_rate(args)
    random.seed(args.seed)
    np.random.seed(args.seed)
    if torch is not None:
        torch.manual_seed(args.seed)

    run_dir = args.output_root / f"{args.tag}_entry_layer_order_seed{args.seed}_budget{args.eval_budget}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if torch is None:
        print("PyTorch is not available; using numpy bandit fallback search.")
        return train_bandit(args, run_dir)

    (
        baseline_entries,
        footer_lines,
        layer_names,
        baseline_candidate,
        board_data,
        fixed_targets,
        initial_source,
    ) = prepare_fixed_baseline(run_dir, args)
    baseline_stats = baseline_candidate.stats
    fixed_nets = set(fixed_targets)

    probe_env = build_env(0, run_dir, board_data, baseline_entries, footer_lines, layer_names, baseline_stats, args, fixed_targets)
    require_valid_actions(probe_env.get_action_mask(), "initial 135 Windows fixed-net validation")
    feature_dim = probe_env.feature_dim
    net_count = probe_env.net_count
    n_ops = probe_env.n_ops

    device = resolve_device(args.device)
    q_net = QNetwork(feature_dim, net_count, n_ops, args.hidden_dim).to(device)
    target_net = QNetwork(feature_dim, net_count, n_ops, args.hidden_dim).to(device)
    target_net.load_state_dict(q_net.state_dict())
    target_net.eval()
    optimizer = torch.optim.Adam(q_net.parameters(), lr=args.lr)
    replay = ReplayBuffer(args.replay_size)

    envs = [
        build_env(i, run_dir, board_data, baseline_entries, footer_lines, layer_names, baseline_stats, args, fixed_targets)
        for i in range(args.n_envs)
    ]

    states = []
    for env in envs:
        obs, _ = env.reset(0)
        require_valid_actions(env.get_action_mask(), "initial 135 Windows fixed-net validation")
        states.append(obs)

    eval_used = 0
    env_steps = 0
    episode_counter = args.n_envs
    train_steps = 0
    best_partial = baseline_candidate
    best_full = baseline_candidate if baseline_stats.routed_nets == baseline_stats.total_nets else None
    best_wire = best_full
    first_full_eval: int | None = 0 if best_full is not None else None
    early_stop_eval = 0 if completion_target_reached(best_partial, args) else None
    seed_limit = seed_swap_limit(args)
    seed_rng = random.Random(args.seed + 13579)
    seed_evals = 0
    for candidate in seed_swap_candidates(
        run_dir,
        baseline_entries,
        footer_lines,
        layer_names,
        seed_limit,
        seed_rng,
        args.seed_swap_mode,
        fixed_nets,
    ):
        if eval_used >= args.eval_budget or early_stop_eval is not None:
            break
        seed_evals += 1
        eval_used += 1
        if eval135.partial_key(candidate) > eval135.partial_key(best_partial):
            best_partial = candidate
        if candidate.stats.routed_nets == candidate.stats.total_nets:
            if first_full_eval is None:
                first_full_eval = eval_used
            if best_full is None or eval135.full_key(candidate) > eval135.full_key(best_full):
                best_full = candidate
            if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                best_wire = candidate
            if early_stop_eval is None and completion_target_reached(best_partial, args):
                early_stop_eval = eval_used

    while eval_used < args.eval_budget and early_stop_eval is None:
        epsilon = args.eps_end + (args.eps_start - args.eps_end) * max(
            0.0, (args.eval_budget - eval_used) / max(1, args.eval_budget)
        )
        state_tensor = torch.tensor(np.stack(states), dtype=torch.float32, device=device)
        with torch.no_grad():
            q_values = q_net(state_tensor).cpu().numpy()

        next_states = []
        for i, env in enumerate(envs):
            if eval_used >= args.eval_budget or early_stop_eval is not None:
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
                    if eval135.partial_key(candidate) > eval135.partial_key(best_partial):
                        best_partial = candidate
                    if candidate.stats.routed_nets == candidate.stats.total_nets:
                        if first_full_eval is None:
                            first_full_eval = eval_used
                        if best_full is None or eval135.full_key(candidate) > eval135.full_key(best_full):
                            best_full = candidate
                        if best_wire is None or candidate.stats.total_wire_length < best_wire.stats.total_wire_length:
                            best_wire = candidate
                        if early_stop_eval is None and completion_target_reached(best_partial, args):
                            early_stop_eval = eval_used

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

    eval135.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval135.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval135.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = Summary(
        tag=args.tag,
        variant="entry_layer_order_dqn",
        seed=args.seed,
        eval_budget=args.eval_budget,
        eval_used=eval_used,
        stop_completion_rate=args.stop_completion_rate,
        early_stop_reached=early_stop_eval is not None,
        early_stop_eval=early_stop_eval,
        seed_swap_fraction=args.seed_swap_fraction,
        seed_swap_evals=seed_evals,
        seed_swap_mode=args.seed_swap_mode,
        env_steps=env_steps,
        n_envs=args.n_envs,
        max_episode_steps=args.max_episode_steps,
        reached_full=best_full is not None,
        first_full_eval=first_full_eval,
        best_partial=eval135.candidate_to_json(best_partial),
        best_full=eval135.candidate_to_json(best_full),
        best_wire_full=eval135.candidate_to_json(best_wire),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_net_count=len(fixed_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
    )
    write_summary(run_dir, summary)
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, baseline_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    return summary


if __name__ == "__main__":
    train(parse_args())
