#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from explain_135 import generate_explanation
import rl_135_core as core
import routing_eval_135 as eval135
from missing_focus_env_135 import MissingFocusRoutingEnv
from routing_env_135 import (
    CandidateRecord,
    RoutingLocalActionEnv,
    candidate_to_json,
    partial_key,
    save_artifacts,
)


@dataclass
class Summary:
    tag: str
    variant: str
    seed: int
    eval_budget: int
    eval_used: int
    env_steps: int
    n_envs: int
    max_episode_steps: int
    reached_full: bool
    first_full_eval: int | None
    best_partial: dict[str, Any]
    best_full: dict[str, Any] | None
    best_wire_full: dict[str, Any] | None
    best_via_full: dict[str, Any] | None
    fixed_csv: str | None
    fixed_net_count: int
    initial_order: str | None
    initial_source: str
    primary_best_kind: str
    primary_best_order_txt: str | None


class QNetwork(nn.Module):
    def __init__(self, feature_dim: int, net_count: int, hidden_dim: int = 256, n_ops: int = 4) -> None:
        super().__init__()
        self.n_actions = net_count * n_ops
        in_dim = feature_dim * net_count
        self.encoder = nn.Sequential(
            nn.Linear(in_dim, hidden_dim),
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
            np.array(action, dtype=np.int64),
            np.array(reward, dtype=np.float32),
            np.stack(next_obs),
            np.stack(next_mask),
            np.array(done, dtype=np.float32),
        )

    def __len__(self) -> int:
        return len(self.buffer)


def resolve_device(name: str) -> torch.device:
    if name == 'auto':
        return torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    return torch.device(name)


def masked_argmax(q_values: np.ndarray, mask: np.ndarray) -> int:
    masked = np.where(mask > 0, q_values, -1e18)
    return int(masked.argmax())


def random_valid_action(mask: np.ndarray) -> int:
    require_valid_actions(mask)
    valid = np.flatnonzero(mask > 0)
    return int(np.random.choice(valid))


def require_valid_actions(mask: np.ndarray, context: str = "action selection") -> None:
    if not np.any(mask > 0):
        raise RuntimeError(
            f"No legal mutable actions remain during {context}. "
            "Reduce --fixed-csv coverage or leave at least one mutable net."
        )


def load_fixed_nets(csv_path: Path | None, baseline_entries: list[core.OrderEntry]) -> dict[str, tuple[str, int]]:
    if csv_path is None:
        return {}

    baseline_nets = {entry.net for entry in baseline_entries}
    valid_layers = {entry.layer for entry in baseline_entries}
    required_fields = {"net", "layer", "order"}
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
            if net in fixed_targets:
                raise ValueError(f"{csv_path}:{line_no} duplicates fixed net {net}")
            if net not in baseline_nets:
                raise ValueError(f"{csv_path}:{line_no} references unknown net {net}")
            if layer not in valid_layers:
                raise ValueError(
                    f"{csv_path}:{line_no} uses unknown layer {layer!r}; "
                    f"expected one of {', '.join(sorted(valid_layers))}"
                )
            try:
                order = int(order_raw)
            except ValueError as exc:
                raise ValueError(f"{csv_path}:{line_no} has non-integer order {order_raw!r}") from exc
            if order <= 0:
                raise ValueError(f"{csv_path}:{line_no} order must be positive, got {order}")
            slot = (layer, order)
            if slot in fixed_slots:
                raise ValueError(
                    f"{csv_path}:{line_no} conflicts with fixed net {fixed_slots[slot]} "
                    f"at {layer},{order}"
                )
            fixed_targets[net] = slot
            fixed_slots[slot] = net
    return fixed_targets


def load_initial_entries(order_path: Path | None, baseline_entries: list[core.OrderEntry]) -> list[core.OrderEntry]:
    if order_path is None:
        return [core.OrderEntry(e.net, e.layer, e.order) for e in baseline_entries]

    entries, _footer_lines = core.read_order_entries(order_path.expanduser())
    if not entries:
        raise ValueError(f"{order_path} does not contain any order rows")

    baseline_nets = {entry.net for entry in baseline_entries}
    baseline_index = {entry.net: index for index, entry in enumerate(baseline_entries)}
    valid_layers = {entry.layer for entry in baseline_entries}
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
                f"expected one of {', '.join(sorted(valid_layers))}"
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

    normalized: list[core.OrderEntry] = []
    layer_order = [layer for layer in ("TOP", "BOTTOM") if layer in valid_layers]
    layer_order.extend(sorted(valid_layers - set(layer_order)))
    for layer in layer_order:
        layer_entries = [entry for entry in entries if entry.layer == layer]
        layer_entries.sort(key=lambda item: (item.order, baseline_index[item.net]))
        for order, entry in enumerate(layer_entries, start=1):
            normalized.append(core.OrderEntry(entry.net, layer, order))
    return normalized


