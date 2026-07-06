#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

GA_DIR = Path(__file__).resolve().parent
RL_DIR = GA_DIR.parent / "rl"
sys.path.insert(0, str(RL_DIR))
sys.path.insert(0, str(GA_DIR))

from explain_135 import generate_explanation
import rl_135_core as core
import routing_eval_135 as eval135
from routing_env_135 import OP_EARLIER, OP_LATER, OP_RESTORE, OP_SWAP_LAYER_BASE, Windows135RoutingEnv


@dataclass
class Individual:
    entries: list[core.OrderEntry]
    candidate: eval135.RouteCandidate | None
    score: tuple[float, float, float, float]
    source: str


@dataclass
class Summary:
    tag: str
    variant: str
    seed: int
    algorithm: str
    eval_budget: int
    eval_used: int
    stop_completion_rate: float
    early_stop_reached: bool
    early_stop_eval: int | None
    population_size: int
    elite_size: int
    mutation_rate: float
    crossover_rate: float
    local_search_rate: float
    generations: int
    deterministic_evals: int
    elapsed_seconds: float
    reached_full: bool
    first_full_eval: int | None
    best_partial: dict[str, Any]
    best_full: dict[str, Any] | None
    best_wire_full: dict[str, Any] | None
    best_via_full: dict[str, Any] | None
    fixed_csv: str | None
    fixed_net_count: int
    initial_order: str | None
    initial_source: str
    primary_best_kind: str
    primary_best_order_txt: str | None
    initial_layer_net_counts: dict[str, int]


def clone_entries(entries: list[core.OrderEntry]) -> list[core.OrderEntry]:
    return [core.OrderEntry(entry.net, entry.layer, entry.order) for entry in entries]


def load_fixed_targets(
    csv_path: Path | None,
    baseline_entries: list[core.OrderEntry],
    layer_names: list[str],
) -> dict[str, tuple[str, int]]:
    if csv_path is None:
        return {}

    net_to_entry = {entry.net: entry for entry in baseline_entries}
    required_fields = {"net", "layer", "order"}
    valid_layers = set(layer_names)
    fixed_targets: dict[str, tuple[str, int]] = {}
    fixed_slots: dict[tuple[str, int], str] = {}
    with csv_path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path} is empty; expected CSV header: net,layer,order")
        missing_fields = required_fields - set(reader.fieldnames)
        if missing_fields:
            raise ValueError(f"{csv_path} is missing required columns: {', '.join(sorted(missing_fields))}")
        for line_no, row in enumerate(reader, start=2):
            net = (row.get("net") or "").strip()
            layer = (row.get("layer") or "").strip()
            order_raw = (row.get("order") or "").strip()
            if not net and not layer and not order_raw:
                continue
            if not net or not layer or not order_raw:
                raise ValueError(f"{csv_path}:{line_no} must provide net, layer, and order")
            if net not in net_to_entry:
                raise ValueError(f"{csv_path}:{line_no} references unknown net {net}")
            if layer not in valid_layers:
                raise ValueError(
                    f"{csv_path}:{line_no} uses unknown layer {layer!r}; "
                    f"expected one of {', '.join(layer_names)}"
                )
            try:
                order = int(order_raw)
            except ValueError as exc:
                raise ValueError(f"{csv_path}:{line_no} has non-integer order {order_raw!r}") from exc
            if order <= 0:
                raise ValueError(f"{csv_path}:{line_no} order must be positive, got {order}")

            slot = (layer, order)
            if net in fixed_targets:
                raise ValueError(f"{csv_path}:{line_no} duplicates fixed net {net}")
            if slot in fixed_slots:
                raise ValueError(
                    f"{csv_path}:{line_no} conflicts with fixed net {fixed_slots[slot]} "
                    f"at {layer},{order}"
                )
            fixed_targets[net] = slot
            fixed_slots[slot] = net
    return fixed_targets


