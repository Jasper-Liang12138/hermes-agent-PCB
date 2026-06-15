#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import math
import random
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    import torch
    from torch import nn
    from torch.distributions import Bernoulli, Normal
except ModuleNotFoundError:
    torch = None
    Bernoulli = None
    Normal = None

    class _MissingTorchNN:
        class Module:
            pass

    nn = _MissingTorchNN()


RL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = RL_DIR.parent
F_BIN = PROJECT_DIR / "f.out"
TURN_SCRIPT = PROJECT_DIR / "Turn_135_QYF.py"

BASE_LAYOUT = "402Pin_08BGA_8L_S_01141700.txt"
BASE_ORDER = "order_out.txt"
BASE_NETLIST = "net_list.txt"
BASE_PARAMETER = "parameter.txt"
BASE_PINS = "U22_pins.csv"

COPY_INPUTS = [
    BASE_LAYOUT,
    BASE_ORDER,
    BASE_NETLIST,
    BASE_PARAMETER,
    BASE_PINS,
    "ARC_to_135.py",
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
    observation: np.ndarray
    pin_names: list[str]
    net_values: list[float]
    top_count: int
    bottom_count: int


@dataclass
class RouteStats:
    total_nets: int
    routed_nets: int
    completion_rate: float
    total_wire_length: float
    line_segments: int
    arc_segments: int
    vias: int
    line_out_bytes: int
    output_bytes: int


@dataclass
class TrialResult:
    run_name: str
    success: bool
    reward: float
    return_code_f: int
    return_code_turn: int
    kept_flip_count: int
    changed_flip_count: int
    top_count: int
    bottom_count: int
    stats: RouteStats
    run_dir: str | None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stats"] = asdict(self.stats)
        return payload


def read_order_entries(path: Path) -> tuple[list[OrderEntry], list[str]]:
    entries: list[OrderEntry] = []
    footer_lines: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) >= 3:
                entries.append(OrderEntry(net=parts[0], layer=parts[1], order=int(parts[2])))
            else:
                footer_lines.append(stripped)
    return entries, footer_lines


def write_order_entries(path: Path, entries: list[OrderEntry], footer_lines: list[str]) -> None:
    top_entries = [entry for entry in entries if entry.layer == "TOP"]
    bottom_entries = [entry for entry in entries if entry.layer == "BOTTOM"]

    top_entries.sort(key=lambda item: item.order)
    bottom_entries.sort(key=lambda item: item.order)

    with path.open("w", encoding="utf-8") as handle:
        for entry in top_entries:
            handle.write(f"{entry.net} {entry.layer} {entry.order}\n")
        handle.write("\n")
        for entry in bottom_entries:
            handle.write(f"{entry.net} {entry.layer} {entry.order}\n")
        if footer_lines:
            handle.write("\n")
            for line in footer_lines:
                handle.write(f"{line}\n")


