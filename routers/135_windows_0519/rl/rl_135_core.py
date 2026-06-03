#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


RL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = RL_DIR.parent

BASE_LAYOUT = "402Pin_08BGA_8L_S_01141700.txt"
BASE_ORDER = "order_input.txt"
BASE_ROUTE_OUTPUT = "line.out"
BASE_STATS_OUTPUT = "statistical.out"

COPY_FILES = [
    BASE_LAYOUT,
    BASE_ORDER,
    "135_main.exe",
]


@dataclass
class OrderEntry:
    net: str
    layer: str
    order: int


@dataclass
class BoardData:
    entries: list[OrderEntry]
    footer_lines: list[str]
    layer_names: list[str]
    observation: np.ndarray


@dataclass
class RouteStats:
    total_nets: int
    routed_nets: int
    completion_rate: float
    total_wire_length: float
    line_segments: int
    arc_segments: int
    vias: int
    route_output_bytes: int
    stats_output_bytes: int


@dataclass
class TrialResult:
    run_name: str
    success: bool
    reward: float
    return_code_main: int
    changed_net_count: int
    changed_order_count: int
    stats: RouteStats
    missing_nets: list[str]
    run_dir: str | None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stats"] = asdict(self.stats)
        return payload


def read_order_entries(path: Path) -> tuple[list[OrderEntry], list[str], list[str]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path} is too short for Windows 135 order format")
    component_name = lines[0]
    try:
        layer_count = int(lines[1])
    except ValueError as exc:
        raise ValueError(f"{path}: second line must be layer count") from exc

    entries: list[OrderEntry] = []
    layer_names: list[str] = []
    seen_layers: set[str] = set()
    idx = 2
    for _layer_block in range(layer_count):
        if idx >= len(lines):
            break
        try:
            block_count = int(lines[idx])
        except ValueError as exc:
            raise ValueError(f"{path}:{idx + 1} must be a layer block count") from exc
        idx += 1
        for _ in range(block_count):
            if idx >= len(lines):
                raise ValueError(f"{path} ended inside a layer block")
            parts = lines[idx].split()
            idx += 1
            if len(parts) < 3:
                continue
            net, layer, order_raw = parts[0], parts[1], parts[2]
            try:
                order = int(order_raw)
            except ValueError as exc:
                raise ValueError(f"{path}:{idx} has non-integer order {order_raw!r}") from exc
            entries.append(OrderEntry(net=net, layer=layer, order=order))
            if layer not in seen_layers:
                seen_layers.add(layer)
                layer_names.append(layer)

    # Be permissive for manually edited files that omit the block header counts.
    if not entries:
        for raw in lines[2:]:
            parts = raw.split()
            if len(parts) >= 3:
                net, layer, order = parts[0], parts[1], int(parts[2])
                entries.append(OrderEntry(net=net, layer=layer, order=order))
                if layer not in seen_layers:
                    seen_layers.add(layer)
                    layer_names.append(layer)

    layer_index = {layer: idx for idx, layer in enumerate(layer_names)}
    entries.sort(key=lambda item: (layer_index[item.layer], item.order, item.net))
    return entries, [component_name], layer_names


def clone_entries(entries: list[OrderEntry]) -> list[OrderEntry]:
    return [OrderEntry(entry.net, entry.layer, entry.order) for entry in entries]


def canonicalize_entries(entries: list[OrderEntry], layer_names: list[str]) -> list[OrderEntry]:
    result: list[OrderEntry] = []
    for layer in layer_names:
        layer_entries = sorted([p for p in entries if p.layer == layer], key=lambda item: (item.order, item.net))
        for order, entry in enumerate(layer_entries, start=1):
            result.append(OrderEntry(entry.net, layer, order))
    return result


def write_order_entries(path: Path, entries: list[OrderEntry], footer_lines: list[str], layer_names: list[str]) -> None:
    layer_index = {layer: idx for idx, layer in enumerate(layer_names)}
    ordered_entries = sorted(
        clone_entries(entries),
        key=lambda item: (layer_index.get(item.layer, len(layer_names)), item.order, item.net),
    )
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"{footer_lines[0] if footer_lines else ''}\n")
        active_layers = [layer for layer in layer_names if any(p.layer == layer for p in ordered_entries)]
        handle.write(f"{len(active_layers)}\n")
        for layer in layer_names:
            layer_entries = [p for p in ordered_entries if p.layer == layer]
            if not layer_entries:
                continue
            handle.write(f"{len(layer_entries)}\n")
            for entry in layer_entries:
                handle.write(f"{entry.net} {entry.layer} {entry.order}\n")


