#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

import rl_135_core as core
from rl_135_core import OrderEntry, RouteStats


@dataclass
class RouteCandidate:
    name: str
    source: str
    entries: list[OrderEntry]
    stats: RouteStats
    run_dir: Path | None
    missing_nets: list[str]
    top_mask: np.ndarray | None = None
    order_scores: np.ndarray | None = None


def candidate_to_json(candidate: RouteCandidate | None) -> dict | None:
    if candidate is None:
        return None
    run_dir = None
    if candidate.run_dir is not None:
        run_dir = os.path.relpath(candidate.run_dir, start=Path(__file__).resolve().parent)
    return {
        "name": candidate.name,
        "source": candidate.source,
        "stats": asdict(candidate.stats),
        "missing_nets": candidate.missing_nets,
        "run_dir": run_dir,
    }


def clone_entries(entries: Iterable[OrderEntry]) -> list[OrderEntry]:
    return [OrderEntry(e.net, e.layer, e.order) for e in entries]


def signature(entries: list[OrderEntry]) -> tuple[tuple[str, str, int], ...]:
    return tuple((e.net, e.layer, e.order) for e in entries)


def read_missing_nets(run_dir: Path) -> list[str]:
    order_nets: set[str] = set()
    for raw in (run_dir / core.BASE_ORDER).read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if len(parts) >= 3:
            order_nets.add(parts[0])
    routed_nets: set[str] = set()
    for raw in (run_dir / "line.out").read_text(encoding="utf-8").splitlines():
        parts = raw.strip().split("!")
        if len(parts) >= 4 and parts[1].upper() in {"LINE", "ARC"}:
            routed_nets.add(parts[3])
    return sorted(order_nets - routed_nets)


def run_turn_if_needed(run_dir: Path) -> int:
    output_path = run_dir / "output.txt"
    if output_path.exists():
        return 0
    if not (run_dir / "line.out").exists():
        return 1
    return core.run_command(
        ["python3", str(core.TURN_SCRIPT), core.BASE_LAYOUT, "line.out", "output.txt"],
        cwd=run_dir,
        log_path=run_dir / "turn.log",
    )


def evaluate_entries(
    output_root: Path,
    footer_lines: list[str],
    entries: list[OrderEntry],
    run_name: str,
    source: str,
    top_mask: np.ndarray | None = None,
    order_scores: np.ndarray | None = None,
) -> RouteCandidate | None:
    run_dir = output_root / "episodes" / run_name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    core.copy_inputs(run_dir)
    core.write_order_entries(run_dir / core.BASE_ORDER, entries, footer_lines)
    f_return_code = core.run_command(
        [str(core.F_BIN), core.BASE_ORDER, core.BASE_LAYOUT],
        cwd=run_dir,
        log_path=run_dir / "f.log",
    )
    if f_return_code != 0 or not (run_dir / "line.out").exists():
        shutil.rmtree(run_dir)
        return None
    stats = core.compute_stats(run_dir)
    missing_nets = read_missing_nets(run_dir)
    return RouteCandidate(
        run_name,
        source,
        clone_entries(entries),
        stats,
        run_dir,
        missing_nets,
        top_mask=top_mask,
        order_scores=order_scores,
    )


def prepare_baseline(output_root: Path | None = None) -> tuple[list[OrderEntry], list[str], RouteCandidate, core.BoardData]:
    entries, footer_lines = core.read_order_entries(core.PROJECT_DIR / core.BASE_ORDER)
    board_data = core.load_board_data(core.PROJECT_DIR)
    if output_root is None:
        stats = core.compute_stats(core.PROJECT_DIR)
        missing_nets = read_missing_nets(core.PROJECT_DIR)
        baseline_candidate = RouteCandidate(
            "baseline",
            "baseline",
            clone_entries(entries),
            stats,
            core.PROJECT_DIR,
            missing_nets,
        )
    else:
        baseline_candidate = evaluate_entries(
            output_root=output_root,
            footer_lines=footer_lines,
            entries=entries,
            run_name="baseline",
            source="baseline",
        )
        if baseline_candidate is None:
            raise RuntimeError("135 baseline evaluation failed")
    return entries, footer_lines, baseline_candidate, board_data
