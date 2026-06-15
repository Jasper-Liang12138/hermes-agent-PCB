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
from routing_env_135 import (
    N_OPS,
    OP_EARLIER,
    OP_FLIP,
    OP_LATER,
    OP_RESTORE,
    CandidateRecord,
    RoutingLocalActionEnv,
    candidate_to_json,
    partial_key,
    save_artifacts,
)


@dataclass
class Individual:
    entries: list[core.OrderEntry]
    candidate: CandidateRecord | None
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


def clone_entries(entries: list[core.OrderEntry]) -> list[core.OrderEntry]:
    return [core.OrderEntry(e.net, e.layer, e.order) for e in entries]


def load_fixed_nets(csv_path: Path | None, baseline_entries: list[core.OrderEntry]) -> dict[str, tuple[str, int]]:
    if csv_path is None:
        return {}

    baseline_nets = {entry.net for entry in baseline_entries}
    valid_layers = {entry.layer for entry in baseline_entries}
    required_fields = {"net", "layer", "order"}
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
            if net in fixed_targets:
                raise ValueError(f"{csv_path}:{line_no} duplicates fixed net {net}")
            if net not in baseline_nets:
                raise ValueError(f"{csv_path}:{line_no} references unknown net {net}")
            if layer not in valid_layers:
                raise ValueError(
                    f"{csv_path}:{line_no} uses unknown layer {layer!r}; "
                    f"expected one of {', '.join(sorted(valid_layers))}"
                )
            try:
                order = int(order_raw)
            except ValueError as exc:
                raise ValueError(f"{csv_path}:{line_no} has non-integer order {order_raw!r}") from exc
            if order <= 0:
                raise ValueError(f"{csv_path}:{line_no} order must be positive, got {order}")
            slot = (layer, order)
            if slot in fixed_slots:
                raise ValueError(
                    f"{csv_path}:{line_no} conflicts with fixed net {fixed_slots[slot]} "
                    f"at {layer},{order}"
                )
            fixed_targets[net] = slot
            fixed_slots[slot] = net
    return fixed_targets


def load_initial_entries(order_path: Path | None, baseline_entries: list[core.OrderEntry]) -> list[core.OrderEntry]:
    if order_path is None:
        return clone_entries(baseline_entries)

    entries, _footer_lines = core.read_order_entries(order_path.expanduser())
    if not entries:
        raise ValueError(f"{order_path} does not contain any order rows")

    baseline_nets = {entry.net for entry in baseline_entries}
    baseline_index = {entry.net: index for index, entry in enumerate(baseline_entries)}
    valid_layers = {entry.layer for entry in baseline_entries}
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
                f"expected one of {', '.join(sorted(valid_layers))}"
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

    normalized: list[core.OrderEntry] = []
    layer_order = [layer for layer in ("TOP", "BOTTOM") if layer in valid_layers]
    layer_order.extend(sorted(valid_layers - set(layer_order)))
    for layer in layer_order:
        layer_entries = [entry for entry in entries if entry.layer == layer]
        layer_entries.sort(key=lambda item: (item.order, baseline_index[item.net]))
        for order, entry in enumerate(layer_entries, start=1):
            normalized.append(core.OrderEntry(entry.net, layer, order))
    return normalized


def make_env(
    output_root: Path,
    board_data: core.BoardData,
    baseline_entries: list[core.OrderEntry],
    footer_lines: list[str],
    baseline_stats: core.RouteStats,
    fixed_targets: dict[str, tuple[str, int]],
) -> RoutingLocalActionEnv:
    return RoutingLocalActionEnv(
        env_id=0,
        output_root=output_root,
        board_data=board_data,
        baseline_entries=baseline_entries,
        footer_lines=footer_lines,
        baseline_stats=baseline_stats,
        max_episode_steps=1,
        missing_weight=1000.0,
        via_weight=8.0,
        wire_weight=0.04,
        full_bonus=120.0,
        failure_penalty=40.0,
        fixed_targets=fixed_targets,
    )


