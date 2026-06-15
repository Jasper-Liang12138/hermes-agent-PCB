#!/usr/bin/env python3

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import numpy as np

import routing_eval_arc as eval_arc
from rl_arc_core import BoardData, PairEntry, RouteStats


OP_EARLIER = 0
OP_LATER = 1
OP_RESTORE = 2
OP_SWAP_LAYER_BASE = 3


class ArcPairRoutingEnv:
    def __init__(
        self,
        env_id: int,
        output_root: Path,
        board_data: BoardData,
        baseline_pairs: list[PairEntry],
        footer_lines: list[str],
        layer_names: list[str],
        baseline_stats: RouteStats,
        max_episode_steps: int,
        missing_weight: float,
        wire_weight: float,
        via_weight: float,
        full_bonus: float,
        failure_penalty: float,
        fixed_pair_targets: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        self.env_id = env_id
        self.output_root = output_root
        self.board_data = board_data
        self.baseline_pairs = [PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in baseline_pairs]
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
        self.pair_to_idx = {p.key: i for i, p in enumerate(self.baseline_pairs)}
        self.baseline_map = {p.key: (p.layer, p.order) for p in self.baseline_pairs}
        self.fixed_pair_targets = dict(fixed_pair_targets or {})
        self.fixed_pair_keys = set(self.fixed_pair_targets)
        unknown_fixed = sorted(self.fixed_pair_keys - set(self.pair_to_idx))
        if unknown_fixed:
            raise ValueError(f"Unknown fixed pair keys: {', '.join(unknown_fixed)}")
        self.pair_keys_by_net = {}
        for pair in self.baseline_pairs:
            self.pair_keys_by_net[pair.neg_net] = pair.key
            self.pair_keys_by_net[pair.pos_net] = pair.key
        self.reset(0)

    @property
    def pair_count(self) -> int:
        return len(self.baseline_pairs)

    @property
    def feature_dim(self) -> int:
        return int(self.get_observation().shape[1])

    def reset(self, episode_index: int) -> tuple[np.ndarray, np.ndarray]:
        self.episode_index = episode_index
        self.step_count = 0
        self.pairs = self.normalize_orders(self.baseline_pairs)
        self.stats = self.baseline_stats
        self.missing_nets = []
        return self.get_observation(), self.get_action_mask()

    def cost(self, stats: RouteStats) -> float:
        missing = stats.total_nets - stats.routed_nets
        return self.missing_weight * missing + self.wire_weight * stats.total_wire_length + self.via_weight * stats.vias

    def normalize_orders(self, pairs: list[PairEntry]) -> list[PairEntry]:
        normalized = [PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in pairs]
        by_key = {p.key: p for p in normalized}
        baseline_idx = {p.key: i for i, p in enumerate(self.baseline_pairs)}
        for key, (target_layer, target_order) in self.fixed_pair_targets.items():
            by_key[key].layer = target_layer
            by_key[key].order = target_order
        for layer in self.layer_names:
            layer_pairs = [p for p in normalized if p.layer == layer]
            fixed_slots = {
                target_order
                for _key, (target_layer, target_order) in self.fixed_pair_targets.items()
                if target_layer == layer
            }
            mutable_pairs = [p for p in layer_pairs if p.key not in self.fixed_pair_keys]
            mutable_pairs.sort(key=lambda item: (item.order, baseline_idx[item.key]))
            slot_limit = max([len(layer_pairs), *fixed_slots], default=0)
            free_slots = [slot for slot in range(1, slot_limit + 1) if slot not in fixed_slots]
            while len(free_slots) < len(mutable_pairs):
                slot_limit += 1
                if slot_limit not in fixed_slots:
                    free_slots.append(slot_limit)
            for order, pair in zip(free_slots, mutable_pairs):
                by_key[pair.key].order = order
        return normalized

    def fixed_constraints_preserved(self, pairs: list[PairEntry]) -> bool:
        by_key = {pair.key: pair for pair in pairs}
        for key in self.fixed_pair_keys:
            pair = by_key.get(key)
            if pair is None:
                return False
            target_layer, target_order = self.fixed_pair_targets[key]
            if pair.layer != target_layer or pair.order != target_order:
                return False
        return True

    def layer_pairs(self, pairs: list[PairEntry], layer: str) -> list[PairEntry]:
        return sorted([p for p in pairs if p.layer == layer], key=lambda item: (item.order, self.pair_to_idx[item.key]))

    def get_observation(self) -> np.ndarray:
        layer_index = {layer: i for i, layer in enumerate(self.layer_names)}
        max_order = max(1, max((p.order for p in self.pairs), default=1))
        rows: list[list[float]] = []
        for i, pair in enumerate(self.pairs):
            curr_layer = [0.0] * len(self.layer_names)
            curr_layer[layer_index[pair.layer]] = 1.0
            base_layer, base_order = self.baseline_map[pair.key]
            base_layer_idx = layer_index[base_layer]
            layer_changed = 1.0 if pair.layer != base_layer else 0.0
            order_delta = (pair.order - base_order) / max_order
            rows.append(
                curr_layer
                + [
                    pair.order / max_order,
                    layer_index[pair.layer] / max(1, len(self.layer_names) - 1),
                    base_layer_idx / max(1, len(self.layer_names) - 1),
                    layer_changed,
                    order_delta,
                ]
                + self.board_data.observation[i].tolist()
            )
        return np.asarray(rows, dtype=np.float32)

    def get_action_mask(self) -> np.ndarray:
        mask = np.zeros((self.pair_count, self.n_ops), dtype=np.float32)
        for i, pair in enumerate(self.pairs):
            if pair.key in self.fixed_pair_keys:
                continue
            same_layer = self.layer_pairs(self.pairs, pair.layer)
            position = next((idx for idx, item in enumerate(same_layer) if item.key == pair.key), None)
            base_layer, base_order = self.baseline_map[pair.key]
            if position is not None and position > 0 and same_layer[position - 1].key not in self.fixed_pair_keys:
                mask[i, OP_EARLIER] = 1.0
            if (
                position is not None
                and position < len(same_layer) - 1
                and same_layer[position + 1].key not in self.fixed_pair_keys
            ):
                mask[i, OP_LATER] = 1.0
            if pair.layer != base_layer or pair.order != base_order:
                occupant = [
                    p for p in self.pairs
                    if p.key != pair.key and p.layer == base_layer and p.order == base_order
                ]
                if not occupant or occupant[0].key not in self.fixed_pair_keys:
                    mask[i, OP_RESTORE] = 1.0
            for layer_offset, target_layer in enumerate(self.layer_names):
                if target_layer == pair.layer:
                    continue
                target_same_slot = [p for p in self.pairs if p.layer == target_layer and p.order == pair.order]
                if len(target_same_slot) == 1 and target_same_slot[0].key not in self.fixed_pair_keys:
                    mask[i, OP_SWAP_LAYER_BASE + layer_offset] = 1.0
        return mask.reshape(-1)

    def focus_action_mask(self, epsilon: float, prune_strength: float) -> np.ndarray:
        legal = self.get_action_mask().reshape(self.pair_count, self.n_ops)
        if random.random() > prune_strength:
            return legal.reshape(-1)
        focus: set[int] = set()
        for net in self.missing_nets:
            key = self.pair_keys_by_net.get(net)
            if key in self.pair_to_idx:
                idx = self.pair_to_idx[key]
                focus.add(idx)
                focus.update(range(max(0, idx - 2), min(self.pair_count, idx + 3)))
                order = self.pairs[idx].order
                for j, pair in enumerate(self.pairs):
                    if pair.order == order:
                        focus.add(j)
        if not focus or epsilon > 0.45:
            focus.update(random.sample(range(self.pair_count), k=min(12, self.pair_count)))
        pruned = np.zeros_like(legal)
        for idx in focus:
            pruned[idx] = legal[idx]
        if pruned.sum() <= 0:
            return legal.reshape(-1)
        return pruned.reshape(-1)

    def apply_action(self, flat_action: int) -> list[PairEntry]:
        pair_idx = flat_action // self.n_ops
        op = flat_action % self.n_ops
        pairs = [PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in self.pairs]
        pair_by_key = {p.key: p for p in pairs}
        target = pairs[pair_idx]
        if target.key in self.fixed_pair_keys:
            return [PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in self.pairs]

        if op == OP_EARLIER:
            same_layer = self.layer_pairs(pairs, target.layer)
            position = next((idx for idx, item in enumerate(same_layer) if item.key == target.key), None)
            if position is not None and position > 0:
                prev = pair_by_key[same_layer[position - 1].key]
                if prev.key not in self.fixed_pair_keys:
                    target.order, prev.order = prev.order, target.order
        elif op == OP_LATER:
            same_layer = self.layer_pairs(pairs, target.layer)
            position = next((idx for idx, item in enumerate(same_layer) if item.key == target.key), None)
            if position is not None and position < len(same_layer) - 1:
                nxt = pair_by_key[same_layer[position + 1].key]
                if nxt.key not in self.fixed_pair_keys:
                    target.order, nxt.order = nxt.order, target.order
        elif op == OP_RESTORE:
            base_layer, base_order = self.baseline_map[target.key]
            other = [
                p for p in pairs
                if p.key != target.key and p.layer == base_layer and p.order == base_order
            ]
            if other and other[0].key not in self.fixed_pair_keys:
                other_pair = pair_by_key[other[0].key]
                other_pair.layer = target.layer
                other_pair.order = target.order
            target.layer = base_layer
            target.order = base_order
        elif op >= OP_SWAP_LAYER_BASE:
            layer_idx = op - OP_SWAP_LAYER_BASE
            if 0 <= layer_idx < len(self.layer_names):
                target_layer = self.layer_names[layer_idx]
                other = [p for p in pairs if p.layer == target_layer and p.order == target.order]
                if target_layer != target.layer and len(other) == 1 and other[0].key not in self.fixed_pair_keys:
                    other_pair = pair_by_key[other[0].key]
                    other_pair.layer, target.layer = target.layer, other_pair.layer
        normalized = self.normalize_orders(pairs)
        if not self.fixed_constraints_preserved(normalized):
            raise RuntimeError("Internal error: fixed pair constraints were modified by an action")
        return normalized

    def evaluate_pairs(self, pairs: list[PairEntry], run_name: str, source: str) -> eval_arc.RouteCandidate | None:
        return eval_arc.evaluate_pairs(self.output_root, self.footer_lines, self.layer_names, pairs, run_name, source)

    def step(self, flat_action: int, global_eval_index: int) -> tuple[np.ndarray, np.ndarray, float, bool, dict[str, Any]]:
        self.step_count += 1
        prev_cost = self.cost(self.stats)
        new_pairs = self.apply_action(flat_action)
        if eval_arc.signature(new_pairs) == eval_arc.signature(self.pairs):
            done = self.step_count >= self.max_episode_steps
            return self.get_observation(), self.get_action_mask(), -0.2, done, {"evaluated": False, "candidate": None}

        run_name = f"env{self.env_id:02d}_{global_eval_index:05d}_s{self.step_count:03d}"
        candidate = self.evaluate_pairs(new_pairs, run_name, f"pair_action:{flat_action % self.n_ops}")
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

        self.pairs = [PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in new_pairs]
        self.stats = candidate.stats
        self.missing_nets = list(candidate.missing_nets)
        done = self.step_count >= self.max_episode_steps
        return self.get_observation(), self.get_action_mask(), reward, done, {
            "evaluated": True,
            "candidate": candidate,
        }