def prepare_initial_state(
    run_dir: Path,
    baseline_entries: list[core.OrderEntry],
    footer_lines: list[str],
    baseline_candidate: CandidateRecord,
    args: argparse.Namespace,
) -> tuple[list[core.OrderEntry], core.BoardData, CandidateRecord, dict[str, tuple[str, int]], str]:
    source_entries = load_initial_entries(args.initial_order, baseline_entries)
    source_candidate = baseline_candidate
    initial_source = "baseline"
    if args.initial_order is not None:
        initial_source = "initial_order"
        if eval135.signature(source_entries) != eval135.signature(baseline_entries):
            evaluated = eval135.evaluate_entries(
                run_dir,
                footer_lines,
                source_entries,
                "initial_order",
                f"initial_order:{args.initial_order}",
            )
            if evaluated is None:
                raise RuntimeError("Initial 135 order evaluation failed")
            source_candidate = evaluated

    source_board_data = core.load_board_data(
        core.PROJECT_DIR,
        order_entries=source_entries,
        footer_lines=footer_lines,
    )
    fixed_targets = load_fixed_nets(args.fixed_csv, source_entries)
    if fixed_targets:
        initial_source = f"{initial_source}+fixed_csv"
        print(f"Loaded {len(fixed_targets)} fixed 135 nets from {args.fixed_csv}")

    setup_env = make_fixed_initial_env(
        run_dir,
        source_board_data,
        source_entries,
        footer_lines,
        source_candidate.stats,
        args,
        fixed_targets,
    )
    initial_entries = [core.OrderEntry(e.net, e.layer, e.order) for e in setup_env.entries]
    initial_candidate = source_candidate
    if eval135.signature(initial_entries) != eval135.signature(source_entries):
        evaluated = eval135.evaluate_entries(
            run_dir,
            footer_lines,
            initial_entries,
            "fixed_initial",
            "fixed_csv_initial",
        )
        if evaluated is None:
            raise RuntimeError("Fixed CSV initial 135 routing evaluation failed")
        initial_candidate = evaluated

    initial_board_data = core.load_board_data(
        core.PROJECT_DIR,
        order_entries=initial_entries,
        footer_lines=footer_lines,
    )
    return initial_entries, initial_board_data, initial_candidate, fixed_targets, initial_source


def choose_primary_best(
    best_full: CandidateRecord | None,
    best_partial: CandidateRecord,
) -> tuple[str, CandidateRecord]:
    if best_full is not None:
        return "best_full", best_full
    return "best_partial", best_partial


def export_order_txt(candidate: CandidateRecord | None, dest: Path) -> str | None:
    if candidate is None or candidate.run_dir is None:
        return None
    src = Path(candidate.run_dir) / core.BASE_ORDER
    if not src.exists():
        return None
    shutil.copy2(src, dest)
    return dest.name


