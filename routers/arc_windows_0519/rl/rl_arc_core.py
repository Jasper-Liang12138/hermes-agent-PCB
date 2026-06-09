#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


RL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = RL_DIR.parent

BASE_LAYOUT = "1231_4_arc.txt"
BASE_ORDER = "order_input.txt"
BASE_CONSTRAIN = "constrain.txt"
BASE_ARC_OUTPUT = "ARC_output.txt"
BASE_NETLIST = "net_list.txt"
BASE_PINS = "U27_pins.csv"

COPY_FILES = [
    BASE_LAYOUT,
    BASE_CONSTRAIN,
    "arc_main.exe",
    "data.txt",
]


@dataclass
class PairEntry:
    key: str
    neg_net: str
    pos_net: str
    layer: str
    order: int


@dataclass
class BoardData:
    pairs: list[PairEntry]
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
    arc_output_bytes: int


@dataclass
class TrialResult:
    run_name: str
    success: bool
    reward: float
    return_code_c: int
    return_code_turn: int
    changed_pair_count: int
    changed_order_count: int
    stats: RouteStats
    missing_nets: list[str]
    run_dir: str | None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stats"] = asdict(self.stats)
        return payload


def read_order_pairs(path: Path) -> tuple[list[PairEntry], list[str], list[str]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path} is too short for Windows arc order format")
    component_name = lines[0]
    try:
        layer_count = int(lines[1])
    except ValueError as exc:
        raise ValueError(f"{path}: second line must be layer count") from exc

    blocks: list[list[tuple[str, str, int]]] = []
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
        block: list[tuple[str, str, int]] = []
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
            block.append((net, layer, order))
            if layer not in seen_layers:
                seen_layers.add(layer)
                layer_names.append(layer)
        blocks.append(sorted(block, key=lambda item: item[2]))

    # Be permissive for manually edited files that omit the Windows block headers.
    if not blocks:
        flat_block: list[tuple[str, str, int]] = []
        for raw in lines[2:]:
            parts = raw.split()
            if len(parts) >= 3:
                net, layer, order = parts[0], parts[1], int(parts[2])
                flat_block.append((net, layer, order))
                if layer not in seen_layers:
                    seen_layers.add(layer)
                    layer_names.append(layer)
        by_layer: dict[str, list[tuple[str, str, int]]] = {layer: [] for layer in layer_names}
        for item in flat_block:
            by_layer[item[1]].append(item)
        blocks = [sorted(by_layer[layer], key=lambda item: item[2]) for layer in layer_names]

    pairs: list[PairEntry] = []
    for block in blocks:
        if not block:
            continue
        if len(block) % 2 != 0:
            layer = block[0][1]
            raise ValueError(f"Layer {layer} has odd row count {len(block)}; expected adjacent P/N pairs")
        for pair_index in range(0, len(block), 2):
            first_net, first_layer, first_order = block[pair_index]
            second_net, second_layer, _second_order = block[pair_index + 1]
            if first_layer != second_layer:
                raise ValueError(f"Adjacent pair {first_net}/{second_net} spans {first_layer}/{second_layer}")
            pair_order = (first_order + 1) // 2
            key = f"{first_net}|{second_net}"
            pairs.append(PairEntry(key=key, neg_net=first_net, pos_net=second_net, layer=first_layer, order=pair_order))

    layer_index = {layer: idx for idx, layer in enumerate(layer_names)}
    pairs.sort(key=lambda item: (layer_index[item.layer], item.order, item.key))
    return pairs, [component_name], layer_names


def clone_pairs(pairs: list[PairEntry]) -> list[PairEntry]:
    return [PairEntry(p.key, p.neg_net, p.pos_net, p.layer, p.order) for p in pairs]


def canonicalize_pairs(pairs: list[PairEntry], layer_names: list[str]) -> list[PairEntry]:
    result: list[PairEntry] = []
    for layer in layer_names:
        layer_pairs = sorted([p for p in pairs if p.layer == layer], key=lambda item: (item.order, item.key))
        for order, pair in enumerate(layer_pairs, start=1):
            result.append(PairEntry(pair.key, pair.neg_net, pair.pos_net, layer, order))
    return result


def write_order_pairs(path: Path, pairs: list[PairEntry], footer_lines: list[str], layer_names: list[str]) -> None:
    layer_index = {layer: idx for idx, layer in enumerate(layer_names)}
    ordered_pairs = sorted(
        clone_pairs(pairs),
        key=lambda item: (layer_index.get(item.layer, len(layer_names)), item.order, item.key),
    )
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"{footer_lines[0] if footer_lines else ''}\n")
        active_layers = [layer for layer in layer_names if any(p.layer == layer for p in ordered_pairs)]
        handle.write(f"{len(active_layers)}\n")
        for layer in layer_names:
            layer_pairs = [p for p in ordered_pairs if p.layer == layer]
            if not layer_pairs:
                continue
            handle.write(f"{len(layer_pairs) * 2}\n")
            for pair in layer_pairs:
                base = (pair.order - 1) * 2
                handle.write(f"{pair.neg_net} {pair.layer} {base + 1}\n")
                handle.write(f"{pair.pos_net} {pair.layer} {base + 2}\n")