def prepare_initial_state(
    run_dir: Path,
    args: argparse.Namespace,
) -> tuple[
    list[core.OrderEntry],
    list[str],
    CandidateRecord,
    core.BoardData,
    dict[str, tuple[str, int]],
    str,
]:
    baseline_entries, footer_lines, baseline_candidate, _board_data = eval135.prepare_baseline(run_dir)
    source_entries = load_initial_entries(args.initial_order, baseline_entries)
    source_candidate = baseline_candidate
    initial_source = "baseline"
    if args.initial_order is not None:
        initial_source = "initial_order"
        if eval135.signature(source_entries) != eval135.signature(baseline_entries):
            evaluated = eval135.evaluate_entries(
                run_dir,
                footer_lines,
                source_entries,
                "initial_order",
                f"initial_order:{args.initial_order}",
            )
            if evaluated is None:
                raise RuntimeError("Initial 135 order evaluation failed")
            source_candidate = evaluated

    source_board_data = core.load_board_data(
        core.PROJECT_DIR,
        order_entries=source_entries,
        footer_lines=footer_lines,
    )
    fixed_targets = load_fixed_nets(args.fixed_csv, source_entries)
    if fixed_targets:
        initial_source = f"{initial_source}+fixed_csv"
        print(f"Loaded {len(fixed_targets)} fixed 135 nets from {args.fixed_csv}")

    setup_env = make_env(
        run_dir,
        source_board_data,
        source_entries,
        footer_lines,
        source_candidate.stats,
        fixed_targets,
    )
    initial_entries = clone_entries(setup_env.entries)
    initial_candidate = source_candidate
    if eval135.signature(initial_entries) != eval135.signature(source_entries):
        evaluated = eval135.evaluate_entries(
            run_dir,
            footer_lines,
            initial_entries,
            "fixed_initial",
            "fixed_csv_initial",
        )
        if evaluated is None:
            raise RuntimeError("Fixed CSV initial 135 routing evaluation failed")
        initial_candidate = evaluated

    initial_board_data = core.load_board_data(
        core.PROJECT_DIR,
        order_entries=initial_entries,
        footer_lines=footer_lines,
    )
    return initial_entries, footer_lines, initial_candidate, initial_board_data, fixed_targets, initial_source


def candidate_score(candidate: CandidateRecord | None) -> tuple[float, float, float, float]:
    if candidate is None:
        return (-1.0, -1.0, -1.0e18, -1.0e18)
    stats = candidate.stats
    return (
        float(stats.routed_nets),
        float(stats.completion_rate),
        -float(stats.total_wire_length),
        -float(stats.vias),
    )


def better_individual(left: Individual, right: Individual) -> bool:
    return left.score > right.score


def action_for_net(env: RoutingLocalActionEnv, net: str, op: int) -> int | None:
    for index, entry in enumerate(env.entries):
        if entry.net == net:
            return index * N_OPS + op
    return None


def apply_action_if_legal(
    env: RoutingLocalActionEnv,
    entries: list[core.OrderEntry],
    net: str,
    op: int,
) -> tuple[list[core.OrderEntry], bool]:
    env.entries = env.canonicalize(entries)
    flat_action = action_for_net(env, net, op)
    if flat_action is None:
        return clone_entries(entries), False
    mask = env.get_action_mask()
    if flat_action >= len(mask) or mask[flat_action] <= 0:
        return clone_entries(entries), False
    return env.apply_action(flat_action), True


def legal_actions(env: RoutingLocalActionEnv, entries: list[core.OrderEntry]) -> list[int]:
    env.entries = env.canonicalize(entries)
    mask = env.get_action_mask()
    return [index for index, allowed in enumerate(mask) if allowed > 0]


def mutate_entries(
    entries: list[core.OrderEntry],
    env: RoutingLocalActionEnv,
    rng: random.Random,
    steps: int,
) -> list[core.OrderEntry]:
    current = clone_entries(entries)
    for _ in range(max(0, steps)):
        actions = legal_actions(env, current)
        if not actions:
            break
        env.entries = env.canonicalize(current)
        current = env.apply_action(rng.choice(actions))
    return env.canonicalize(current)


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


def action_priority(env: RoutingLocalActionEnv, flat_action: int, focus_nets: set[str]) -> tuple[int, int, int]:
    net_idx = flat_action // N_OPS
    op = flat_action % N_OPS
    entry = env.entries[net_idx]
    focus = 0 if entry.net in focus_nets else 1
    op_rank = {OP_FLIP: 0, OP_EARLIER: 1, OP_LATER: 2, OP_RESTORE: 3}.get(op, 4)
    return (focus, op_rank, entry.order)