def load_initial_entries(
    order_path: Path | None,
    baseline_entries: list[core.OrderEntry],
    layer_names: list[str],
) -> list[core.OrderEntry]:
    if order_path is None:
        return clone_entries(baseline_entries)

    entries, _footer_lines, _input_layer_names = core.read_order_entries(order_path.expanduser())
    if not entries:
        raise ValueError(f"{order_path} does not contain any order rows")

    baseline_nets = {entry.net for entry in baseline_entries}
    valid_layers = set(layer_names)
    seen_nets: set[str] = set()
    seen_slots: dict[tuple[str, int], str] = {}
    for entry in entries:
        if entry.net in seen_nets:
            raise ValueError(f"{order_path} duplicates net {entry.net}")
        if entry.net not in baseline_nets:
            raise ValueError(f"{order_path} references unknown net {entry.net}")
        if entry.layer not in valid_layers:
            raise ValueError(
                f"{order_path} uses unknown layer {entry.layer!r}; "
                f"expected one of {', '.join(layer_names)}"
            )
        if entry.order <= 0:
            raise ValueError(f"{order_path} has non-positive order {entry.order} for {entry.net}")
        slot = (entry.layer, entry.order)
        if slot in seen_slots:
            raise ValueError(
                f"{order_path} assigns both {seen_slots[slot]} and {entry.net} to "
                f"{entry.layer},{entry.order}"
            )
        seen_nets.add(entry.net)
        seen_slots[slot] = entry.net

    missing = sorted(baseline_nets - seen_nets)
    if missing:
        raise ValueError(f"{order_path} is missing {len(missing)} baseline nets, first missing: {missing[0]}")
    return core.canonicalize_entries(entries, layer_names)


def make_env(
    output_root: Path,
    board_data: core.BoardData,
    baseline_entries: list[core.OrderEntry],
    footer_lines: list[str],
    layer_names: list[str],
    baseline_stats: core.RouteStats,
    fixed_targets: dict[str, tuple[str, int]],
) -> Windows135RoutingEnv:
    return Windows135RoutingEnv(
        env_id=0,
        output_root=output_root,
        board_data=board_data,
        baseline_entries=baseline_entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
        baseline_stats=baseline_stats,
        max_episode_steps=1,
        missing_weight=10000.0,
        wire_weight=0.02,
        via_weight=0.0,
        full_bonus=1000.0,
        failure_penalty=5000.0,
        fixed_targets=fixed_targets,
    )


