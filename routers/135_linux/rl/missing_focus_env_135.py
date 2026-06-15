#!/usr/bin/env python3

from __future__ import annotations

import math
import random

import numpy as np

import rl_135_core as core
import routing_eval_135 as eval135
from rl_135_core import BoardData, OrderEntry, RouteStats
from routing_env_135 import RoutingLocalActionEnv


class MissingFocusRoutingEnv(RoutingLocalActionEnv):
    def __init__(self, *args, neighbor_k: int, delta_order_threshold: float, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.neighbor_k = neighbor_k
        self.delta_order_threshold = delta_order_threshold
        pin_coords = core.parse_pin_csv(core.PROJECT_DIR / core.BASE_PINS)
        self.net_neighbors = self._build_neighbors(pin_coords)

    def _build_neighbors(self, pin_coords: dict[str, tuple[float, float]]) -> dict[str, list[str]]:
        neighbors: dict[str, list[str]] = {}
        positions = {}
        for entry, pin_name in zip(self.board_data.entries, self.board_data.pin_names):
            if pin_name in pin_coords:
                positions[entry.net] = pin_coords[pin_name]
        nets = list(positions)
        for net in self.net_names:
            if net not in positions:
                neighbors[net] = []
                continue
            x0, y0 = positions[net]
            ranked = []
            for other in nets:
                if other == net:
                    continue
                x1, y1 = positions[other]
                ranked.append((math.hypot(x1 - x0, y1 - y0), other))
            ranked.sort(key=lambda item: item[0])
            neighbors[net] = [name for _dist, name in ranked]
        return neighbors

    def deviated_nets(self) -> set[str]:
        focus: set[str] = set()
        for entry in self.entries:
            base_layer, base_order = self.baseline_map[entry.net]
            if entry.layer != base_layer or abs(entry.order - base_order) >= self.delta_order_threshold:
                focus.add(entry.net)
        return focus

    def expand_neighbors(self, seeds: set[str], neighbor_k: int, hop2_k: int = 0) -> set[str]:
        result = set(seeds)
        frontier = list(seeds)
        for net in frontier:
            result.update(self.net_neighbors.get(net, [])[:neighbor_k])
        if hop2_k > 0:
            one_hop = list(result)
            for net in one_hop:
                result.update(self.net_neighbors.get(net, [])[:hop2_k])
        return result

    def focus_nets_missing_focus(self, epsilon: float, prune_strength: float) -> set[str] | None:
        gate = prune_strength + (0.07 if self.missing_nets else -0.05)
        gate = min(0.98, max(0.0, gate))
        if random.random() > gate:
            return None
        if self.missing_nets:
            focus = set(self.missing_nets)
            focus = self.expand_neighbors(focus, self.neighbor_k, hop2_k=max(4, self.neighbor_k // 6))
            deviated = self.deviated_nets()
            for net in list(deviated):
                if net in focus:
                    focus.update(self.net_neighbors.get(net, [])[: max(6, self.neighbor_k // 4)])
        else:
            deviated = self.deviated_nets()
            focus = self.expand_neighbors(deviated, max(10, self.neighbor_k // 2), hop2_k=0)
            if not focus:
                focus = set(random.sample(self.net_names, k=min(max(12, len(self.net_names) // 10), len(self.net_names))))
        if epsilon > 0.45:
            extra = random.sample(self.net_names, k=min(max(4, len(self.net_names) // 20), len(self.net_names)))
            focus.update(extra)
        return focus

    def get_missing_focus_action_mask(self, epsilon: float, prune_strength: float) -> np.ndarray:
        legal = self.get_action_mask().reshape(len(self.entries), 4)
        focus = self.focus_nets_missing_focus(epsilon, prune_strength)
        if focus is None:
            return legal.reshape(-1)
        pruned = np.zeros_like(legal)
        for idx, entry in enumerate(self.entries):
            if entry.net in focus:
                pruned[idx] = legal[idx]
        if pruned.sum() <= 0:
            return legal.reshape(-1)
        return pruned.reshape(-1)
