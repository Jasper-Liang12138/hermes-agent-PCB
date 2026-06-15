#!/usr/bin/env python3

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

import routing_eval_135 as eval135
from rl_135_core import BoardData, OrderEntry, RouteStats


N_OPS = 4
OP_FLIP = 0
OP_EARLIER = 1
OP_LATER = 2
OP_RESTORE = 3
OP_NAMES = {
    OP_FLIP: "flip",
    OP_EARLIER: "earlier",
    OP_LATER: "later",
    OP_RESTORE: "restore",
}

CandidateRecord = eval135.RouteCandidate


def candidate_to_json(candidate: CandidateRecord | None) -> dict[str, Any] | None:
    return eval135.candidate_to_json(candidate)


def partial_key(candidate: CandidateRecord) -> tuple[float, int, float]:
    return (candidate.stats.completion_rate, -candidate.stats.vias, -candidate.stats.total_wire_length)


def save_artifacts(candidate: CandidateRecord | None, dest: Path) -> None:
    if candidate is None or candidate.run_dir is None:
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    source_dir = Path(candidate.run_dir)
    keep_files = [
        "order_out.txt",
        "line.out",
        "output.txt",
        "f.log",
        "turn.log",
        "parameter.txt",
        "402Pin_08BGA_8L_S_01141700.txt",
        "net_list.txt",
        "U22_pins.csv",
    ]
    for name in keep_files:
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)
    eval135.run_turn_if_needed(dest)
    order_path = dest / "order_out.txt"
    if order_path.exists():
        shutil.copy2(order_path, dest / "layer_order.txt")
    (dest / "candidate_meta.json").write_text(json.dumps(candidate_to_json(candidate), indent=2), encoding="utf-8")