def parse_pin_csv(csv_file: Path) -> dict[str, tuple[float, float]]:
    pin_coords: dict[str, tuple[float, float]] = {}
    with csv_file.open("r", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or row[0].startswith("#") or row[0].startswith("Units"):
                continue
            if not row[0] or not row[0].strip():
                continue
            pin_name = row[0].strip()
            try:
                x = float(row[2].strip())
                y = float(row[3].strip())
            except (ValueError, IndexError):
                continue
            pin_coords[pin_name] = (x, y)
    return pin_coords


def parse_netlist(netlist_file: Path) -> tuple[list[str], dict[str, list[str]], dict[str, float]]:
    nets: list[str] = []
    net_to_pins: dict[str, list[str]] = {}
    net_values: dict[str, float] = {}
    with netlist_file.open("r", encoding="utf-8") as handle:
        lines = handle.readlines()

    if lines:
        last_line = lines[-1].strip()
        if last_line and ";" not in last_line:
            lines = lines[:-1]

    for raw in lines:
        line = raw.strip()
        if not line or ";" not in line:
            continue
        parts = [item.strip() for item in line.split(";")]
        if len(parts) < 2:
            continue
        net_name = parts[0]
        pin_part = parts[1]
        pins = []
        for token in pin_part.split():
            if token.startswith("U22."):
                pins.append(token.replace("U22.", ""))
        nets.append(net_name)
        net_to_pins[net_name] = pins
        if len(parts) >= 3:
            try:
                net_values[net_name] = float(parts[2])
            except ValueError:
                net_values[net_name] = 0.0
    return nets, net_to_pins, net_values


def get_pin_rank(pin_name: str, pin_coords: dict[str, tuple[float, float]]) -> bool:
    if pin_name not in pin_coords:
        return False
    x, y = pin_coords[pin_name]
    xs = sorted({coords[0] for coords in pin_coords.values()})
    ys = sorted({coords[1] for coords in pin_coords.values()})

    if len(xs) < 4 or len(ys) < 4:
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        tolerance = 1.0
        return (
            abs(x - min_x) <= tolerance
            or abs(x - max_x) <= tolerance
            or abs(y - min_y) <= tolerance
            or abs(y - max_y) <= tolerance
        )

    tolerance = 1.0
    min_x_1st, min_x_2nd = xs[0], xs[1]
    max_x_1st, max_x_2nd = xs[-1], xs[-2]
    min_y_1st, min_y_2nd = ys[0], ys[1]
    max_y_1st, max_y_2nd = ys[-1], ys[-2]

    return (
        abs(x - min_x_1st) <= tolerance
        or abs(x - min_x_2nd) <= tolerance
        or abs(x - max_x_1st) <= tolerance
        or abs(x - max_x_2nd) <= tolerance
        or abs(y - min_y_1st) <= tolerance
        or abs(y - min_y_2nd) <= tolerance
        or abs(y - max_y_1st) <= tolerance
        or abs(y - max_y_2nd) <= tolerance
    )


def load_board_data(
    script_dir: Path = PROJECT_DIR,
    order_entries: list[OrderEntry] | None = None,
    footer_lines: list[str] | None = None,
) -> BoardData:
    if order_entries is None:
        order_entries, footer_lines = read_order_entries(script_dir / BASE_ORDER)
    else:
        order_entries = [OrderEntry(entry.net, entry.layer, entry.order) for entry in order_entries]
        footer_lines = list(footer_lines or [])
    pin_coords = parse_pin_csv(script_dir / BASE_PINS)
    _, net_to_pins, net_values = parse_netlist(script_dir / BASE_NETLIST)

    xs = [coords[0] for coords in pin_coords.values()]
    ys = [coords[1] for coords in pin_coords.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    max_order = max(entry.order for entry in order_entries)
    max_value = max(net_values.values()) if net_values else 1.0

    features: list[list[float]] = []
    pin_names: list[str] = []
    values: list[float] = []

    for index, entry in enumerate(order_entries):
        pins = net_to_pins.get(entry.net, [])
        pin_name = pins[0] if pins else ""
        pin_names.append(pin_name)
        x, y = pin_coords.get(pin_name, (min_x, min_y))
        x_norm = 0.0 if max_x == min_x else (x - min_x) / (max_x - min_x)
        y_norm = 0.0 if max_y == min_y else (y - min_y) / (max_y - min_y)
        is_top = 1.0 if entry.layer == "TOP" else 0.0
        is_bottom = 1.0 if entry.layer == "BOTTOM" else 0.0
        order_norm = entry.order / max_order if max_order else 0.0
        value = net_values.get(entry.net, 0.0)
        values.append(value)
        value_norm = value / max_value if max_value else 0.0
        outer_ring = 1.0 if pin_name and get_pin_rank(pin_name, pin_coords) else 0.0
        pin_count = float(len(pins))
        features.append(
            [
                is_top,
                is_bottom,
                order_norm,
                x_norm,
                y_norm,
                value_norm,
                outer_ring,
                1.0 if pin_count > 1 else 0.0,
                pin_count / 8.0,
                index / max(1, len(order_entries) - 1),
            ]
        )

    observation = np.asarray(features, dtype=np.float32)
    return BoardData(
        entries=order_entries,
        footer_lines=footer_lines,
        observation=observation,
        pin_names=pin_names,
        net_values=values,
        top_count=sum(1 for entry in order_entries if entry.layer == "TOP"),
        bottom_count=sum(1 for entry in order_entries if entry.layer == "BOTTOM"),
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


def compute_stats(work_dir: Path) -> RouteStats:
    order_path = work_dir / BASE_ORDER
    line_path = work_dir / "line.out"
    output_path = work_dir / "output.txt"

    total_nets = 0
    with order_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            parts = raw.split()
            if len(parts) >= 3:
                total_nets += 1

    routed_nets: set[str] = set()
    total_wire_length = 0.0
    line_segments = 0
    arc_segments = 0
    vias = 0

    with line_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            parts = line.split("!")
            if len(parts) < 2:
                continue
            kind = parts[1].upper()

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

    routed_nets_count = len(routed_nets)
    return RouteStats(
        total_nets=total_nets,
        routed_nets=routed_nets_count,
        completion_rate=routed_nets_count / total_nets if total_nets else 0.0,
        total_wire_length=total_wire_length,
        line_segments=line_segments,
        arc_segments=arc_segments,
        vias=vias,
        line_out_bytes=line_path.stat().st_size,
        output_bytes=output_path.stat().st_size if output_path.exists() else 0,
    )


def copy_inputs(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    for filename in COPY_INPUTS:
        shutil.copy2(PROJECT_DIR / filename, work_dir / filename)
    runtime_src = PROJECT_DIR.parent / "run_with_local_libstdcpp.sh"
    if runtime_src.exists():
        shutil.copy2(runtime_src, work_dir.parent / "run_with_local_libstdcpp.sh")
        (work_dir.parent / "run_with_local_libstdcpp.sh").chmod(0o755)
    for name in ["main", "main.bin"]:
        main_src = PROJECT_DIR / name
        if main_src.exists():
            shutil.copy2(main_src, work_dir / name)
            (work_dir / name).chmod(0o755)


class RL135Environment:
    def __init__(
        self,
        output_root: Path,
        reward_completion_scale: float = 1000.0,
        reward_wire_penalty: float = 20.0,
        reward_via_penalty: float = 50.0,
        reward_flip_penalty: float = 2.0,
        failure_penalty: float = 250.0,
        priority_delta_scale: float = 4.0,
    ) -> None:
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.board_data = load_board_data()
        self.observation = self.board_data.observation.copy()
        self.reward_completion_scale = reward_completion_scale
        self.reward_wire_penalty = reward_wire_penalty
        self.reward_via_penalty = reward_via_penalty
        self.reward_flip_penalty = reward_flip_penalty
        self.failure_penalty = failure_penalty
        self.priority_delta_scale = priority_delta_scale
        self.baseline_dir = self.output_root / "baseline"
        self.baseline_stats = self._ensure_baseline()

    @property
    def net_count(self) -> int:
        return len(self.board_data.entries)

    @property
    def feature_dim(self) -> int:
        return int(self.observation.shape[1])

    def _ensure_baseline(self) -> RouteStats:
        stats_path = self.baseline_dir / "stats.json"
        if stats_path.exists():
            payload = json.loads(stats_path.read_text(encoding="utf-8"))
            return RouteStats(**payload)

        copy_inputs(self.baseline_dir)
        f_return_code = run_command(
            [str(F_BIN), BASE_ORDER, BASE_LAYOUT],
            cwd=self.baseline_dir,
            log_path=self.baseline_dir / "f.log",
        )
        if f_return_code != 0:
            raise RuntimeError(f"Baseline f.out run failed with code {f_return_code}")
        turn_return_code = run_command(
            ["python3", str(TURN_SCRIPT), BASE_LAYOUT, "line.out", "output.txt"],
            cwd=self.baseline_dir,
            log_path=self.baseline_dir / "turn.log",
        )
        if turn_return_code != 0:
            raise RuntimeError(f"Baseline Turn_135_QYF.py run failed with code {turn_return_code}")

        stats = compute_stats(self.baseline_dir)
        stats_path.write_text(json.dumps(asdict(stats), indent=2), encoding="utf-8")
        return stats

    def build_candidate_entries(self, flip_mask: np.ndarray, priorities: np.ndarray) -> list[OrderEntry]:
        if flip_mask.shape[0] != self.net_count or priorities.shape[0] != self.net_count:
            raise ValueError("Action shape does not match net count")

        entries = []
        for entry, flip in zip(self.board_data.entries, flip_mask):
            new_layer = "BOTTOM" if int(flip) and entry.layer == "TOP" else entry.layer
            if int(flip) and entry.layer == "BOTTOM":
                new_layer = "TOP"
            entries.append(OrderEntry(net=entry.net, layer=new_layer, order=entry.order))

        top_items = [
            (idx, entry, entry.order + self.priority_delta_scale * float(np.tanh(priorities[idx])))
            for idx, entry in enumerate(entries)
            if entry.layer == "TOP"
        ]
        bottom_items = [
            (idx, entry, entry.order + self.priority_delta_scale * float(np.tanh(priorities[idx])))
            for idx, entry in enumerate(entries)
            if entry.layer == "BOTTOM"
        ]
        top_items.sort(key=lambda item: (item[2], item[0]))
        bottom_items.sort(key=lambda item: (item[2], item[0]))

        ordered_entries: list[OrderEntry] = []
        for order, (_idx, entry, _priority) in enumerate(top_items, start=1):
            ordered_entries.append(OrderEntry(net=entry.net, layer="TOP", order=order))
        for order, (_idx, entry, _priority) in enumerate(bottom_items, start=1):
            ordered_entries.append(OrderEntry(net=entry.net, layer="BOTTOM", order=order))
        return ordered_entries

    def compute_reward(self, stats: RouteStats, success: bool, changed_flip_count: int) -> float:
        if not success:
            return -self.failure_penalty

        wire_ratio = (
            stats.total_wire_length / self.baseline_stats.total_wire_length
            if self.baseline_stats.total_wire_length > 0
            else 1.0
        )
        via_ratio = (
            stats.vias / self.baseline_stats.vias
            if self.baseline_stats.vias > 0
            else 1.0
        )
        reward = self.reward_completion_scale * stats.completion_rate
        reward -= self.reward_wire_penalty * max(0.0, wire_ratio - 1.0)
        reward -= self.reward_via_penalty * max(0.0, via_ratio - 1.0)
        reward -= self.reward_flip_penalty * changed_flip_count
        return reward

    def save_best_artifacts(self, source_run_dir: Path, target_dir: Path, result: TrialResult) -> None:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_run_dir, target_dir)
        if not (target_dir / "output.txt").exists() and (target_dir / "line.out").exists():
            run_command(
                ["python3", str(TURN_SCRIPT), BASE_LAYOUT, "line.out", "output.txt"],
                cwd=target_dir,
                log_path=target_dir / "turn.log",
            )
        (target_dir / "result.json").write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")

    def evaluate_and_maybe_keep_best(
        self,
        flip_mask: np.ndarray,
        priorities: np.ndarray,
        run_name: str,
        best_dir: Path | None = None,
        keep_if_best: bool = False,
    ) -> tuple[TrialResult, Path]:
        run_dir = self.output_root / "episodes" / run_name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        copy_inputs(run_dir)
        entries = self.build_candidate_entries(flip_mask=flip_mask, priorities=priorities)
        write_order_entries(run_dir / BASE_ORDER, entries, self.board_data.footer_lines)

        f_return_code = run_command(
            [str(F_BIN), BASE_ORDER, BASE_LAYOUT],
            cwd=run_dir,
            log_path=run_dir / "f.log",
        )
        turn_return_code = 0
        success = f_return_code == 0 and (run_dir / "line.out").exists()
        stats = compute_stats(run_dir) if success else RouteStats(
            total_nets=self.net_count,
            routed_nets=0,
            completion_rate=0.0,
            total_wire_length=0.0,
            line_segments=0,
            arc_segments=0,
            vias=0,
            line_out_bytes=0,
            output_bytes=0,
        )
        changed_flip_count = int(np.asarray(flip_mask).sum())
        reward = self.compute_reward(stats=stats, success=success, changed_flip_count=changed_flip_count)
        result = TrialResult(
            run_name=run_name,
            success=success,
            reward=reward,
            return_code_f=f_return_code,
            return_code_turn=turn_return_code,
            kept_flip_count=int(self.net_count - changed_flip_count),
            changed_flip_count=changed_flip_count,
            top_count=sum(1 for entry in entries if entry.layer == "TOP"),
            bottom_count=sum(1 for entry in entries if entry.layer == "BOTTOM"),
            stats=stats,
            run_dir=str(run_dir),
        )
        (run_dir / "result.json").write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
        if keep_if_best and best_dir is not None:
            self.save_best_artifacts(run_dir, best_dir, result)
        return result, run_dir


class ResidualPolicy(nn.Module):
    def __init__(self, net_count: int, feature_dim: int, hidden_dim: int = 256) -> None:
        super().__init__()
        input_dim = net_count * feature_dim
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
        )
        self.flip_head = nn.Linear(hidden_dim, net_count)
        self.priority_head = nn.Linear(hidden_dim, net_count)
        self.value_head = nn.Linear(hidden_dim, 1)
        self.priority_log_std = nn.Parameter(torch.full((net_count,), -6.0))

        nn.init.zeros_(self.flip_head.weight)
        nn.init.constant_(self.flip_head.bias, -8.0)
        nn.init.zeros_(self.priority_head.weight)
        nn.init.zeros_(self.priority_head.bias)
        nn.init.zeros_(self.value_head.weight)
        nn.init.zeros_(self.value_head.bias)

    def forward(self, obs: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        if obs.dim() == 2:
            obs = obs.unsqueeze(0)
        flat = obs.reshape(obs.shape[0], -1)
        hidden = self.encoder(flat)
        flip_logits = self.flip_head(hidden)
        priority_mean = self.priority_head(hidden)
        value = self.value_head(hidden).squeeze(-1)
        log_std = self.priority_log_std.unsqueeze(0).expand_as(priority_mean)
        return flip_logits, priority_mean, log_std, value

    def sample_action(self, obs: torch.Tensor, deterministic: bool = False) -> tuple[dict[str, torch.Tensor], torch.Tensor, torch.Tensor]:
        flip_logits, priority_mean, log_std, value = self.forward(obs)
        flip_dist = Bernoulli(logits=flip_logits)
        priority_dist = Normal(priority_mean, log_std.exp())

        if deterministic:
            flip_action = (torch.sigmoid(flip_logits) >= 0.5).float()
            priority_action = priority_mean
        else:
            flip_action = flip_dist.sample()
            priority_action = priority_dist.rsample()

        log_prob = flip_dist.log_prob(flip_action).sum(dim=-1)
        log_prob += priority_dist.log_prob(priority_action).sum(dim=-1)
        entropy = flip_dist.entropy().sum(dim=-1) + priority_dist.entropy().sum(dim=-1)
        return {"flip": flip_action, "priority": priority_action}, log_prob, entropy

    def evaluate_actions(
        self,
        obs: torch.Tensor,
        flip_action: torch.Tensor,
        priority_action: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        flip_logits, priority_mean, log_std, value = self.forward(obs)
        flip_dist = Bernoulli(logits=flip_logits)
        priority_dist = Normal(priority_mean, log_std.exp())
        log_prob = flip_dist.log_prob(flip_action).sum(dim=-1)
        log_prob += priority_dist.log_prob(priority_action).sum(dim=-1)
        entropy = flip_dist.entropy().sum(dim=-1) + priority_dist.entropy().sum(dim=-1)
        return log_prob, entropy, value


def set_global_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    if torch is not None:
        torch.manual_seed(seed)


def tensor_to_numpy(action: torch.Tensor) -> np.ndarray:
    return action.detach().cpu().numpy().reshape(-1)


def save_checkpoint(
    checkpoint_path: Path,
    policy: ResidualPolicy,
    optimizer: torch.optim.Optimizer,
    config: dict[str, Any],
    best_result: TrialResult | None,
) -> None:
    payload = {
        "model_state": policy.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "config": config,
        "best_result": best_result.to_json() if best_result else None,
    }
    torch.save(payload, checkpoint_path)


def load_checkpoint(checkpoint_path: Path, device: torch.device) -> dict[str, Any]:
    return torch.load(checkpoint_path, map_location=device)