def pair_signature(pairs: list[PairEntry]) -> tuple[tuple[str, str, int], ...]:
    return tuple((p.key, p.layer, p.order) for p in pairs)


def parse_pin_csv(csv_file: Path) -> dict[str, tuple[float, float]]:
    pin_coords: dict[str, tuple[float, float]] = {}
    if not csv_file.exists():
        return pin_coords
    with csv_file.open("r", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or row[0].startswith("#") or row[0].startswith("Units"):
                continue
            if not row[0].strip():
                continue
            try:
                pin_coords[row[0].strip()] = (float(row[2]), float(row[3]))
            except (IndexError, ValueError):
                continue
    return pin_coords


def parse_netlist(netlist_file: Path) -> dict[str, tuple[str, str, float]]:
    net_info: dict[str, tuple[str, str, float]] = {}
    if not netlist_file.exists():
        return net_info
    for raw in netlist_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or ";" not in line:
            continue
        parts = [item.strip() for item in line.split(";")]
        if len(parts) < 2:
            continue
        pins = parts[1].split()
        local_pin = ""
        remote_pin = ""
        for token in pins:
            if token.startswith("U27."):
                local_pin = token.replace("U27.", "")
            elif "." in token:
                remote_pin = token
        width = 0.0
        if len(parts) >= 3:
            try:
                width = float(parts[2])
            except ValueError:
                width = 0.0
        net_info[parts[0]] = (local_pin, remote_pin, width)
    return net_info


def load_board_data(
    script_dir: Path = PROJECT_DIR,
    pairs: list[PairEntry] | None = None,
    footer_lines: list[str] | None = None,
    layer_names: list[str] | None = None,
) -> BoardData:
    if pairs is None:
        pairs, footer_lines, layer_names = read_order_pairs(script_dir / BASE_ORDER)
    else:
        pairs = clone_pairs(pairs)
        footer_lines = list(footer_lines or [])
        layer_names = list(layer_names or sorted({pair.layer for pair in pairs}))
    pin_coords = parse_pin_csv(script_dir / BASE_PINS)
    net_info = parse_netlist(script_dir / BASE_NETLIST)

    xs = [x for x, _y in pin_coords.values()] or [0.0]
    ys = [y for _x, y in pin_coords.values()] or [0.0]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    max_order = max((pair.order for pair in pairs), default=1)
    max_width = max((info[2] for info in net_info.values()), default=1.0) or 1.0
    layer_index = {layer: idx for idx, layer in enumerate(layer_names)}

    features: list[list[float]] = []
    for index, pair in enumerate(pairs):
        local_pin = net_info.get(pair.neg_net, ("", "", 0.0))[0] or net_info.get(pair.pos_net, ("", "", 0.0))[0]
        x, y = pin_coords.get(local_pin, (min_x, min_y))
        x_norm = 0.0 if max_x == min_x else (x - min_x) / (max_x - min_x)
        y_norm = 0.0 if max_y == min_y else (y - min_y) / (max_y - min_y)
        width = max(net_info.get(pair.neg_net, ("", "", 0.0))[2], net_info.get(pair.pos_net, ("", "", 0.0))[2])
        layer_one_hot = [1.0 if layer == pair.layer else 0.0 for layer in layer_names]
        features.append(
            layer_one_hot
            + [
                pair.order / max_order,
                x_norm,
                y_norm,
                width / max_width,
                index / max(1, len(pairs) - 1),
                layer_index[pair.layer] / max(1, len(layer_names) - 1),
            ]
        )

    return BoardData(
        pairs=pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
        observation=np.asarray(features, dtype=np.float32),
    )


def run_command(cmd: list[str], cwd: Path, log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8") as handle:
        run_args = cmd
        if os.name == "nt":
            run_args = [cmd[0].replace("/", "\\"), *cmd[1:]]
            run_args = subprocess.list2cmdline(run_args)
        process = subprocess.run(
            run_args,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            shell=os.name == "nt",
        )
    return process.returncode


def copy_inputs(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    for filename in COPY_FILES:
        src = PROJECT_DIR / filename
        if src.exists():
            shutil.copy2(src, work_dir / filename)


def compute_stats(work_dir: Path) -> RouteStats:
    order_path = work_dir / BASE_ORDER
    arc_path = work_dir / BASE_ARC_OUTPUT

    total_nets = len(read_order_pairs(order_path)[0]) * 2

    routed_nets: set[str] = set()
    total_wire_length = 0.0
    line_segments = 0
    arc_segments = 0
    vias = 0

    if arc_path.exists():
        for raw in arc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split("!")
            if len(parts) < 2:
                continue
            kind = parts[1].upper()
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
        arc_output_bytes=arc_path.stat().st_size if arc_path.exists() else 0,
    )


def read_missing_nets(work_dir: Path) -> list[str]:
    order_nets: set[str] = set()
    for pair in read_order_pairs(work_dir / BASE_ORDER)[0]:
        order_nets.add(pair.neg_net)
        order_nets.add(pair.pos_net)
    routed_nets: set[str] = set()
    arc_path = work_dir / BASE_ARC_OUTPUT
    if arc_path.exists():
        for raw in arc_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = raw.strip().split("!")
            if len(parts) >= 4 and parts[1].upper() in {"LINE", "ARC"}:
                routed_nets.add(parts[3])
    return sorted(order_nets - routed_nets)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