class RoutingLocalActionEnv:
    def __init__(
        self,
        env_id: int,
        output_root: Path,
        board_data: BoardData,
        baseline_entries: list[OrderEntry],
        footer_lines: list[str],
        baseline_stats: RouteStats,
        max_episode_steps: int,
        missing_weight: float,
        via_weight: float,
        wire_weight: float,
        full_bonus: float,
        failure_penalty: float,
        fixed_targets: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        self.env_id = env_id
        self.output_root = output_root
        self.board_data = board_data
        self.baseline_entries = [OrderEntry(e.net, e.layer, e.order) for e in baseline_entries]
        self.footer_lines = list(footer_lines)
        self.baseline_stats = baseline_stats
        self.max_episode_steps = max_episode_steps
        self.missing_weight = missing_weight
        self.via_weight = via_weight
        self.wire_weight = wire_weight
        self.full_bonus = full_bonus
        self.failure_penalty = failure_penalty
        self.net_names = [e.net for e in self.baseline_entries]
        self.net_to_idx = {e.net: i for i, e in enumerate(self.baseline_entries)}
        self.baseline_map = {e.net: (e.layer, e.order) for e in self.baseline_entries}
        self.fixed_targets = dict(fixed_targets or {})
        self.fixed_nets = set(self.fixed_targets)
        unknown_fixed = sorted(self.fixed_nets - set(self.net_names))
        if unknown_fixed:
            raise ValueError(f"Unknown fixed nets: {', '.join(unknown_fixed)}")
        self.static_tail = self.board_data.observation[:, 3:]
        self.reset(0)

    def reset(self, episode_index: int) -> tuple[np.ndarray, np.ndarray]:
        self.episode_index = episode_index
        self.step_count = 0
        self.entries = self.canonicalize(self.baseline_entries)
        self.stats = self.baseline_stats
        self.missing_nets = self.compute_missing(self.entries, self.stats)
        return self.get_observation(), self.get_action_mask()

    def cost(self, stats: RouteStats) -> float:
        missing = stats.total_nets - stats.routed_nets
        return (
            self.missing_weight * missing
            + self.via_weight * stats.vias
            + self.wire_weight * stats.total_wire_length
        )

    def compute_missing(self, entries: list[OrderEntry], stats: RouteStats) -> list[str]:
        if stats.routed_nets >= stats.total_nets:
            return []
        return []

    def get_observation(self) -> np.ndarray:
        top = np.array([1.0 if e.layer == "TOP" else 0.0 for e in self.entries], dtype=np.float32)
        bottom = 1.0 - top
        top_count = max(1, int(top.sum()))
        bottom_count = max(1, len(self.entries) - int(top.sum()))
        curr_order = np.zeros(len(self.entries), dtype=np.float32)
        delta_order = np.zeros(len(self.entries), dtype=np.float32)
        layer_changed = np.zeros(len(self.entries), dtype=np.float32)
        for i, e in enumerate(self.entries):
            denom = top_count if e.layer == "TOP" else bottom_count
            curr_order[i] = e.order / max(1, denom)
            base_layer, base_order = self.baseline_map[e.net]
            base_denom = self.board_data.top_count if base_layer == "TOP" else self.board_data.bottom_count
            delta_order[i] = curr_order[i] - (base_order / max(1, base_denom))
            layer_changed[i] = 1.0 if e.layer != base_layer else 0.0
        obs = np.concatenate(
            [
                top[:, None],
                bottom[:, None],
                curr_order[:, None],
                self.board_data.observation[:, :3],
                self.static_tail,
                layer_changed[:, None],
                delta_order[:, None],
            ],
            axis=1,
        )
        return obs.astype(np.float32)

    def get_action_mask(self) -> np.ndarray:
        mask = np.zeros((len(self.entries), N_OPS), dtype=np.float32)
        for i, e in enumerate(self.entries):
            if e.net in self.fixed_nets:
                continue
            mutable_layer_entries = [
                x for x in self.entries
                if x.layer == e.layer and x.net not in self.fixed_nets
            ]
            mutable_layer_entries.sort(key=lambda item: (item.order, self.net_to_idx[item.net]))
            mutable_rank = next(
                (idx for idx, item in enumerate(mutable_layer_entries) if item.net == e.net),
                None,
            )
            base_layer, base_order = self.baseline_map[e.net]
            mask[i, OP_FLIP] = 1.0
            mask[i, OP_EARLIER] = 1.0 if mutable_rank is not None and mutable_rank > 0 else 0.0
            mask[i, OP_LATER] = (
                1.0
                if mutable_rank is not None and mutable_rank < len(mutable_layer_entries) - 1
                else 0.0
            )
            mask[i, OP_RESTORE] = 1.0 if (e.layer != base_layer or e.order != base_order) else 0.0
        return mask.reshape(-1)

    def canonicalize(self, entries: list[OrderEntry]) -> list[OrderEntry]:
        by_net = {e.net: OrderEntry(e.net, e.layer, e.order) for e in entries}
        for net, (target_layer, target_order) in self.fixed_targets.items():
            by_net[net] = OrderEntry(net, target_layer, target_order)

        ordered: list[OrderEntry] = []
        for layer in ("TOP", "BOTTOM"):
            layer_entries = [e for e in by_net.values() if e.layer == layer]
            fixed_slots = {
                target_order
                for _net, (target_layer, target_order) in self.fixed_targets.items()
                if target_layer == layer
            }
            mutable_entries = [e for e in layer_entries if e.net not in self.fixed_nets]
            mutable_entries.sort(key=lambda item: (item.order, self.net_to_idx[item.net]))
            slot_limit = max([len(layer_entries), *fixed_slots], default=0)
            free_slots = [slot for slot in range(1, slot_limit + 1) if slot not in fixed_slots]
            while len(free_slots) < len(mutable_entries):
                slot_limit += 1
                if slot_limit not in fixed_slots:
                    free_slots.append(slot_limit)

            for entry in layer_entries:
                if entry.net in self.fixed_nets:
                    target_layer, target_order = self.fixed_targets[entry.net]
                    ordered.append(OrderEntry(entry.net, target_layer, target_order))
            for entry, slot in zip(mutable_entries, free_slots):
                ordered.append(OrderEntry(entry.net, layer, slot))
        ordered.sort(key=lambda item: (0 if item.layer == "TOP" else 1, item.order, self.net_to_idx[item.net]))
        return ordered

    def fixed_constraints_preserved(self, entries: list[OrderEntry]) -> bool:
        by_net = {entry.net: entry for entry in entries}
        for net in self.fixed_nets:
            entry = by_net.get(net)
            if entry is None:
                return False
            target_layer, target_order = self.fixed_targets[net]
            if entry.layer != target_layer or entry.order != target_order:
                return False
        return True

    def mutable_layer_entries(self, entries: list[OrderEntry], layer: str) -> list[OrderEntry]:
        layer_entries = [e for e in entries if e.layer == layer and e.net not in self.fixed_nets]
        layer_entries.sort(key=lambda item: (item.order, self.net_to_idx[item.net]))
        return layer_entries

    def apply_action(self, flat_action: int) -> list[OrderEntry]:
        net_idx = flat_action // N_OPS
        op = flat_action % N_OPS
        entries = [OrderEntry(e.net, e.layer, e.order) for e in self.entries]
        entry_by_net = {e.net: e for e in entries}
        target = entries[net_idx]
        if target.net in self.fixed_nets:
            return [OrderEntry(e.net, e.layer, e.order) for e in self.entries]
        if op == OP_FLIP:
            target.layer = "BOTTOM" if target.layer == "TOP" else "TOP"
            if target.layer == "TOP":
                top_count = sum(1 for e in entries if e.layer == "TOP")
                target.order = min(max(1, target.order), top_count)
            else:
                bottom_count = sum(1 for e in entries if e.layer == "BOTTOM")
                target.order = min(max(1, target.order), bottom_count)
        elif op == OP_EARLIER:
            mutable_entries = self.mutable_layer_entries(entries, target.layer)
            position = next((idx for idx, item in enumerate(mutable_entries) if item.net == target.net), None)
            if position is not None and position > 0:
                previous = entry_by_net[mutable_entries[position - 1].net]
                target.order, previous.order = previous.order, target.order
        elif op == OP_LATER:
            mutable_entries = self.mutable_layer_entries(entries, target.layer)
            position = next((idx for idx, item in enumerate(mutable_entries) if item.net == target.net), None)
            if position is not None and position < len(mutable_entries) - 1:
                next_entry = entry_by_net[mutable_entries[position + 1].net]
                target.order, next_entry.order = next_entry.order, target.order
        elif op == OP_RESTORE:
            base_layer, base_order = self.baseline_map[target.net]
            target.layer = base_layer
            target.order = base_order
        canonical = self.canonicalize(entries)
        if not self.fixed_constraints_preserved(canonical):
            raise RuntimeError("Internal error: fixed net constraints were modified by an action")
        return canonical

    def evaluate_entries(self, entries: list[OrderEntry], run_name: str, source: str) -> CandidateRecord | None:
        return eval135.evaluate_entries(self.output_root, self.footer_lines, entries, run_name, source)

    def step(self, flat_action: int, global_eval_index: int) -> tuple[np.ndarray, np.ndarray, float, bool, dict[str, Any]]:
        self.step_count += 1
        prev_cost = self.cost(self.stats)
        new_entries = self.apply_action(flat_action)
        if eval135.signature(new_entries) == eval135.signature(self.entries):
            reward = -0.2
            done = self.step_count >= self.max_episode_steps
            return self.get_observation(), self.get_action_mask(), reward, done, {
                "evaluated": False,
                "success": True,
                "candidate": None,
            }
        run_name = f"env{self.env_id:02d}_{global_eval_index:05d}_s{self.step_count:03d}"
        candidate = self.evaluate_entries(new_entries, run_name, f"a2c:{OP_NAMES[flat_action % N_OPS]}")
        if candidate is None:
            reward = -self.failure_penalty
            done = self.step_count >= self.max_episode_steps
            return self.get_observation(), self.get_action_mask(), reward, done, {
                "evaluated": True,
                "success": False,
                "candidate": None,
            }
        new_cost = self.cost(candidate.stats)
        reward = prev_cost - new_cost
        if self.stats.routed_nets < self.stats.total_nets and candidate.stats.routed_nets == candidate.stats.total_nets:
            reward += self.full_bonus
        self.entries = [OrderEntry(e.net, e.layer, e.order) for e in new_entries]
        self.stats = candidate.stats
        self.missing_nets = list(candidate.missing_nets)
        done = self.step_count >= self.max_episode_steps
        return self.get_observation(), self.get_action_mask(), reward, done, {
            "evaluated": True,
            "success": True,
            "candidate": candidate,
        }
