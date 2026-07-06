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
    layer_names: list[str] | None = None


_SAVED_ARTIFACTS: dict[tuple[str, tuple[object, ...]], Path] = {}


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _link_or_copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _link_artifact_tree(src_dir: Path, dst_dir: Path) -> None:
    _remove_path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for root, dir_names, file_names in os.walk(src_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(src_dir)
        target_root = dst_dir / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for dir_name in dir_names:
            (target_root / dir_name).mkdir(exist_ok=True)
        for file_name in file_names:
            _link_or_copy_file(root_path / file_name, target_root / file_name)


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
        "layer_names": candidate.layer_names,
        "run_dir": run_dir,
    }


def signature(pairs: list[PairEntry]) -> tuple[tuple[str, str, int], ...]:
    return core.pair_signature(pairs)


def _candidate_artifact_key(candidate: RouteCandidate, run_final_turn: bool) -> tuple[object, ...]:
    stats = candidate.stats
    return (
        signature(candidate.pairs),
        tuple(candidate.missing_nets),
        tuple(candidate.layer_names or ()),
        run_final_turn,
        stats.total_nets,
        stats.routed_nets,
        round(stats.total_wire_length, 6),
        stats.line_segments,
        stats.arc_segments,
        stats.vias,
    )


def _run_c(work_dir: Path) -> int:
    return core.run_command(
        ["./arc_main.exe", core.BASE_ORDER, core.BASE_LAYOUT, core.BASE_CONSTRAIN],
        cwd=work_dir,
        log_path=work_dir / "main.log",
    )


def run_turn(work_dir: Path) -> int:
    return 0


def evaluate_pairs(
    output_root: Path,
    footer_lines: list[str],
    layer_names: list[str],
    pairs: list[PairEntry],
    run_name: str,
    source: str,
    keep_failed: bool = False,
    run_final_turn: bool = False,
    keep_artifacts: bool = False,
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
        if not (keep_artifacts or keep_failed):
            shutil.rmtree(run_dir)
        return None

    stats = core.compute_stats(run_dir)
    missing_nets = core.read_missing_nets(run_dir)
    artifact_dir = run_dir if keep_artifacts else None
    if keep_artifacts or keep_failed:
        core.write_json(
            run_dir / "result.json",
            {
                "name": run_name,
                "source": source,
                "stats": asdict(stats),
                "missing_nets": missing_nets,
                "run_dir": str(run_dir) if keep_artifacts else None,
            },
        )
    if not keep_artifacts:
        shutil.rmtree(run_dir)
    return RouteCandidate(run_name, source, core.clone_pairs(pairs), stats, artifact_dir, missing_nets, list(layer_names))


def prepare_baseline(output_root: Path | None = None) -> tuple[list[PairEntry], list[str], list[str], RouteCandidate, core.BoardData]:
    board_data = core.load_board_data(core.PROJECT_DIR)
    pairs = core.clone_pairs(board_data.pairs)
    if output_root is None:
        work_dir = core.PROJECT_DIR
        stats = core.compute_stats(work_dir)
        missing = core.read_missing_nets(work_dir)
        candidate = RouteCandidate("baseline", "baseline", pairs, stats, work_dir, missing, list(board_data.layer_names))
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
        keep_artifacts=False,
    )
    if candidate is None:
        raise RuntimeError("Arc exe baseline evaluation failed")
    return pairs, board_data.footer_lines, board_data.layer_names, candidate, board_data


def partial_key(candidate: RouteCandidate) -> tuple[int, float]:
    return (candidate.stats.routed_nets, -candidate.stats.total_wire_length)


def full_key(candidate: RouteCandidate) -> tuple[float, int]:
    return (-candidate.stats.total_wire_length, -candidate.stats.vias)


def write_candidate_artifacts(
    candidate: RouteCandidate,
    output_dir: Path,
    footer_lines: list[str],
    layer_names: list[str],
    *,
    run_final_turn: bool = False,
) -> bool:
    _remove_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    keep_files = [
        core.BASE_ORDER,
        core.BASE_ARC_OUTPUT,
        core.BASE_CONSTRAIN,
        core.BASE_NETLIST,
        core.BASE_PINS,
        core.BASE_LAYOUT,
        "main.log",
        "parameter.txt",
        "data.txt",
        "arc_main.exe",
        "result.json",
    ]

    if candidate.run_dir is None:
        core.copy_inputs(output_dir)
        core.write_order_pairs(output_dir / core.BASE_ORDER, candidate.pairs, footer_lines, layer_names)
        c_return_code = _run_c(output_dir)
        if c_return_code != 0:
            return False
        if run_final_turn and run_turn(output_dir) != 0:
            return False
    else:
        source_dir = Path(candidate.run_dir)
        for name in keep_files:
            src = source_dir / name
            if src.exists():
                _link_or_copy_file(src, output_dir / name)
        if run_final_turn and run_turn(output_dir) != 0:
            return False

    order_path = output_dir / core.BASE_ORDER
    if order_path.exists():
        _link_or_copy_file(order_path, output_dir / "layer_order.txt")
    return True


def save_artifacts(candidate: RouteCandidate | None, dest: Path, run_final_turn: bool = False) -> None:
    if candidate is None:
        return
    cache_key = (str(dest.parent.resolve()), _candidate_artifact_key(candidate, run_final_turn))
    cached_dest = _SAVED_ARTIFACTS.get(cache_key)
    if cached_dest is not None and cached_dest.exists() and cached_dest.resolve() != dest.resolve():
        _link_artifact_tree(cached_dest, dest)
        return

    board_data = core.load_board_data(core.PROJECT_DIR)
    write_candidate_artifacts(
        candidate,
        dest,
        board_data.footer_lines,
        board_data.layer_names,
        run_final_turn=run_final_turn,
    )
    core.write_json(dest / "candidate_meta.json", candidate_to_json(candidate))
    _SAVED_ARTIFACTS[cache_key] = dest


def pairs_to_layer_array(pairs: list[PairEntry], layer_names: list[str]) -> np.ndarray:
    index = {layer: i for i, layer in enumerate(layer_names)}
    return np.asarray([index[p.layer] for p in pairs], dtype=np.int64)