def deterministic_probe_entries(
    entries: list[core.OrderEntry],
    env: RoutingLocalActionEnv,
    focus_nets: set[str],
    limit: int,
) -> list[tuple[list[core.OrderEntry], str]]:
    if limit <= 0:
        return []
    env.entries = env.canonicalize(entries)
    actions = legal_actions(env, env.entries)
    actions.sort(key=lambda action: action_priority(env, action, focus_nets))
    probes: list[tuple[list[core.OrderEntry], str]] = []
    seen: set[tuple[tuple[str, str, int], ...]] = set()
    for action in actions:
        env.entries = env.canonicalize(entries)
        candidate_entries = env.apply_action(action)
        signature = eval135.signature(candidate_entries)
        if signature in seen:
            continue
        seen.add(signature)
        op = action % N_OPS
        net = env.entries[action // N_OPS].net
        probes.append((candidate_entries, f"deterministic_probe:{net}:op{op}"))
        if len(probes) >= limit:
            break
    return probes


def crossover_entries(
    parent_a: list[core.OrderEntry],
    parent_b: list[core.OrderEntry],
    env: RoutingLocalActionEnv,
    rng: random.Random,
) -> list[core.OrderEntry]:
    current = env.canonicalize(parent_a)
    target_by_net = {entry.net: entry for entry in parent_b}
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
            env.entries = env.canonicalize(current)
            by_net = {entry.net: entry for entry in env.entries}
            current_entry = by_net.get(net)
            target_entry = target_by_net.get(net)
            if current_entry is None or target_entry is None:
                break
            if (current_entry.layer, current_entry.order) == (target_entry.layer, target_entry.order):
                break
            if current_entry.layer != target_entry.layer:
                current, changed = apply_action_if_legal(env, current, net, OP_FLIP)
                if not changed:
                    break
                continue
            op = OP_EARLIER if current_entry.order > target_entry.order else OP_LATER
            current, changed = apply_action_if_legal(env, current, net, op)
            if not changed:
                break
    return env.canonicalize(current)


def evaluate_individual(
    run_dir: Path,
    footer_lines: list[str],
    entries: list[core.OrderEntry],
    source: str,
    eval_index: int,
    cache: dict[tuple[tuple[str, str, int], ...], CandidateRecord | None],
) -> tuple[Individual, bool]:
    normalized = clone_entries(entries)
    signature = eval135.signature(normalized)
    if signature in cache:
        candidate = cache[signature]
        return Individual(normalized, candidate, candidate_score(candidate), source), False

    candidate = eval135.evaluate_entries(
        run_dir,
        footer_lines,
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
    candidate: CandidateRecord | None,
    eval_index: int,
    best_partial: CandidateRecord,
    best_full: CandidateRecord | None,
    best_wire: CandidateRecord | None,
    best_via: CandidateRecord | None,
    first_full_eval: int | None,
) -> tuple[CandidateRecord, CandidateRecord | None, CandidateRecord | None, CandidateRecord | None, int | None]:
    if candidate is None:
        return best_partial, best_full, best_wire, best_via, first_full_eval
    if candidate_score(candidate) > candidate_score(best_partial):
        best_partial = candidate
    if candidate.stats.routed_nets == candidate.stats.total_nets:
        if first_full_eval is None:
            first_full_eval = eval_index
        if best_full is None or (candidate.stats.total_wire_length, candidate.stats.vias) < (
            best_full.stats.total_wire_length,
            best_full.stats.vias,
        ):
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
    best_full: CandidateRecord | None,
    best_partial: CandidateRecord,
) -> tuple[str, CandidateRecord]:
    if best_full is not None:
        return "best_full", best_full
    return "best_partial", best_partial