def prepare_initial_state(
    run_dir: Path,
    args: argparse.Namespace,
) -> tuple[
    list[core.OrderEntry],
    list[str],
    list[str],
    eval135.RouteCandidate,
    core.BoardData,
    dict[str, tuple[str, int]],
    str,
]:
    baseline_entries, footer_lines, layer_names, baseline_candidate, _board_data = eval135.prepare_baseline(run_dir)
    source_entries = load_initial_entries(args.initial_order, baseline_entries, layer_names)
    source_candidate = baseline_candidate
    initial_source = "baseline"
    if args.initial_order is not None:
        initial_source = "initial_order"
        if eval135.signature(source_entries) != eval135.signature(baseline_entries):
            evaluated = eval135.evaluate_entries(
                run_dir,
                footer_lines,
                layer_names,
                source_entries,
                "initial_order",
                f"initial_order:{args.initial_order}",
            )
            if evaluated is None:
                raise RuntimeError("Initial 135 Windows order evaluation failed")
            source_candidate = evaluated

    source_board_data = core.load_board_data(
        core.PROJECT_DIR,
        entries=source_entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    fixed_targets = load_fixed_targets(args.fixed_csv, source_entries, layer_names)
    if fixed_targets:
        initial_source = f"{initial_source}+fixed_csv"
        print(f"Loaded {len(fixed_targets)} fixed 135 Windows nets from {args.fixed_csv}")

    setup_env = make_env(
        run_dir,
        source_board_data,
        source_entries,
        footer_lines,
        layer_names,
        source_candidate.stats,
        fixed_targets,
    )
    initial_entries = clone_entries(setup_env.entries)
    initial_candidate = source_candidate
    if eval135.signature(initial_entries) != eval135.signature(source_entries):
        evaluated = eval135.evaluate_entries(
            run_dir,
            footer_lines,
            layer_names,
            initial_entries,
            "fixed_initial",
            "fixed_csv_initial",
        )
        if evaluated is None:
            raise RuntimeError("Fixed CSV initial 135 Windows routing evaluation failed")
        initial_candidate = evaluated

    initial_board_data = core.load_board_data(
        core.PROJECT_DIR,
        entries=initial_entries,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    return initial_entries, footer_lines, layer_names, initial_candidate, initial_board_data, fixed_targets, initial_source


def candidate_score(candidate: eval135.RouteCandidate | None) -> tuple[float, float, float, float]:
    if candidate is None:
        return (-1.0, -1.0, -1.0e18, -1.0e18)
    stats = candidate.stats
    return (
        float(stats.routed_nets),
        float(stats.completion_rate),
        -float(stats.total_wire_length),
        -float(stats.vias),
    )


def layer_entry_counts(entries: list[core.OrderEntry], layer_names: list[str]) -> dict[str, int]:
    return {layer: sum(1 for entry in entries if entry.layer == layer) for layer in layer_names}


def same_layer_counts(
    entries: list[core.OrderEntry],
    layer_names: list[str],
    expected_counts: dict[str, int],
) -> bool:
    return layer_entry_counts(entries, layer_names) == expected_counts


def action_for_entry(env: Windows135RoutingEnv, net: str, op: int) -> int | None:
    for index, entry in enumerate(env.entries):
        if entry.net == net:
            return index * env.n_ops + op
    return None


def apply_action_if_legal(
    env: Windows135RoutingEnv,
    entries: list[core.OrderEntry],
    net: str,
    op: int,
    expected_counts: dict[str, int],
) -> tuple[list[core.OrderEntry], bool]:
    env.entries = env.normalize_orders(entries)
    flat_action = action_for_entry(env, net, op)
    if flat_action is None:
        return clone_entries(entries), False
    mask = env.get_action_mask()
    if flat_action >= len(mask) or mask[flat_action] <= 0:
        return clone_entries(entries), False
    updated = env.apply_action(flat_action)
    if not same_layer_counts(updated, env.layer_names, expected_counts):
        return clone_entries(entries), False
    return updated, True


def legal_count_preserving_actions(
    env: Windows135RoutingEnv,
    entries: list[core.OrderEntry],
    expected_counts: dict[str, int],
) -> list[int]:
    env.entries = env.normalize_orders(entries)
    mask = env.get_action_mask()
    actions: list[int] = []
    for flat_action, allowed in enumerate(mask):
        if allowed <= 0:
            continue
        updated = env.apply_action(flat_action)
        if same_layer_counts(updated, env.layer_names, expected_counts):
            actions.append(flat_action)
    return actions


def mutate_entries(
    entries: list[core.OrderEntry],
    env: Windows135RoutingEnv,
    rng: random.Random,
    steps: int,
    expected_counts: dict[str, int],
) -> list[core.OrderEntry]:
    current = clone_entries(entries)
    for _ in range(max(0, steps)):
        actions = legal_count_preserving_actions(env, current, expected_counts)
        if not actions:
            break
        env.entries = env.normalize_orders(current)
        updated = env.apply_action(rng.choice(actions))
        if same_layer_counts(updated, env.layer_names, expected_counts):
            current = updated
    return env.normalize_orders(current)


def random_perturbation_steps(item_count: int, rng: random.Random) -> int:
    return rng.randint(1, max(4, int(0.1 * item_count)))


def adaptive_perturbation_steps(item_count: int, eval_used: int, eval_budget: int, rng: random.Random) -> int:
    if eval_budget <= 0:
        return 1
    progress = eval_used / eval_budget
    if progress < 0.35:
        hi = max(4, int(0.08 * item_count))
    elif progress < 0.75:
        hi = max(8, int(0.16 * item_count))
    else:
        hi = max(12, int(0.24 * item_count))
    return rng.randint(1, hi)


def action_priority(env: Windows135RoutingEnv, flat_action: int, focus_nets: set[str]) -> tuple[int, int, int]:
    entry_idx = flat_action // env.n_ops
    op = flat_action % env.n_ops
    entry = env.entries[entry_idx]
    focus = 0 if entry.net in focus_nets else 1
    if op >= OP_SWAP_LAYER_BASE:
        op_rank = 0
    else:
        op_rank = {OP_EARLIER: 1, OP_LATER: 2, OP_RESTORE: 3}.get(op, 4)
    return (focus, op_rank, entry.order)


def seed_swap_entry_probes(
    baseline_entries: list[core.OrderEntry],
    layer_names: list[str],
    rng: random.Random,
    limit: int,
    fixed_nets: set[str],
    mode: str,
) -> list[tuple[list[core.OrderEntry], str]]:
    if limit <= 0:
        return []
    max_order = max((entry.order for entry in baseline_entries), default=0)
    swap_specs: list[tuple[int, str, str]] = []
    for order in range(1, max_order + 1):
        for left_idx, left_layer in enumerate(layer_names):
            for right_layer in layer_names[left_idx + 1 :]:
                swap_specs.append((order, left_layer, right_layer))
    if mode == "random":
        rng.shuffle(swap_specs)

    probes: list[tuple[list[core.OrderEntry], str]] = []
    for order, left_layer, right_layer in swap_specs:
        if len(probes) >= limit:
            break
        entries = clone_entries(baseline_entries)
        left = [entry for entry in entries if entry.layer == left_layer and entry.order == order]
        right = [entry for entry in entries if entry.layer == right_layer and entry.order == order]
        if len(left) != 1 or len(right) != 1:
            continue
        if left[0].net in fixed_nets or right[0].net in fixed_nets:
            continue
        left[0].layer, right[0].layer = right[0].layer, left[0].layer
        probes.append((core.canonicalize_entries(entries, layer_names), f"seed_swap:{left_layer}<->{right_layer}:slot{order}"))
    return probes


def deterministic_action_entry_probes(
    entries: list[core.OrderEntry],
    env: Windows135RoutingEnv,
    focus_nets: set[str],
    expected_counts: dict[str, int],
    limit: int,
) -> list[tuple[list[core.OrderEntry], str]]:
    if limit <= 0:
        return []
    env.entries = env.normalize_orders(entries)
    actions = legal_count_preserving_actions(env, env.entries, expected_counts)
    actions.sort(key=lambda action: action_priority(env, action, focus_nets))
    probes: list[tuple[list[core.OrderEntry], str]] = []
    seen: set[tuple[tuple[str, str, int], ...]] = set()
    for action in actions:
        env.entries = env.normalize_orders(entries)
        candidate_entries = env.apply_action(action)
        if not same_layer_counts(candidate_entries, env.layer_names, expected_counts):
            continue
        signature = eval135.signature(candidate_entries)
        if signature in seen:
            continue
        seen.add(signature)
        entry = env.entries[action // env.n_ops]
        op = action % env.n_ops
        probes.append((candidate_entries, f"deterministic_probe:{entry.net}:op{op}"))
        if len(probes) >= limit:
            break
    return probes


def crossover_entries(
    parent_a: list[core.OrderEntry],
    parent_b: list[core.OrderEntry],
    env: Windows135RoutingEnv,
    rng: random.Random,
    expected_counts: dict[str, int],
) -> list[core.OrderEntry]:
    current = env.normalize_orders(parent_a)
    target_by_net = {entry.net: entry for entry in parent_b}
    layer_index = {layer: index for index, layer in enumerate(env.layer_names)}
    changed_nets = [
        entry.net
        for entry in current
        if entry.net not in env.fixed_nets
        and entry.net in target_by_net
        and (entry.layer, entry.order) != (target_by_net[entry.net].layer, target_by_net[entry.net].order)
    ]
    rng.shuffle(changed_nets)
    if not changed_nets:
        return current

    target_count = rng.randint(1, max(1, min(len(changed_nets), max(2, len(changed_nets) // 3))))
    for net in changed_nets[:target_count]:
        for _ in range(len(parent_a) * 2):
            env.entries = env.normalize_orders(current)
            by_net = {entry.net: entry for entry in env.entries}
            current_entry = by_net.get(net)
            target_entry = target_by_net.get(net)
            if current_entry is None or target_entry is None:
                break
            if (current_entry.layer, current_entry.order) == (target_entry.layer, target_entry.order):
                break
            if current_entry.layer != target_entry.layer:
                target_layer_index = layer_index[target_entry.layer]
                current, changed = apply_action_if_legal(
                    env,
                    current,
                    net,
                    OP_SWAP_LAYER_BASE + target_layer_index,
                    expected_counts,
                )
                if changed:
                    continue
            op = OP_EARLIER if current_entry.order > target_entry.order else OP_LATER
            current, changed = apply_action_if_legal(env, current, net, op, expected_counts)
            if not changed:
                break
    return env.normalize_orders(current)


def evaluate_individual(
    run_dir: Path,
    footer_lines: list[str],
    layer_names: list[str],
    entries: list[core.OrderEntry],
    source: str,
    eval_index: int,
    cache: dict[tuple[tuple[str, str, int], ...], eval135.RouteCandidate | None],
) -> tuple[Individual, bool]:
    normalized = core.canonicalize_entries(entries, layer_names)
    signature = eval135.signature(normalized)
    if signature in cache:
        candidate = cache[signature]
        return Individual(normalized, candidate, candidate_score(candidate), source), False

    candidate = eval135.evaluate_entries(
        run_dir,
        footer_lines,
        layer_names,
        normalized,
        f"ga_{eval_index:05d}",
        source,
    )
    cache[signature] = candidate
    return Individual(normalized, candidate, candidate_score(candidate), source), True


def tournament(population: list[Individual], rng: random.Random, k: int = 3) -> Individual:
    sample = rng.sample(population, k=min(k, len(population)))
    return max(sample, key=lambda item: item.score)


def update_best_135(
    candidate: eval135.RouteCandidate | None,
    eval_index: int,
    best_partial: eval135.RouteCandidate,
    best_full: eval135.RouteCandidate | None,
    best_wire: eval135.RouteCandidate | None,
    best_via: eval135.RouteCandidate | None,
    first_full_eval: int | None,
) -> tuple[eval135.RouteCandidate, eval135.RouteCandidate | None, eval135.RouteCandidate | None, eval135.RouteCandidate | None, int | None]:
    if candidate is None:
        return best_partial, best_full, best_wire, best_via, first_full_eval
    if eval135.partial_key(candidate) > eval135.partial_key(best_partial):
        best_partial = candidate
    if candidate.stats.routed_nets == candidate.stats.total_nets:
        if first_full_eval is None:
            first_full_eval = eval_index
        if best_full is None or eval135.full_key(candidate) > eval135.full_key(best_full):
            best_full = candidate
        if best_wire is None or (candidate.stats.total_wire_length, candidate.stats.vias) < (
            best_wire.stats.total_wire_length,
            best_wire.stats.vias,
        ):
            best_wire = candidate
        if best_via is None or (candidate.stats.vias, candidate.stats.total_wire_length) < (
            best_via.stats.vias,
            best_via.stats.total_wire_length,
        ):
            best_via = candidate
    return best_partial, best_full, best_wire, best_via, first_full_eval


def choose_primary_best(
    best_full: eval135.RouteCandidate | None,
    best_partial: eval135.RouteCandidate,
) -> tuple[str, eval135.RouteCandidate]:
    if best_full is not None:
        return "best_full", best_full
    return "best_partial", best_partial


def export_order_txt(candidate: eval135.RouteCandidate | None, dest: Path) -> str | None:
    if candidate is None or candidate.run_dir is None:
        return None
    src = Path(candidate.run_dir) / core.BASE_ORDER
    if not src.exists():
        return None
    shutil.copy2(src, dest)
    return dest.name


def validate_args(args: argparse.Namespace) -> None:
    if args.eval_budget < 0:
        raise ValueError("--eval-budget must be non-negative")
    if args.stop_completion_rate < 0.0 or args.stop_completion_rate > 1.0:
        raise ValueError("--stop-completion-rate must be between 0 and 1")
    if args.population_size <= 0:
        raise ValueError("--population-size must be positive")
    if args.elite_size <= 0:
        raise ValueError("--elite-size must be positive")
    if args.elite_size > args.population_size:
        raise ValueError("--elite-size cannot exceed --population-size")
    for name in ("mutation_rate", "crossover_rate", "local_search_rate"):
        value = getattr(args, name)
        if value < 0.0 or value > 1.0:
            raise ValueError(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.deterministic_probe_fraction < 0.0 or args.deterministic_probe_fraction > 1.0:
        raise ValueError("--deterministic-probe-fraction must be between 0 and 1")


def completion_target_reached(candidate: eval135.RouteCandidate | None, args: argparse.Namespace) -> bool:
    return (
        args.stop_completion_rate > 0.0
        and candidate is not None
        and candidate.stats.completion_rate >= args.stop_completion_rate
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genetic/local search optimizer for the Windows 135 routing flow.")
    parser.add_argument("--stop-completion-rate", type=float, default=1.0, help="Stop once best completion_rate reaches this value; use 0 to disable early stop.")
    parser.add_argument("--eval-budget", type=int, default=200)
    parser.add_argument("--algorithm", choices=("ga",), default="ga", help=argparse.SUPPRESS)
    parser.add_argument("--population-size", type=int, default=32)
    parser.add_argument("--elite-size", type=int, default=4)
    parser.add_argument("--deterministic-probe-fraction", type=float, default=0.25)
    parser.add_argument("--seed-swap-mode", choices=("random", "ordered"), default="random")
    parser.add_argument("--mutation-rate", type=float, default=0.35)
    parser.add_argument("--crossover-rate", type=float, default=0.60)
    parser.add_argument("--local-search-rate", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20260425)
    parser.add_argument("--tag", default="ga_135_windows")
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parent / "search_runs")
    parser.add_argument("--no-explain", action="store_true", help="Skip deterministic explanation.md generation.")
    parser.add_argument(
        "--fixed-csv",
        type=Path,
        default=None,
        help="Optional CSV with net,layer,order rows. Each row locks one net.",
    )
    parser.add_argument(
        "--initial-order",
        type=Path,
        default=None,
        help="Optional complete order_input.txt-style file to use as the GA initial layer/order state.",
    )
    return parser.parse_args()


def train(args: argparse.Namespace) -> Summary:
    start_time = time.monotonic()
    validate_args(args)
    rng = random.Random(args.seed)
    run_dir = args.output_root / f"{args.tag}_ga_layer_order_seed{args.seed}_budget{args.eval_budget}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    (
        initial_entries,
        footer_lines,
        layer_names,
        initial_candidate,
        board_data,
        fixed_targets,
        initial_source,
    ) = prepare_initial_state(run_dir, args)
    initial_counts = layer_entry_counts(initial_entries, layer_names)
    env = make_env(run_dir, board_data, initial_entries, footer_lines, layer_names, initial_candidate.stats, fixed_targets)

    cache: dict[tuple[tuple[str, str, int], ...], eval135.RouteCandidate | None] = {
        eval135.signature(initial_entries): initial_candidate,
    }
    eval_used = 0
    generations = 0
    initial_individual = Individual(
        clone_entries(initial_entries),
        initial_candidate,
        candidate_score(initial_candidate),
        initial_source,
    )
    population: list[Individual] = [initial_individual]
    population_signatures = {eval135.signature(initial_entries)}
    best_partial = initial_candidate
    best_full = initial_candidate if initial_candidate.stats.routed_nets == initial_candidate.stats.total_nets else None
    best_wire = best_full
    best_via = best_full
    first_full_eval = 0 if best_full is not None else None
    deterministic_evals = 0
    early_stop_eval = 0 if completion_target_reached(best_partial, args) else None

    def add_individual(individual: Individual, consumed: bool) -> None:
        nonlocal eval_used, best_partial, best_full, best_wire, early_stop_eval, best_via, first_full_eval
        if consumed:
            eval_used += 1
        signature = eval135.signature(individual.entries)
        if signature not in population_signatures:
            population.append(individual)
            population_signatures.add(signature)
        best_partial, best_full, best_wire, best_via, first_full_eval = update_best_135(
            individual.candidate,
            eval_used,
            best_partial,
            best_full,
            best_wire,
            best_via,
            first_full_eval,
        )
        if early_stop_eval is None and completion_target_reached(best_partial, args):
            early_stop_eval = eval_used

    focus_nets = set(initial_candidate.missing_nets)
    if focus_nets:
        by_net = {entry.net: entry for entry in initial_entries}
        for missing_net in list(focus_nets):
            entry = by_net.get(missing_net)
            if entry is None:
                continue
            for neighbor in initial_entries:
                if neighbor.layer == entry.layer and abs(neighbor.order - entry.order) <= 3:
                    focus_nets.add(neighbor.net)

    probe_fraction = min(args.deterministic_probe_fraction, 0.15)
    probe_limit = min(args.eval_budget, max(0, int(args.eval_budget * probe_fraction)))
    seed_rng = random.Random(args.seed + 13579)
    seed_probe_limit = max(0, probe_limit // 2)
    probes = seed_swap_entry_probes(
        initial_entries,
        layer_names,
        seed_rng,
        seed_probe_limit,
        set(fixed_targets),
        args.seed_swap_mode,
    )
    probes.extend(
        deterministic_action_entry_probes(
            initial_entries,
            env,
            focus_nets,
            initial_counts,
            max(0, probe_limit - len(probes)),
        )
    )
    for entries, source in probes:
        if eval_used >= args.eval_budget or early_stop_eval is not None:
            break
        if not same_layer_counts(entries, layer_names, initial_counts):
            continue
        signature = eval135.signature(core.canonicalize_entries(entries, layer_names))
        if signature in population_signatures:
            continue
        individual, consumed = evaluate_individual(
            run_dir,
            footer_lines,
            layer_names,
            entries,
            source,
            eval_used + 1,
            cache,
        )
        if consumed:
            deterministic_evals += 1
        add_individual(individual, consumed)

    attempts = 0
    while len(population) < args.population_size and eval_used < args.eval_budget and attempts < args.population_size * 20:
        attempts += 1
        steps = adaptive_perturbation_steps(len(initial_entries), eval_used, args.eval_budget, rng)
        entries = mutate_entries(initial_entries, env, rng, steps, initial_counts)
        signature = eval135.signature(entries)
        if signature in population_signatures:
            continue
        individual, consumed = evaluate_individual(
            run_dir,
            footer_lines,
            layer_names,
            entries,
            f"ga_initial_mutation:{steps}",
            eval_used + 1,
            cache,
        )
        add_individual(individual, consumed)

    ga_stagnant_generations = 0
    while eval_used < args.eval_budget and early_stop_eval is None:
        generations += 1
        eval_at_generation_start = eval_used
        population.sort(key=lambda item: item.score, reverse=True)
        previous_best_score = population[0].score
        next_population = population[: args.elite_size]
        next_signatures = {eval135.signature(individual.entries) for individual in next_population}

        for elite in population[: args.elite_size]:
            if eval_used >= args.eval_budget or early_stop_eval is not None:
                break
            if rng.random() > args.local_search_rate:
                continue
            steps = rng.randint(1, 2)
            entries = mutate_entries(elite.entries, env, rng, steps, initial_counts)
            signature = eval135.signature(entries)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
                layer_names,
                entries,
                f"ga_local_search:{elite.source}:{steps}",
                eval_used + 1,
                cache,
            )
            next_population.append(individual)
            next_signatures.add(signature)
            add_individual(individual, consumed)

        attempts = 0
        max_attempts = args.population_size * 30
        while len(next_population) < args.population_size and eval_used < args.eval_budget and attempts < max_attempts:
            attempts += 1
            parent_a = tournament(population, rng)
            parent_b = tournament(population, rng)
            if rng.random() < args.crossover_rate:
                entries = crossover_entries(parent_a.entries, parent_b.entries, env, rng, initial_counts)
                source = f"ga_crossover:{parent_a.source}:{parent_b.source}"
            else:
                entries = clone_entries(parent_a.entries)
                source = f"ga_clone:{parent_a.source}"

            if rng.random() < args.mutation_rate or eval135.signature(entries) == eval135.signature(parent_a.entries):
                steps = adaptive_perturbation_steps(len(entries), eval_used, args.eval_budget, rng)
                entries = mutate_entries(entries, env, rng, steps, initial_counts)
                source = f"{source}:mut{steps}"
            else:
                entries = env.normalize_orders(entries)

            if not same_layer_counts(entries, layer_names, initial_counts):
                continue
            signature = eval135.signature(entries)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
                layer_names,
                entries,
                source,
                eval_used + 1,
                cache,
            )
            next_population.append(individual)
            next_signatures.add(signature)
            add_individual(individual, consumed)

        next_population.sort(key=lambda item: item.score, reverse=True)
        restart_added = 0
        if next_population and next_population[0].score <= previous_best_score:
            ga_stagnant_generations += 1
        else:
            ga_stagnant_generations = 0

        should_restart = ga_stagnant_generations >= 3 or eval_used == eval_at_generation_start
        restart_target = max(1, args.population_size // 4)
        restart_attempts = 0
        while (
            should_restart
            and restart_added < restart_target
            and eval_used < args.eval_budget
        and early_stop_eval is None
            and restart_attempts < args.population_size * 20
        ):
            restart_attempts += 1
            base_entries = initial_entries if rng.random() < 0.65 else next_population[0].entries
            steps = adaptive_perturbation_steps(len(base_entries), eval_used, args.eval_budget, rng)
            steps += random_perturbation_steps(len(base_entries), rng)
            entries = mutate_entries(base_entries, env, rng, steps, initial_counts)
            if not same_layer_counts(entries, layer_names, initial_counts):
                continue
            if not env.fixed_constraints_preserved(entries):
                continue
            signature = eval135.signature(entries)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
                layer_names,
                entries,
                f"ga_diversity_restart:mut{steps}",
                eval_used + 1,
                cache,
            )
            next_population.append(individual)
            next_signatures.add(signature)
            add_individual(individual, consumed)
            if consumed:
                restart_added += 1
        if restart_added:
            ga_stagnant_generations = 0
            next_population.sort(key=lambda item: item.score, reverse=True)

        population = next_population[: args.population_size]
        if eval_used == eval_at_generation_start and restart_added == 0:
            break

    eval135.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval135.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval135.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    eval135.save_artifacts(best_via, run_dir / "best_via_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = Summary(
        tag=args.tag,
        variant="ga_layer_order_search",
        seed=args.seed,
        algorithm="ga",
        eval_budget=args.eval_budget,
        eval_used=eval_used,
        stop_completion_rate=args.stop_completion_rate,
        early_stop_reached=early_stop_eval is not None,
        early_stop_eval=early_stop_eval,
        population_size=args.population_size,
        elite_size=args.elite_size,
        mutation_rate=args.mutation_rate,
        crossover_rate=args.crossover_rate,
        local_search_rate=args.local_search_rate,
        generations=generations,
        deterministic_evals=deterministic_evals,
        elapsed_seconds=time.monotonic() - start_time,
        reached_full=best_full is not None,
        first_full_eval=first_full_eval,
        best_partial=eval135.candidate_to_json(best_partial),
        best_full=eval135.candidate_to_json(best_full),
        best_wire_full=eval135.candidate_to_json(best_wire),
        best_via_full=eval135.candidate_to_json(best_via),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_net_count=len(fixed_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
        initial_layer_net_counts=initial_counts,
    )
    (run_dir / "summary.json").write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    print(json.dumps(asdict(summary), indent=2))
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, initial_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    print(f"135 Windows GA layer/order run written to: {run_dir}")
    return summary


def main() -> None:
    train(parse_args())


if __name__ == "__main__":
    main()