def make_fixed_initial_env(
    output_root: Path,
    board_data: core.BoardData,
    baseline_entries: list[core.OrderEntry],
    footer_lines: list[str],
    baseline_stats: core.RouteStats,
    args: argparse.Namespace,
    fixed_targets: dict[str, tuple[str, int]],
) -> RoutingLocalActionEnv:
    return RoutingLocalActionEnv(
        env_id=0,
        output_root=output_root,
        board_data=board_data,
        baseline_entries=baseline_entries,
        footer_lines=footer_lines,
        baseline_stats=baseline_stats,
        max_episode_steps=args.max_episode_steps,
        missing_weight=args.missing_weight,
        via_weight=args.via_weight,
        wire_weight=args.wire_weight,
        full_bonus=args.full_bonus,
        failure_penalty=args.failure_penalty,
        fixed_targets=fixed_targets,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Missing-focus Double DQN on 135 routing.')
    p.add_argument('--eval-budget', type=int, default=500)
    p.add_argument('--n-envs', type=int, default=4)
    p.add_argument('--max-episode-steps', type=int, default=12)
    p.add_argument('--hidden-dim', type=int, default=256)
    p.add_argument('--lr', type=float, default=5e-4)
    p.add_argument('--gamma', type=float, default=0.97)
    p.add_argument('--batch-size', type=int, default=64)
    p.add_argument('--replay-size', type=int, default=4000)
    p.add_argument('--warmup', type=int, default=80)
    p.add_argument('--train-every', type=int, default=4)
    p.add_argument('--target-update', type=int, default=50)
    p.add_argument('--eps-start', type=float, default=0.9)
    p.add_argument('--eps-end', type=float, default=0.05)
    p.add_argument('--prune-strength', type=float, default=0.7)
    p.add_argument('--neighbor-k', type=int, default=40)
    p.add_argument('--delta-order-threshold', type=float, default=2.0)
    p.add_argument('--missing-weight', type=float, default=1000.0)
    p.add_argument('--via-weight', type=float, default=8.0)
    p.add_argument('--wire-weight', type=float, default=0.04)
    p.add_argument('--full-bonus', type=float, default=120.0)
    p.add_argument('--failure-penalty', type=float, default=40.0)
    p.add_argument('--seed', type=int, default=20260422)
    p.add_argument('--device', default='auto')
    p.add_argument('--tag', default='dqn_pruned_variant')
    p.add_argument('--output-root', type=Path, default=Path(__file__).resolve().parent / 'search_runs')
    p.add_argument('--no-explain', action='store_true', help='Skip deterministic explanation.md generation.')
    p.add_argument(
        '--fixed-csv',
        type=Path,
        default=None,
        help='Optional CSV with net,layer,order target rows to keep fixed during search.',
    )
    p.add_argument(
        '--initial-order',
        type=Path,
        default=None,
        help='Optional complete order_out.txt-style file to use as the RL initial layer/order state.',
    )
    return p.parse_args()



def train(args: argparse.Namespace) -> Summary:
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    run_dir = args.output_root / f'{args.tag}_missing_focus_seed{args.seed}_budget{args.eval_budget}'
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    baseline_entries, footer_lines, baseline_candidate, board_data = eval135.prepare_baseline()
    del board_data
    initial_entries, initial_board_data, initial_candidate, fixed_targets, initial_source = prepare_initial_state(
        run_dir,
        baseline_entries,
        footer_lines,
        baseline_candidate,
        args,
    )
    initial_stats = initial_candidate.stats
    feature_dim = 15
    device = resolve_device(args.device)
    q_net = QNetwork(feature_dim=feature_dim, net_count=len(baseline_entries), hidden_dim=args.hidden_dim).to(device)
    target_net = QNetwork(feature_dim=feature_dim, net_count=len(baseline_entries), hidden_dim=args.hidden_dim).to(device)
    target_net.load_state_dict(q_net.state_dict())
    target_net.eval()
    optimizer = torch.optim.Adam(q_net.parameters(), lr=args.lr)
    replay = ReplayBuffer(args.replay_size)

    env_root = run_dir / 'episodes'
    env_root.mkdir(parents=True, exist_ok=True)
    envs = [
        MissingFocusRoutingEnv(
            env_id=i,
            output_root=env_root,
            board_data=initial_board_data,
            baseline_entries=initial_entries,
            footer_lines=footer_lines,
            baseline_stats=initial_stats,
            max_episode_steps=args.max_episode_steps,
            missing_weight=args.missing_weight,
            via_weight=args.via_weight,
            wire_weight=args.wire_weight,
            full_bonus=args.full_bonus,
            failure_penalty=args.failure_penalty,
            neighbor_k=args.neighbor_k,
            delta_order_threshold=args.delta_order_threshold,
            fixed_targets=fixed_targets,
        )
        for i in range(args.n_envs)
    ]

    states = []
    for env in envs:
        obs, _ = env.reset(0)
        require_valid_actions(env.get_action_mask(), "initial 135 fixed-net validation")
        states.append(obs)

    eval_used = 0
    env_steps = 0
    episode_counter = args.n_envs
    train_steps = 0
    first_full_eval: int | None = None
    best_partial = initial_candidate
    best_full: CandidateRecord | None = None
    best_wire: CandidateRecord | None = None
    best_via: CandidateRecord | None = None

    while eval_used < args.eval_budget:
        progress = eval_used / max(1, args.eval_budget)
        epsilon = args.eps_end + (args.eps_start - args.eps_end) * max(0.0, (args.eval_budget - eval_used) / max(1, args.eval_budget))
        state_tensor = torch.tensor(np.stack(states), dtype=torch.float32, device=device)
        with torch.no_grad():
            q_values = q_net(state_tensor).cpu().numpy()

        next_states = []
        for i, env in enumerate(envs):
            if eval_used >= args.eval_budget:
                next_states.append(states[i])
                continue
            action_mask = env.get_missing_focus_action_mask(epsilon=epsilon, prune_strength=args.prune_strength)
            require_valid_actions(action_mask)
            if random.random() < epsilon:
                action = random_valid_action(action_mask)
            else:
                action = masked_argmax(q_values[i], action_mask)
            next_obs, _next_mask_legal, reward, done, info = env.step(action, eval_used + 1)
            env_steps += 1
            next_action_mask = env.get_missing_focus_action_mask(epsilon=epsilon, prune_strength=args.prune_strength)
            if info.get('evaluated', False):
                eval_used += 1
                cand = info.get('candidate')
                if cand is not None:
                    if partial_key(cand) > partial_key(best_partial):
                        best_partial = cand
                    if cand.stats.routed_nets == cand.stats.total_nets:
                        if first_full_eval is None:
                            first_full_eval = eval_used
                        if best_full is None or (cand.stats.vias, cand.stats.total_wire_length) < (best_full.stats.vias, best_full.stats.total_wire_length):
                            best_full = cand
                        if best_wire is None or (cand.stats.total_wire_length, cand.stats.vias) < (best_wire.stats.total_wire_length, best_wire.stats.vias):
                            best_wire = cand
                        if best_via is None or (cand.stats.vias, cand.stats.total_wire_length) < (best_via.stats.vias, best_via.stats.total_wire_length):
                            best_via = cand
            replay.add(states[i], action_mask, action, reward, next_obs, next_action_mask, done)
            if done:
                next_obs, _ = env.reset(episode_counter)
                episode_counter += 1
            next_states.append(next_obs)
        states = next_states

        if len(replay) >= args.warmup and env_steps % args.train_every == 0:
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
                next_q_target = target_net(next_obs_t).masked_fill(next_mask_t <= 0, -1e9).gather(1, next_actions.unsqueeze(1)).squeeze(1)
                targets = rew_t + args.gamma * next_q_target * (1.0 - done_t)
            loss = F.smooth_l1_loss(q_pred, targets)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(q_net.parameters(), 1.0)
            optimizer.step()
            train_steps += 1
            if train_steps % args.target_update == 0:
                target_net.load_state_dict(q_net.state_dict())

    save_artifacts(best_partial, run_dir / 'best_partial')
    save_artifacts(best_full, run_dir / 'best_full')
    save_artifacts(best_wire, run_dir / 'best_wire_full')
    save_artifacts(best_via, run_dir / 'best_via_full')
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / 'best_layer_order.txt')

    summary = Summary(
        tag=args.tag,
        variant='missing_focus',
        seed=args.seed,
        eval_budget=args.eval_budget,
        eval_used=eval_used,
        env_steps=env_steps,
        n_envs=args.n_envs,
        max_episode_steps=args.max_episode_steps,
        reached_full=best_full is not None,
        first_full_eval=first_full_eval,
        best_partial=candidate_to_json(best_partial),
        best_full=candidate_to_json(best_full),
        best_wire_full=candidate_to_json(best_wire),
        best_via_full=candidate_to_json(best_via),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_net_count=len(fixed_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
    )
    (run_dir / 'summary.json').write_text(json.dumps(vars(summary), indent=2), encoding='utf-8')
    print(json.dumps(vars(summary), indent=2))
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, initial_candidate, primary_best)
        print(f'Explanation written to: {explanation_path}')
    print(f'Variant DQN-pruned run written to: {run_dir}')
    return summary


if __name__ == '__main__':
    train(parse_args())