def export_order_txt(candidate: CandidateRecord | None, dest: Path) -> str | None:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genetic/local search optimizer for 135 layer/order routing.")
    parser.add_argument("--eval-budget", type=int, default=500)
    parser.add_argument("--algorithm", choices=("ga",), default="ga", help=argparse.SUPPRESS)
    parser.add_argument("--population-size", type=int, default=32)
    parser.add_argument("--elite-size", type=int, default=4)
    parser.add_argument("--deterministic-probe-fraction", type=float, default=0.50)
    parser.add_argument("--mutation-rate", type=float, default=0.35)
    parser.add_argument("--crossover-rate", type=float, default=0.60)
    parser.add_argument("--local-search-rate", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=20260422)
    parser.add_argument("--tag", default="ga_135")
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parent / "search_runs")
    parser.add_argument("--no-explain", action="store_true", help="Skip deterministic explanation.md generation.")
    parser.add_argument(
        "--fixed-csv",
        type=Path,
        default=None,
        help="Optional CSV with net,layer,order target rows to keep fixed during search.",
    )
    parser.add_argument(
        "--initial-order",
        type=Path,
        default=None,
        help="Optional complete order_out.txt-style file to use as the GA initial layer/order state.",
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
        initial_candidate,
        board_data,
        fixed_targets,
        initial_source,
    ) = prepare_initial_state(run_dir, args)
    env = make_env(run_dir, board_data, initial_entries, footer_lines, initial_candidate.stats, fixed_targets)

    cache: dict[tuple[tuple[str, str, int], ...], CandidateRecord | None] = {
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

    def add_individual(individual: Individual, consumed: bool) -> None:
        nonlocal eval_used, best_partial, best_full, best_wire, best_via, first_full_eval
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
    probe_limit = min(
        args.eval_budget,
        max(0, int(args.eval_budget * probe_fraction)),
    )
    for entries, source in deterministic_probe_entries(initial_entries, env, focus_nets, probe_limit):
        if eval_used >= args.eval_budget:
            break
        signature = eval135.signature(entries)
        if signature in population_signatures:
            continue
        individual, consumed = evaluate_individual(
            run_dir,
            footer_lines,
            entries,
            source,
            eval_used + 1,
            cache,
        )
        if consumed:
            deterministic_evals += 1
        add_individual(individual, consumed)

    target_population = args.population_size

    attempts = 0
    while (
        len(population) < target_population
        and eval_used < args.eval_budget
        and attempts < args.population_size * 20
    ):
        attempts += 1
        steps = adaptive_perturbation_steps(len(initial_entries), eval_used, args.eval_budget, rng)
        entries = mutate_entries(initial_entries, env, rng, steps)
        signature = eval135.signature(entries)
        if signature in population_signatures:
            continue
        individual, consumed = evaluate_individual(
            run_dir,
            footer_lines,
            entries,
            f"ga_initial_mutation:{steps}",
            eval_used + 1,
            cache,
        )
        add_individual(individual, consumed)

    ga_stagnant_generations = 0
    while eval_used < args.eval_budget:
        generations += 1
        eval_at_generation_start = eval_used
        population.sort(key=lambda item: item.score, reverse=True)
        previous_best_score = population[0].score
        next_population = population[: args.elite_size]
        next_signatures = {eval135.signature(individual.entries) for individual in next_population}

        for elite in population[: args.elite_size]:
            if eval_used >= args.eval_budget:
                break
            if rng.random() > args.local_search_rate:
                continue
            steps = rng.randint(1, 2)
            entries = mutate_entries(elite.entries, env, rng, steps)
            signature = eval135.signature(entries)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
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
                entries = crossover_entries(parent_a.entries, parent_b.entries, env, rng)
                source = f"ga_crossover:{parent_a.source}:{parent_b.source}"
            else:
                entries = clone_entries(parent_a.entries)
                source = f"ga_clone:{parent_a.source}"

            if rng.random() < args.mutation_rate or eval135.signature(entries) == eval135.signature(parent_a.entries):
                steps = adaptive_perturbation_steps(len(entries), eval_used, args.eval_budget, rng)
                entries = mutate_entries(entries, env, rng, steps)
                source = f"{source}:mut{steps}"
            else:
                entries = env.canonicalize(entries)

            signature = eval135.signature(entries)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
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
        if args.algorithm == "ga":
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
                and restart_attempts < args.population_size * 20
            ):
                restart_attempts += 1
                base_entries = initial_entries if rng.random() < 0.65 else next_population[0].entries
                steps = adaptive_perturbation_steps(len(base_entries), eval_used, args.eval_budget, rng)
                steps += random_perturbation_steps(len(base_entries), rng)
                entries = mutate_entries(base_entries, env, rng, steps)
                signature = eval135.signature(entries)
                if signature in next_signatures:
                    continue
                individual, consumed = evaluate_individual(
                    run_dir,
                    footer_lines,
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

    save_artifacts(best_partial, run_dir / "best_partial")
    save_artifacts(best_full, run_dir / "best_full")
    save_artifacts(best_wire, run_dir / "best_wire_full")
    save_artifacts(best_via, run_dir / "best_via_full")
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = Summary(
        tag=args.tag,
        variant="ga_layer_order_search",
        seed=args.seed,
        algorithm="ga",
        eval_budget=args.eval_budget,
        eval_used=eval_used,
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
        best_partial=candidate_to_json(best_partial),
        best_full=candidate_to_json(best_full),
        best_wire_full=candidate_to_json(best_wire),
        best_via_full=candidate_to_json(best_via),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_net_count=len(fixed_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
    )
    (run_dir / "summary.json").write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    print(json.dumps(asdict(summary), indent=2))
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, initial_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    print(f"135 GA layer/order run written to: {run_dir}")
    return summary


def main() -> None:
    train(parse_args())


if __name__ == "__main__":
    main()
