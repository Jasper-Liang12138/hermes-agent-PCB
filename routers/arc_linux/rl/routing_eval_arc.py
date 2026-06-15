#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

import rl_arc_core as core
from rl_arc_core import PairEntry, RouteStats


@dataclass
class RouteCandidate:
    name: str
    source: str
    pairs: list[PairEntry]
    stats: RouteStats
    run_dir: Path | None
    missing_nets: list[str]


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


def signature(pairs: list[PairEntry]) -> tuple[tuple[str, str, int], ...]:
    return core.pair_signature(pairs)


def _run_c(work_dir: Path) -> int:
    return core.run_command(
        [
            "./c.out",
            core.BASE_ORDER,
            core.BASE_LAYOUT,
            core.BASE_CONSTRAIN,
            core.BASE_COMPONENT,
        ],
        cwd=work_dir,
        log_path=work_dir / "c.log",
    )


def run_turn(work_dir: Path) -> int:
    return core.run_command(
        ["python3", "Turn_QYF.py", core.BASE_LAYOUT, core.BASE_ARC_OUTPUT, core.BASE_FINAL_OUTPUT],
        cwd=work_dir,
        log_path=work_dir / "turn.log",
    )


def evaluate_pairs(
    output_root: Path,
    footer_lines: list[str],
    layer_names: list[str],
    pairs: list[PairEntry],
    run_name: str,
    source: str,
    keep_failed: bool = False,
    run_final_turn: bool = False,
) -> RouteCandidate | None:
    run_dir = output_root / "episodes" / run_name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    core.copy_inputs(run_dir)
    core.write_order_pairs(run_dir / core.BASE_ORDER, pairs, footer_lines, layer_names)
    c_return_code = _run_c(run_dir)
    turn_return_code = 0
    if c_return_code == 0 and run_final_turn:
        turn_return_code = run_turn(run_dir)

    if c_return_code != 0 or turn_return_code != 0:
        if not keep_failed:
            shutil.rmtree(run_dir)
        return None

    stats = core.compute_stats(run_dir)
    missing_nets = core.read_missing_nets(run_dir)
    result = {
        "name": run_name,
        "source": source,
        "stats": asdict(stats),
        "missing_nets": missing_nets,
        "run_dir": str(run_dir),
    }
    core.write_json(run_dir / "result.json", result)
    return RouteCandidate(run_name, source, core.clone_pairs(pairs), stats, run_dir, missing_nets)


def prepare_baseline(output_root: Path | None = None) -> tuple[list[PairEntry], list[str], list[str], RouteCandidate, core.BoardData]:
    board_data = core.load_board_data(core.PROJECT_DIR)
    pairs = core.clone_pairs(board_data.pairs)
    if output_root is None:
        work_dir = core.PROJECT_DIR
        stats = core.compute_stats(work_dir)
        missing = core.read_missing_nets(work_dir)
        candidate = RouteCandidate("baseline", "baseline", pairs, stats, work_dir, missing)
        return pairs, board_data.footer_lines, board_data.layer_names, candidate, board_data

    candidate = evaluate_pairs(
        output_root=output_root,
        footer_lines=board_data.footer_lines,
        layer_names=board_data.layer_names,
        pairs=pairs,
        run_name="baseline",
        source="baseline",
        keep_failed=True,
        run_final_turn=False,
    )
    if candidate is None:
        raise RuntimeError("Arc baseline evaluation failed")
    return pairs, board_data.footer_lines, board_data.layer_names, candidate, board_data


def partial_key(candidate: RouteCandidate) -> tuple[int, float]:
    return (candidate.stats.routed_nets, -candidate.stats.total_wire_length)


def full_key(candidate: RouteCandidate) -> tuple[float, int]:
    return (-candidate.stats.total_wire_length, -candidate.stats.vias)


def save_artifacts(candidate: RouteCandidate | None, dest: Path, run_final_turn: bool = False) -> None:
    if candidate is None or candidate.run_dir is None:
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    source_dir = Path(candidate.run_dir)
    keep_files = [
        core.BASE_ORDER,
        core.BASE_ARC_OUTPUT,
        core.BASE_FINAL_OUTPUT,
        core.BASE_CONSTRAIN,
        core.BASE_COMPONENT,
        core.BASE_NETLIST,
        core.BASE_PINS,
        core.BASE_LAYOUT,
        "c.log",
        "turn.log",
        "parameter.txt",
        "data.txt",
        "get_parameter.py",
        "get_pins.py",
        "get_nets.py",
        "Turn_QYF.py",
        "c.out",
        "result.json",
    ]
    for name in keep_files:
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)
    if run_final_turn and not (dest / core.BASE_FINAL_OUTPUT).exists() and (dest / core.BASE_ARC_OUTPUT).exists():
        run_turn(dest)
    order_path = dest / core.BASE_ORDER
    if order_path.exists():
        shutil.copy2(order_path, dest / "layer_order.txt")
    core.write_json(dest / "candidate_meta.json", candidate_to_json(candidate))


def pairs_to_layer_array(pairs: list[PairEntry], layer_names: list[str]) -> np.ndarray:
    index = {layer: i for i, layer in enumerate(layer_names)}
    return np.asarray([index[p.layer] for p in pairs], dtype=np.int64)
