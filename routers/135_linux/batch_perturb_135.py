#!/usr/bin/env python3

import argparse
import csv
import json
import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
F_BIN = SCRIPT_DIR / "f.out"
TURN_SCRIPT = SCRIPT_DIR / "Turn_135_QYF.py"

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
    "d.out",
    "e.out",
    "f.out",
    "main",
]


@dataclass
class RunResult:
    run_name: str
    seed: int
    layer_swap_pairs: int
    top_order_shuffle_count: int
    bottom_order_shuffle_count: int
    return_code_f: int
    return_code_turn: int
    total_nets: int
    routed_nets: int
    completion_rate: float
    total_wire_length: float
    line_segments: int
    arc_segments: int
    vias: int
    line_out_bytes: int
    output_bytes: int
    success: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-run the 135 routing flow after random layer/order perturbations."
    )
    parser.add_argument("--runs", type=int, default=100, help="Number of perturbed runs.")
    parser.add_argument(
        "--seed",
        type=int,
        default=20260412,
        help="Base random seed for reproducible perturbations.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=SCRIPT_DIR / "batch_results",
        help="Parent directory for batch outputs.",
    )
    parser.add_argument(
        "--tag",
        default="run",
        help="Optional tag appended to the batch directory name.",
    )
    return parser.parse_args()


def ensure_required_files() -> None:
    required = [F_BIN, TURN_SCRIPT]
    required.extend(SCRIPT_DIR / name for name in COPY_INPUTS)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required files:\n" + "\n".join(missing))


def read_order_entries(path: Path) -> list[dict]:
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) < 3:
                continue
            entries.append(
                {
                    "net": parts[0],
                    "layer": parts[1],
                    "order": int(parts[2]),
                }
            )
    return entries


def write_order_entries(path: Path, entries: list[dict]) -> None:
    top_entries = [entry for entry in entries if entry["layer"] == "TOP"]
    bottom_entries = [entry for entry in entries if entry["layer"] == "BOTTOM"]

    with path.open("w", encoding="utf-8") as handle:
        for entry in top_entries:
            handle.write(f'{entry["net"]} {entry["layer"]} {entry["order"]}\n')
        handle.write("\n")
        for entry in bottom_entries:
            handle.write(f'{entry["net"]} {entry["layer"]} {entry["order"]}\n')


def shuffle_orders(entries: list[dict], rng: random.Random) -> int:
    if len(entries) <= 1:
        return 0

    shuffle_count = rng.randint(1, min(20, len(entries)))
    chosen = rng.sample(entries, shuffle_count)
    shuffled_orders = [entry["order"] for entry in chosen]
    rng.shuffle(shuffled_orders)
    if all(entry["order"] == new_order for entry, new_order in zip(chosen, shuffled_orders)):
        shuffled_orders = shuffled_orders[1:] + shuffled_orders[:1]
    for entry, new_order in zip(chosen, shuffled_orders):
        entry["order"] = new_order
    return shuffle_count


def perturb_order(path: Path, rng: random.Random) -> dict:
    entries = read_order_entries(path)
    top_indices = [idx for idx, entry in enumerate(entries) if entry["layer"] == "TOP"]
    bottom_indices = [idx for idx, entry in enumerate(entries) if entry["layer"] == "BOTTOM"]

    swap_pairs = min(len(top_indices), len(bottom_indices))
    if swap_pairs:
        swap_pairs = rng.randint(1, min(20, swap_pairs))
        chosen_top = rng.sample(top_indices, swap_pairs)
        chosen_bottom = rng.sample(bottom_indices, swap_pairs)
        for top_idx, bottom_idx in zip(chosen_top, chosen_bottom):
            entries[top_idx]["layer"], entries[bottom_idx]["layer"] = (
                entries[bottom_idx]["layer"],
                entries[top_idx]["layer"],
            )
    else:
        swap_pairs = 0

    top_entries = [entry for entry in entries if entry["layer"] == "TOP"]
    bottom_entries = [entry for entry in entries if entry["layer"] == "BOTTOM"]

    top_shuffle = shuffle_orders(top_entries, rng)
    bottom_shuffle = shuffle_orders(bottom_entries, rng)

    top_entries.sort(key=lambda item: item["order"])
    bottom_entries.sort(key=lambda item: item["order"])
    write_order_entries(path, top_entries + bottom_entries)

    return {
        "layer_swap_pairs": swap_pairs,
        "top_order_shuffle_count": top_shuffle,
        "bottom_order_shuffle_count": bottom_shuffle,
    }


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


