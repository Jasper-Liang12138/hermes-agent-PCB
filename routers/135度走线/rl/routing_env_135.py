#!/usr/bin/env python3

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import numpy as np

import routing_eval_135 as eval135
from rl_135_core import BoardData, OrderEntry, RouteStats


OP_EARLIER = 0
OP_LATER = 1
OP_RESTORE = 2
OP_SWAP_LAYER_BASE = 3


class Windows135RoutingEnv:
    def __init__(
        self,
        env_id: int,
        output_root: Path,
        board_data: BoardData,
        baseline_entries: list[OrderEntry],
        footer_lines: list[str],
        layer_names: list[str],
        baseline_stats: RouteStats,
        max_episode_steps: int,
        missing_weight: float,
        wire_weight: float,
        via_weight: float,
        full_bonus: float,
        failure_penalty: float,
        fixed_targets: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        self.env_id = env_id
        self.output_root = output_root
        self.board_data = board_data
        self.baseline_entries = [OrderEntry(p.net, p.layer, p.order) for p in baseline_entries]
        self.footer_lines = list(footer_lines)
        self.layer_names = list(layer_names)
        self.baseline_stats = baseline_stats
        self.max_episode_steps = max_episode_steps
        self.missing_weight = missing_weight
        self.wire_weight = wire_weight
        self.via_weight = via_weight
        self.full_bonus = full_bonus
        self.failure_penalty = failure_penalty
        self.n_ops = OP_SWAP_LAYER_BASE + len(self.layer_names)
        self.net_to_idx = {p.net: i for i, p in enumerate(self.baseline_entries)}
        self.baseline_map = {p.net: (p.layer, p.order) for p in self.baseline_entries}
        self.fixed_targets = dict(fixed_targets or {})
        self.fixed_nets = set(self.fixed_targets)
        unknown_fixed = sorted(self.fixed_nets - set(self.net_to_idx))
        if unknown_fixed:
            raise ValueError(f"Unknown fixed entry nets: {', '.join(unknown_fixed)}")
        self.reset(0)

    @property
    def net_count(self) -> int:
        return len(self.baseline_entries)

    @property
    def feature_dim(self) -> int:
        return int(self.get_observation().shape[1])

    def reset(self, episode_index: int) -> tuple[np.ndarray, np.ndarray]:
        self.episode_index = episode_index
        self.step_count = 0
        self.entries = self.normalize_orders(self.baseline_entries)
        self.stats = self.baseline_stats
        self.missing_nets = []
        return self.get_observation(), self.get_action_mask()

    def cost(self, stats: RouteStats) -> float:
        missing = stats.total_nets - stats.routed_nets
        return self.missing_weight * missing + self.wire_weight * stats.total_wire_length + self.via_weight * stats.vias

    def normalize_orders(self, entries: list[OrderEntry]) -> list[OrderEntry]:
        normalized = [OrderEntry(p.net, p.layer, p.order) for p in entries]
        by_net = {p.net: p for p in normalized}
        baseline_idx = {p.net: i for i, p in enumerate(self.baseline_entries)}
        for net, (target_layer, target_order) in self.fixed_targets.items():
            by_net[net].layer = target_layer
            by_net[net].order = target_order
        for layer in self.layer_names:
            layer_entries = [p for p in normalized if p.layer == layer]
            fixed_slots = {
                target_order
                for _net, (target_layer, target_order) in self.fixed_targets.items()
                if target_layer == layer
            }
            mutable_entries = [p for p in layer_entries if p.net not in self.fixed_nets]
            mutable_entries.sort(key=lambda item: (item.order, baseline_idx[item.net]))
            slot_limit = max([len(layer_entries), *fixed_slots], default=0)
            free_slots = [slot for slot in range(1, slot_limit + 1) if slot not in fixed_slots]
            while len(free_slots) < len(mutable_entries):
                slot_limit += 1
                if slot_limit not in fixed_slots:
                    free_slots.append(slot_limit)
            for order, entry in zip(free_slots, mutable_entries):
                by_net[entry.net].order = order
        return normalized

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

    def layer_entries(self, entries: list[OrderEntry], layer: str) -> list[OrderEntry]:
        return sorted([p for p in entries if p.layer == layer], key=lambda item: (item.order, self.net_to_idx[item.net]))

    def get_observation(self) -> np.ndarray:
        layer_index = {layer: i for i, layer in enumerate(self.layer_names)}
        max_order = max(1, max((p.order for p in self.entries), default=1))
        rows: list[list[float]] = []
        for i, entry in enumerate(self.entries):
            curr_layer = [0.0] * len(self.layer_names)
            curr_layer[layer_index[entry.layer]] = 1.0
            base_layer, base_order = self.baseline_map[entry.net]
            base_layer_idx = layer_index[base_layer]
            layer_changed = 1.0 if entry.layer != base_layer else 0.0
            order_delta = (entry.order - base_order) / max_order
            rows.append(
                curr_layer
                + [
                    entry.order / max_order,
                    layer_index[entry.layer] / max(1, len(self.layer_names) - 1),
                    base_layer_idx / max(1, len(self.layer_names) - 1),
                    layer_changed,
                    order_delta,
                ]
                + self.board_data.observation[i].tolist()
            )
        return np.asarray(rows, dtype=np.float32)

    def get_action_mask(self) -> np.ndarray:
        mask = np.zeros((self.net_count, self.n_ops), dtype=np.float32)
        for i, entry in enumerate(self.entries):
            if entry.net in self.fixed_nets:
                continue
            same_layer = self.layer_entries(self.entries, entry.layer)
            position = next((idx for idx, item in enumerate(same_layer) if item.net == entry.net), None)
            base_layer, base_order = self.baseline_map[entry.net]
            if position is not None and position > 0 and same_layer[position - 1].net not in self.fixed_nets:
                mask[i, OP_EARLIER] = 1.0
            if (
                position is not None
                and position < len(same_layer) - 1
                and same_layer[position + 1].net not in self.fixed_nets
            ):
                mask[i, OP_LATER] = 1.0
            if entry.layer != base_layer or entry.order != base_order:
                occupant = [
                    p for p in self.entries
                    if p.net != entry.net and p.layer == base_layer and p.order == base_order
                ]
                if not occupant or occupant[0].net not in self.fixed_nets:
                    mask[i, OP_RESTORE] = 1.0
            for layer_offset, target_layer in enumerate(self.layer_names):
                if target_layer == entry.layer:
                    continue
                target_same_slot = [p for p in self.entries if p.layer == target_layer and p.order == entry.order]
                if len(target_same_slot) == 1 and target_same_slot[0].net not in self.fixed_nets:
                    mask[i, OP_SWAP_LAYER_BASE + layer_offset] = 1.0
        return mask.reshape(-1)

    def focus_action_mask(self, epsilon: float, prune_strength: float) -> np.ndarray:
        legal = self.get_action_mask().reshape(self.net_count, self.n_ops)
        if random.random() > prune_strength:
            return legal.reshape(-1)
        focus: set[int] = set()
        for net in self.missing_nets:
            if net in self.net_to_idx:
                idx = self.net_to_idx[net]
                focus.add(idx)
                focus.update(range(max(0, idx - 2), min(self.net_count, idx + 3)))
                order = self.entries[idx].order
                for j, entry in enumerate(self.entries):
                    if entry.order == order:
                        focus.add(j)
        if not focus or epsilon > 0.45:
            focus.update(random.sample(range(self.net_count), k=min(12, self.net_count)))
        pruned = np.zeros_like(legal)
        for idx in focus:
            pruned[idx] = legal[idx]
        if pruned.sum() <= 0:
            return legal.reshape(-1)
        return pruned.reshape(-1)

    def apply_action(self, flat_action: int) -> list[OrderEntry]:
        entry_idx = flat_action // self.n_ops
        op = flat_action % self.n_ops
        entries = [OrderEntry(p.net, p.layer, p.order) for p in self.entries]
        entry_by_net = {p.net: p for p in entries}
        target = entries[entry_idx]
        if target.net in self.fixed_nets:
            return [OrderEntry(p.net, p.layer, p.order) for p in self.entries]

        if op == OP_EARLIER:
            same_layer = self.layer_entries(entries, target.layer)
            position = next((idx for idx, item in enumerate(same_layer) if item.net == target.net), None)
            if position is not None and position > 0:
                prev = entry_by_net[same_layer[position - 1].net]
                if prev.net not in self.fixed_nets:
                    target.order, prev.order = prev.order, target.order
        elif op == OP_LATER:
            same_layer = self.layer_entries(entries, target.layer)
            position = next((idx for idx, item in enumerate(same_layer) if item.net == target.net), None)
            if position is not None and position < len(same_layer) - 1:
                nxt = entry_by_net[same_layer[position + 1].net]
                if nxt.net not in self.fixed_nets:
                    target.order, nxt.order = nxt.order, target.order
        elif op == OP_RESTORE:
            base_layer, base_order = self.baseline_map[target.net]
            other = [
                p for p in entries
                if p.net != target.net and p.layer == base_layer and p.order == base_order
            ]
            if other and other[0].net not in self.fixed_nets:
                other_entry = entry_by_net[other[0].net]
                other_entry.layer = target.layer
                other_entry.order = target.order
            target.layer = base_layer
            target.order = base_order
        elif op >= OP_SWAP_LAYER_BASE:
            layer_idx = op - OP_SWAP_LAYER_BASE
            if 0 <= layer_idx < len(self.layer_names):
                target_layer = self.layer_names[layer_idx]
                other = [p for p in entries if p.layer == target_layer and p.order == target.order]
                if target_layer != target.layer and len(other) == 1 and other[0].net not in self.fixed_nets:
                    other_entry = entry_by_net[other[0].net]
                    other_entry.layer, target.layer = target.layer, other_entry.layer
        normalized = self.normalize_orders(entries)
        if not self.fixed_constraints_preserved(normalized):
            raise RuntimeError("Internal error: fixed entry constraints were modified by an action")
        return normalized

    def evaluate_entries(self, entries: list[OrderEntry], run_name: str, source: str) -> eval135.RouteCandidate | None:
        return eval135.evaluate_entries(self.output_root, self.footer_lines, self.layer_names, entries, run_name, source)

    def step(self, flat_action: int, global_eval_index: int) -> tuple[np.ndarray, np.ndarray, float, bool, dict[str, Any]]:
        self.step_count += 1
        prev_cost = self.cost(self.stats)
        new_entries = self.apply_action(flat_action)
        if eval135.signature(new_entries) == eval135.signature(self.entries):
            done = self.step_count >= self.max_episode_steps
            return self.get_observation(), self.get_action_mask(), -0.2, done, {"evaluated": False, "candidate": None}

        run_name = f"env{self.env_id:02d}_{global_eval_index:05d}_s{self.step_count:03d}"
        candidate = self.evaluate_entries(new_entries, run_name, f"entry_action:{flat_action % self.n_ops}")
        if candidate is None:
            done = self.step_count >= self.max_episode_steps
            return self.get_observation(), self.get_action_mask(), -self.failure_penalty, done, {
                "evaluated": True,
                "candidate": None,
            }

        new_cost = self.cost(candidate.stats)
        reward = prev_cost - new_cost
        if self.stats.routed_nets < self.stats.total_nets and candidate.stats.routed_nets == candidate.stats.total_nets:
            reward += self.full_bonus

        self.entries = [OrderEntry(p.net, p.layer, p.order) for p in new_entries]
        self.stats = candidate.stats
        self.missing_nets = list(candidate.missing_nets)
        done = self.step_count >= self.max_episode_steps
        return self.get_observation(), self.get_action_mask(), reward, done, {
            "evaluated": True,
            "candidate": candidate,
        }