def entry_signature(entries: list[OrderEntry]) -> tuple[tuple[str, str, int], ...]:
    return tuple((p.net, p.layer, p.order) for p in entries)


def load_board_data(
    script_dir: Path = PROJECT_DIR,
    entries: list[OrderEntry] | None = None,
    footer_lines: list[str] | None = None,
    layer_names: list[str] | None = None,
) -> BoardData:
    if entries is None:
        entries, footer_lines, layer_names = read_order_entries(script_dir / BASE_ORDER)
    else:
        entries = clone_entries(entries)
        footer_lines = list(footer_lines or [])
        layer_names = list(layer_names or sorted({entry.layer for entry in entries}))
    max_order = max((entry.order for entry in entries), default=1)
    layer_index = {layer: idx for idx, layer in enumerate(layer_names)}

    features: list[list[float]] = []
    for index, entry in enumerate(entries):
        layer_one_hot = [1.0 if layer == entry.layer else 0.0 for layer in layer_names]
        features.append(
            layer_one_hot
            + [
                entry.order / max_order,
                0.0,
                0.0,
                0.0,
                index / max(1, len(entries) - 1),
                layer_index[entry.layer] / max(1, len(layer_names) - 1),
            ]
        )

    return BoardData(
        entries=entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
        observation=np.asarray(features, dtype=np.float32),
    )


def run_command(cmd: list[str], cwd: Path, log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    return process.returncode


def copy_inputs(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    for filename in COPY_FILES:
        shutil.copy2(PROJECT_DIR / filename, work_dir / filename)


def compute_stats(work_dir: Path) -> RouteStats:
    order_path = work_dir / BASE_ORDER
    route_path = work_dir / BASE_ROUTE_OUTPUT
    stats_path = work_dir / BASE_STATS_OUTPUT

    total_nets = len(read_order_entries(order_path)[0])

    routed_nets: set[str] = set()
    total_wire_length = 0.0
    line_segments = 0
    arc_segments = 0
    vias = 0

    if route_path.exists():
        for raw in route_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split("!")
            if len(parts) < 2:
                continue
            kind = parts[1].upper()
            if len(parts) >= 3 and parts[2].upper() == "CIRCLE":
                kind = "CIRCLE"
            try:
                if kind == "LINE" and len(parts) == 9:
                    _, _, _obj, net, x1, y1, x2, y2, _width = parts
                    x1, y1, x2, y2 = map(float, (x1, y1, x2, y2))
                    total_wire_length += math.hypot(x2 - x1, y2 - y1)
                    routed_nets.add(net)
                    line_segments += 1
                elif kind == "ARC" and len(parts) == 13:
                    _, _, _obj, net, x1, y1, x2, y2, cx, cy, radius, _width, direction = parts
                    x1, y1, x2, y2, cx, cy, radius = map(float, (x1, y1, x2, y2, cx, cy, radius))
                    angle1 = math.atan2(y1 - cy, x1 - cx)
                    angle2 = math.atan2(y2 - cy, x2 - cx)
                    if direction.strip().upper() == "CLOCKWISE":
                        delta = (angle1 - angle2) % (2 * math.pi)
                    else:
                        delta = (angle2 - angle1) % (2 * math.pi)
                    total_wire_length += abs(radius) * delta
                    routed_nets.add(net)
                    arc_segments += 1
                elif kind == "CIRCLE":
                    vias += 1
            except ValueError:
                continue

    routed_count = len(routed_nets)
    return RouteStats(
        total_nets=total_nets,
        routed_nets=routed_count,
        completion_rate=routed_count / total_nets if total_nets else 0.0,
        total_wire_length=total_wire_length,
        line_segments=line_segments,
        arc_segments=arc_segments,
        vias=vias,
        route_output_bytes=route_path.stat().st_size if route_path.exists() else 0,
        stats_output_bytes=stats_path.stat().st_size if stats_path.exists() else 0,
    )


def read_missing_nets(work_dir: Path) -> list[str]:
    order_nets: set[str] = set()
    order_nets = {entry.net for entry in read_order_entries(work_dir / BASE_ORDER)[0]}
    routed_nets: set[str] = set()
    route_path = work_dir / BASE_ROUTE_OUTPUT
    if route_path.exists():
        for raw in route_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = raw.strip().split("!")
            if len(parts) >= 4 and parts[1].upper() in {"LINE", "ARC"}:
                routed_nets.add(parts[3])
    return sorted(order_nets - routed_nets)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