def compute_stats(work_dir: Path) -> dict:
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
    return {
        "total_nets": total_nets,
        "routed_nets": routed_nets_count,
        "completion_rate": routed_nets_count / total_nets if total_nets else 0.0,
        "total_wire_length": total_wire_length,
        "line_segments": line_segments,
        "arc_segments": arc_segments,
        "vias": vias,
        "line_out_bytes": line_path.stat().st_size,
        "output_bytes": output_path.stat().st_size,
    }


def copy_inputs(work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    for filename in COPY_INPUTS:
        shutil.copy2(SCRIPT_DIR / filename, work_dir / filename)
    for executable in ["d.out", "e.out", "f.out", "main"]:
        (work_dir / executable).chmod(0o755)


def write_json(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def run_single(base_seed: int, run_index: int, batch_dir: Path) -> RunResult:
    run_name = f"run_{run_index:03d}"
    run_dir = batch_dir / run_name
    copy_inputs(run_dir)

    seed = base_seed + run_index
    rng = random.Random(seed)
    perturbation = perturb_order(run_dir / BASE_ORDER, rng)
    write_json(run_dir / "perturbation.json", {"seed": seed, **perturbation})

    f_return_code = run_command(
        [str(run_dir / "f.out"), BASE_ORDER, BASE_LAYOUT],
        cwd=run_dir,
        log_path=run_dir / "f.log",
    )
    turn_return_code = 1
    if f_return_code == 0:
        turn_return_code = run_command(
            ["python3", str(TURN_SCRIPT), BASE_LAYOUT, "line.out", "output.txt"],
            cwd=run_dir,
            log_path=run_dir / "turn.log",
        )

    success = f_return_code == 0 and turn_return_code == 0
    stats = compute_stats(run_dir) if success else {
        "total_nets": 0,
        "routed_nets": 0,
        "completion_rate": 0.0,
        "total_wire_length": 0.0,
        "line_segments": 0,
        "arc_segments": 0,
        "vias": 0,
        "line_out_bytes": 0,
        "output_bytes": 0,
    }

    stats_payload = {
        "run_name": run_name,
        "seed": seed,
        "success": success,
        "return_code_f": f_return_code,
        "return_code_turn": turn_return_code,
        **perturbation,
        **stats,
    }
    write_json(run_dir / "stats.json", stats_payload)

    return RunResult(
        run_name=run_name,
        seed=seed,
        layer_swap_pairs=perturbation["layer_swap_pairs"],
        top_order_shuffle_count=perturbation["top_order_shuffle_count"],
        bottom_order_shuffle_count=perturbation["bottom_order_shuffle_count"],
        return_code_f=f_return_code,
        return_code_turn=turn_return_code,
        total_nets=stats["total_nets"],
        routed_nets=stats["routed_nets"],
        completion_rate=stats["completion_rate"],
        total_wire_length=stats["total_wire_length"],
        line_segments=stats["line_segments"],
        arc_segments=stats["arc_segments"],
        vias=stats["vias"],
        line_out_bytes=stats["line_out_bytes"],
        output_bytes=stats["output_bytes"],
        success=success,
    )


def write_summary_csv(path: Path, results: list[RunResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "run_name",
            "seed",
            "success",
            "layer_swap_pairs",
            "top_order_shuffle_count",
            "bottom_order_shuffle_count",
            "return_code_f",
            "return_code_turn",
            "total_nets",
            "routed_nets",
            "completion_rate",
            "total_wire_length",
            "line_segments",
            "arc_segments",
            "vias",
            "line_out_bytes",
            "output_bytes",
        ])
        for result in results:
            writer.writerow([
                result.run_name,
                result.seed,
                int(result.success),
                result.layer_swap_pairs,
                result.top_order_shuffle_count,
                result.bottom_order_shuffle_count,
                result.return_code_f,
                result.return_code_turn,
                result.total_nets,
                result.routed_nets,
                f"{result.completion_rate:.6f}",
                f"{result.total_wire_length:.6f}",
                result.line_segments,
                result.arc_segments,
                result.vias,
                result.line_out_bytes,
                result.output_bytes,
            ])


def build_aggregate(results: list[RunResult], batch_dir: Path, args: argparse.Namespace) -> dict:
    successes = [result for result in results if result.success]
    best = max(successes, key=lambda item: (item.completion_rate, -item.vias, -item.total_wire_length)) if successes else None
    worst = min(successes, key=lambda item: (item.completion_rate, -item.vias, item.total_wire_length)) if successes else None

    def maybe_average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    return {
        "batch_dir": str(batch_dir),
        "runs_requested": args.runs,
        "seed": args.seed,
        "successful_runs": len(successes),
        "failed_runs": len(results) - len(successes),
        "average_completion_rate": maybe_average([result.completion_rate for result in successes]),
        "average_total_wire_length": maybe_average([result.total_wire_length for result in successes]),
        "average_vias": maybe_average([result.vias for result in successes]),
        "best_run": best.run_name if best else None,
        "worst_run": worst.run_name if worst else None,
        "results": [result.__dict__ for result in results],
    }


def run_baseline(batch_dir: Path) -> None:
    baseline_dir = batch_dir / "baseline"
    copy_inputs(baseline_dir)
    f_return_code = run_command(
        [str(baseline_dir / "f.out"), BASE_ORDER, BASE_LAYOUT],
        cwd=baseline_dir,
        log_path=baseline_dir / "f.log",
    )
    if f_return_code != 0:
        raise RuntimeError(f"Baseline f.out run failed with code {f_return_code}")
    turn_return_code = run_command(
        ["python3", str(TURN_SCRIPT), BASE_LAYOUT, "line.out", "output.txt"],
        cwd=baseline_dir,
        log_path=baseline_dir / "turn.log",
    )
    if turn_return_code != 0:
        raise RuntimeError(f"Baseline Turn_135_QYF.py run failed with code {turn_return_code}")

    baseline_stats = compute_stats(baseline_dir)
    write_json(
        baseline_dir / "stats.json",
        {
            "run_name": "baseline",
            "seed": None,
            "success": True,
            "return_code_f": f_return_code,
            "return_code_turn": turn_return_code,
            **baseline_stats,
        },
    )


def main() -> None:
    args = parse_args()
    ensure_required_files()

    batch_dir = args.output_root / f"{args.tag}_seed{args.seed}_runs{args.runs}"
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    batch_dir.mkdir(parents=True)

    run_baseline(batch_dir)

    results: list[RunResult] = []
    for run_index in range(1, args.runs + 1):
        result = run_single(args.seed, run_index, batch_dir)
        results.append(result)
        print(
            f"[{run_index:03d}/{args.runs:03d}] "
            f"{result.run_name} success={int(result.success)} "
            f"completion={result.completion_rate:.4f} "
            f"vias={result.vias}"
        )

    write_summary_csv(batch_dir / "summary.csv", results)
    aggregate = build_aggregate(results, batch_dir, args)
    write_json(batch_dir / "summary.json", aggregate)
    print(f"Batch results written to: {batch_dir}")


if __name__ == "__main__":
    main()
