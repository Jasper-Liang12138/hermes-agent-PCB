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

from explain_arc import generate_explanation
import rl_arc_core as core
import routing_eval_arc as eval_arc
from routing_env_arc import OP_EARLIER, OP_LATER, OP_RESTORE, OP_SWAP_LAYER_BASE, ArcPairRoutingEnv


@dataclass
class Individual:
    pairs: list[core.PairEntry]
    candidate: eval_arc.RouteCandidate | None
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
    fixed_csv: str | None
    fixed_pair_count: int
    initial_order: str | None
    initial_source: str
    primary_best_kind: str
    primary_best_order_txt: str | None
    initial_layer_pair_counts: dict[str, int]


def load_fixed_pair_targets(
    csv_path: Path | None,
    baseline_pairs: list[core.PairEntry],
    layer_names: list[str],
) -> dict[str, tuple[str, int]]:
    if csv_path is None:
        return {}

    net_to_pair: dict[str, core.PairEntry] = {}
    for pair in baseline_pairs:
        net_to_pair[pair.neg_net] = pair
        net_to_pair[pair.pos_net] = pair

    required_fields = {"net", "layer", "order"}
    valid_layers = set(layer_names)
    fixed_pair_targets: dict[str, tuple[str, int]] = {}
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
            if net not in net_to_pair:
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

            pair = net_to_pair[net]
            if pair.key in fixed_pair_targets:
                raise ValueError(
                    f"{csv_path}:{line_no} duplicates fixed pair {pair.key}; "
                    "use either the P net or the N net, not both"
                )
            slot = (layer, order)
            if slot in fixed_slots:
                raise ValueError(
                    f"{csv_path}:{line_no} conflicts with fixed pair {fixed_slots[slot]} "
                    f"at {layer},{order}"
                )
            fixed_pair_targets[pair.key] = slot
            fixed_slots[slot] = pair.key
    return fixed_pair_targets


def load_initial_pairs(
    order_path: Path | None,
    baseline_pairs: list[core.PairEntry],
    layer_names: list[str],
) -> list[core.PairEntry]:
    if order_path is None:
        return core.clone_pairs(baseline_pairs)

    pairs, _footer_lines, _input_layer_names = core.read_order_pairs(order_path.expanduser())
    if not pairs:
        raise ValueError(f"{order_path} does not contain any order rows")

    baseline_keys = {pair.key for pair in baseline_pairs}
    valid_layers = set(layer_names)
    seen_keys: set[str] = set()
    seen_slots: dict[tuple[str, int], str] = {}
    for pair in pairs:
        if pair.key in seen_keys:
            raise ValueError(f"{order_path} duplicates pair {pair.key}")
        if pair.key not in baseline_keys:
            raise ValueError(f"{order_path} references unknown pair {pair.key}")
        if pair.layer not in valid_layers:
            raise ValueError(
                f"{order_path} uses unknown layer {pair.layer!r}; "
                f"expected one of {', '.join(layer_names)}"
            )
        if pair.order <= 0:
            raise ValueError(f"{order_path} has non-positive pair order {pair.order} for {pair.key}")
        slot = (pair.layer, pair.order)
        if slot in seen_slots:
            raise ValueError(
                f"{order_path} assigns both {seen_slots[slot]} and {pair.key} to "
                f"{pair.layer},{pair.order}"
            )
        seen_keys.add(pair.key)
        seen_slots[slot] = pair.key

    missing = sorted(baseline_keys - seen_keys)
    if missing:
        raise ValueError(f"{order_path} is missing {len(missing)} baseline pairs, first missing: {missing[0]}")
    return core.canonicalize_pairs(pairs, layer_names)


def make_env(
    output_root: Path,
    board_data: core.BoardData,
    baseline_pairs: list[core.PairEntry],
    footer_lines: list[str],
    layer_names: list[str],
    baseline_stats: core.RouteStats,
    fixed_pair_targets: dict[str, tuple[str, int]],
) -> ArcPairRoutingEnv:
    return ArcPairRoutingEnv(
        env_id=0,
        output_root=output_root,
        board_data=board_data,
        baseline_pairs=baseline_pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
        baseline_stats=baseline_stats,
        max_episode_steps=1,
        missing_weight=10000.0,
        wire_weight=0.02,
        via_weight=0.0,
        full_bonus=1000.0,
        failure_penalty=5000.0,
        fixed_pair_targets=fixed_pair_targets,
    )


def prepare_initial_state(
    run_dir: Path,
    args: argparse.Namespace,
) -> tuple[
    list[core.PairEntry],
    list[str],
    list[str],
    eval_arc.RouteCandidate,
    core.BoardData,
    dict[str, tuple[str, int]],
    str,
]:
    baseline_pairs, footer_lines, layer_names, baseline_candidate, _board_data = eval_arc.prepare_baseline(run_dir)
    source_pairs = load_initial_pairs(args.initial_order, baseline_pairs, layer_names)
    source_candidate = baseline_candidate
    initial_source = "baseline"
    if args.initial_order is not None:
        initial_source = "initial_order"
        if eval_arc.signature(source_pairs) != eval_arc.signature(baseline_pairs):
            evaluated = eval_arc.evaluate_pairs(
                run_dir,
                footer_lines,
                layer_names,
                source_pairs,
                "initial_order",
                f"initial_order:{args.initial_order}",
            )
            if evaluated is None:
                raise RuntimeError("Initial arc order evaluation failed")
            source_candidate = evaluated

    source_board_data = core.load_board_data(
        core.PROJECT_DIR,
        pairs=source_pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    fixed_pair_targets = load_fixed_pair_targets(args.fixed_csv, source_pairs, layer_names)
    if fixed_pair_targets:
        initial_source = f"{initial_source}+fixed_csv"
        print(f"Loaded {len(fixed_pair_targets)} fixed arc pairs from {args.fixed_csv}")
    setup_env = make_env(
        run_dir,
        source_board_data,
        source_pairs,
        footer_lines,
        layer_names,
        source_candidate.stats,
        fixed_pair_targets,
    )
    initial_pairs = core.clone_pairs(setup_env.pairs)
    initial_candidate = source_candidate
    if eval_arc.signature(initial_pairs) != eval_arc.signature(source_pairs):
        evaluated = eval_arc.evaluate_pairs(
            run_dir,
            footer_lines,
            layer_names,
            initial_pairs,
            "fixed_initial",
            "fixed_csv_initial",
        )
        if evaluated is None:
            raise RuntimeError("Fixed CSV initial arc routing evaluation failed")
        initial_candidate = evaluated
    initial_board_data = core.load_board_data(
        core.PROJECT_DIR,
        pairs=initial_pairs,
        footer_lines=footer_lines,
        layer_names=layer_names,
    )
    return initial_pairs, footer_lines, layer_names, initial_candidate, initial_board_data, fixed_pair_targets, initial_source


def candidate_score(candidate: eval_arc.RouteCandidate | None) -> tuple[float, float, float, float]:
    if candidate is None:
        return (-1.0, -1.0, -1.0e18, -1.0e18)
    stats = candidate.stats
    return (
        float(stats.routed_nets),
        float(stats.completion_rate),
        -float(stats.total_wire_length),
        -float(stats.vias),
    )


def layer_pair_counts(pairs: list[core.PairEntry], layer_names: list[str]) -> dict[str, int]:
    return {layer: sum(1 for pair in pairs if pair.layer == layer) for layer in layer_names}


def same_layer_counts(
    pairs: list[core.PairEntry],
    layer_names: list[str],
    expected_counts: dict[str, int],
) -> bool:
    return layer_pair_counts(pairs, layer_names) == expected_counts


def action_for_pair(env: ArcPairRoutingEnv, key: str, op: int) -> int | None:
    for index, pair in enumerate(env.pairs):
        if pair.key == key:
            return index * env.n_ops + op
    return None


def apply_action_if_legal(
    env: ArcPairRoutingEnv,
    pairs: list[core.PairEntry],
    key: str,
    op: int,
    expected_counts: dict[str, int],
) -> tuple[list[core.PairEntry], bool]:
    env.pairs = env.normalize_orders(pairs)
    flat_action = action_for_pair(env, key, op)
    if flat_action is None:
        return core.clone_pairs(pairs), False
    mask = env.get_action_mask()
    if flat_action >= len(mask) or mask[flat_action] <= 0:
        return core.clone_pairs(pairs), False
    updated = env.apply_action(flat_action)
    if not same_layer_counts(updated, env.layer_names, expected_counts):
        return core.clone_pairs(pairs), False
    return updated, True


def legal_count_preserving_actions(
    env: ArcPairRoutingEnv,
    pairs: list[core.PairEntry],
    expected_counts: dict[str, int],
) -> list[int]:
    env.pairs = env.normalize_orders(pairs)
    mask = env.get_action_mask()
    actions: list[int] = []
    for flat_action, allowed in enumerate(mask):
        if allowed <= 0:
            continue
        updated = env.apply_action(flat_action)
        if same_layer_counts(updated, env.layer_names, expected_counts):
            actions.append(flat_action)
    return actions


def mutate_pairs(
    pairs: list[core.PairEntry],
    env: ArcPairRoutingEnv,
    rng: random.Random,
    steps: int,
    expected_counts: dict[str, int],
) -> list[core.PairEntry]:
    current = core.clone_pairs(pairs)
    for _ in range(max(0, steps)):
        actions = legal_count_preserving_actions(env, current, expected_counts)
        if not actions:
            break
        env.pairs = env.normalize_orders(current)
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


def action_priority(env: ArcPairRoutingEnv, flat_action: int, focus_pairs: set[str]) -> tuple[int, int, int]:
    pair_idx = flat_action // env.n_ops
    op = flat_action % env.n_ops
    pair = env.pairs[pair_idx]
    focus = 0 if pair.key in focus_pairs else 1
    if op >= OP_SWAP_LAYER_BASE:
        op_rank = 0
    else:
        op_rank = {OP_EARLIER: 1, OP_LATER: 2, OP_RESTORE: 3}.get(op, 4)
    return (focus, op_rank, pair.order)


def seed_swap_pair_probes(
    baseline_pairs: list[core.PairEntry],
    layer_names: list[str],
    rng: random.Random,
    limit: int,
    fixed_pair_keys: set[str],
    mode: str,
) -> list[tuple[list[core.PairEntry], str]]:
    if limit <= 0:
        return []
    max_order = max((pair.order for pair in baseline_pairs), default=0)
    swap_specs: list[tuple[int, str, str]] = []
    for order in range(1, max_order + 1):
        for left_idx, left_layer in enumerate(layer_names):
            for right_layer in layer_names[left_idx + 1 :]:
                swap_specs.append((order, left_layer, right_layer))
    if mode == "random":
        rng.shuffle(swap_specs)

    probes: list[tuple[list[core.PairEntry], str]] = []
    for order, left_layer, right_layer in swap_specs:
        if len(probes) >= limit:
            break
        pairs = core.clone_pairs(baseline_pairs)
        left = [pair for pair in pairs if pair.layer == left_layer and pair.order == order]
        right = [pair for pair in pairs if pair.layer == right_layer and pair.order == order]
        if len(left) != 1 or len(right) != 1:
            continue
        if left[0].key in fixed_pair_keys or right[0].key in fixed_pair_keys:
            continue
        left[0].layer, right[0].layer = right[0].layer, left[0].layer
        probes.append((pairs, f"seed_swap:{left_layer}<->{right_layer}:slot{order}"))
    return probes


def deterministic_action_pair_probes(
    pairs: list[core.PairEntry],
    env: ArcPairRoutingEnv,
    focus_pairs: set[str],
    expected_counts: dict[str, int],
    limit: int,
) -> list[tuple[list[core.PairEntry], str]]:
    if limit <= 0:
        return []
    env.pairs = env.normalize_orders(pairs)
    actions = legal_count_preserving_actions(env, env.pairs, expected_counts)
    actions.sort(key=lambda action: action_priority(env, action, focus_pairs))
    probes: list[tuple[list[core.PairEntry], str]] = []
    seen: set[tuple[tuple[str, str, int], ...]] = set()
    for action in actions:
        env.pairs = env.normalize_orders(pairs)
        candidate_pairs = env.apply_action(action)
        if not same_layer_counts(candidate_pairs, env.layer_names, expected_counts):
            continue
        signature = eval_arc.signature(candidate_pairs)
        if signature in seen:
            continue
        seen.add(signature)
        pair = env.pairs[action // env.n_ops]
        op = action % env.n_ops
        probes.append((candidate_pairs, f"deterministic_probe:{pair.key}:op{op}"))
        if len(probes) >= limit:
            break
    return probes


def crossover_pairs(
    parent_a: list[core.PairEntry],
    parent_b: list[core.PairEntry],
    env: ArcPairRoutingEnv,
    rng: random.Random,
    expected_counts: dict[str, int],
) -> list[core.PairEntry]:
    current = env.normalize_orders(parent_a)
    target_by_key = {pair.key: pair for pair in parent_b}
    layer_index = {layer: index for index, layer in enumerate(env.layer_names)}
    changed_keys = [
        pair.key
        for pair in current
        if pair.key not in env.fixed_pair_keys
        and pair.key in target_by_key
        and (pair.layer, pair.order) != (target_by_key[pair.key].layer, target_by_key[pair.key].order)
    ]
    rng.shuffle(changed_keys)
    if not changed_keys:
        return current

    target_count = rng.randint(1, max(1, min(len(changed_keys), max(2, len(changed_keys) // 3))))
    for key in changed_keys[:target_count]:
        for _ in range(len(parent_a) * 2):
            env.pairs = env.normalize_orders(current)
            by_key = {pair.key: pair for pair in env.pairs}
            current_pair = by_key.get(key)
            target_pair = target_by_key.get(key)
            if current_pair is None or target_pair is None:
                break
            if (current_pair.layer, current_pair.order) == (target_pair.layer, target_pair.order):
                break
            if current_pair.layer != target_pair.layer:
                target_layer_index = layer_index[target_pair.layer]
                current, changed = apply_action_if_legal(
                    env,
                    current,
                    key,
                    OP_SWAP_LAYER_BASE + target_layer_index,
                    expected_counts,
                )
                if changed:
                    continue
            op = OP_EARLIER if current_pair.order > target_pair.order else OP_LATER
            current, changed = apply_action_if_legal(env, current, key, op, expected_counts)
            if not changed:
                break
    return env.normalize_orders(current)


def evaluate_individual(
    run_dir: Path,
    footer_lines: list[str],
    layer_names: list[str],
    pairs: list[core.PairEntry],
    source: str,
    eval_index: int,
    cache: dict[tuple[tuple[str, str, int], ...], eval_arc.RouteCandidate | None],
) -> tuple[Individual, bool]:
    normalized = core.canonicalize_pairs(pairs, layer_names)
    signature = eval_arc.signature(normalized)
    if signature in cache:
        candidate = cache[signature]
        return Individual(normalized, candidate, candidate_score(candidate), source), False

    candidate = eval_arc.evaluate_pairs(
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


def update_best_arc(
    candidate: eval_arc.RouteCandidate | None,
    eval_index: int,
    best_partial: eval_arc.RouteCandidate,
    best_full: eval_arc.RouteCandidate | None,
    best_wire: eval_arc.RouteCandidate | None,
    first_full_eval: int | None,
) -> tuple[eval_arc.RouteCandidate, eval_arc.RouteCandidate | None, eval_arc.RouteCandidate | None, int | None]:
    if candidate is None:
        return best_partial, best_full, best_wire, first_full_eval
    if eval_arc.partial_key(candidate) > eval_arc.partial_key(best_partial):
        best_partial = candidate
    if candidate.stats.routed_nets == candidate.stats.total_nets:
        if first_full_eval is None:
            first_full_eval = eval_index
        if best_full is None or eval_arc.full_key(candidate) > eval_arc.full_key(best_full):
            best_full = candidate
        if best_wire is None or (candidate.stats.total_wire_length, candidate.stats.vias) < (
            best_wire.stats.total_wire_length,
            best_wire.stats.vias,
        ):
            best_wire = candidate
    return best_partial, best_full, best_wire, first_full_eval


def choose_primary_best(
    best_full: eval_arc.RouteCandidate | None,
    best_partial: eval_arc.RouteCandidate,
) -> tuple[str, eval_arc.RouteCandidate]:
    if best_full is not None:
        return "best_full", best_full
    return "best_partial", best_partial


def export_order_txt(candidate: eval_arc.RouteCandidate | None, dest: Path) -> str | None:
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
    parser = argparse.ArgumentParser(description="Genetic/local search optimizer for arc pair layer/order routing.")
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
    parser.add_argument("--tag", default="ga_arc")
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parent / "search_runs")
    parser.add_argument("--no-explain", action="store_true", help="Skip deterministic explanation.md generation.")
    parser.add_argument(
        "--fixed-csv",
        type=Path,
        default=None,
        help="Optional CSV with net,layer,order rows. A P or N net locks its whole differential pair.",
    )
    parser.add_argument(
        "--initial-order",
        type=Path,
        default=None,
        help="Optional complete order_input.txt-style file to use as the GA initial pair layer/order state.",
    )
    return parser.parse_args()


def train(args: argparse.Namespace) -> Summary:
    start_time = time.monotonic()
    validate_args(args)
    rng = random.Random(args.seed)
    run_dir = args.output_root / f"{args.tag}_ga_pair_layer_order_seed{args.seed}_budget{args.eval_budget}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    (
        initial_pairs,
        footer_lines,
        layer_names,
        initial_candidate,
        board_data,
        fixed_pair_targets,
        initial_source,
    ) = prepare_initial_state(run_dir, args)
    initial_counts = layer_pair_counts(initial_pairs, layer_names)
    env = make_env(run_dir, board_data, initial_pairs, footer_lines, layer_names, initial_candidate.stats, fixed_pair_targets)

    cache: dict[tuple[tuple[str, str, int], ...], eval_arc.RouteCandidate | None] = {
        eval_arc.signature(initial_pairs): initial_candidate,
    }
    eval_used = 0
    generations = 0
    initial_individual = Individual(
        core.clone_pairs(initial_pairs),
        initial_candidate,
        candidate_score(initial_candidate),
        initial_source,
    )
    population: list[Individual] = [initial_individual]
    population_signatures = {eval_arc.signature(initial_pairs)}
    best_partial = initial_candidate
    best_full = initial_candidate if initial_candidate.stats.routed_nets == initial_candidate.stats.total_nets else None
    best_wire = best_full
    first_full_eval = 0 if best_full is not None else None
    deterministic_evals = 0

    def add_individual(individual: Individual, consumed: bool) -> None:
        nonlocal eval_used, best_partial, best_full, best_wire, first_full_eval
        if consumed:
            eval_used += 1
        signature = eval_arc.signature(individual.pairs)
        if signature not in population_signatures:
            population.append(individual)
            population_signatures.add(signature)
        best_partial, best_full, best_wire, first_full_eval = update_best_arc(
            individual.candidate,
            eval_used,
            best_partial,
            best_full,
            best_wire,
            first_full_eval,
        )

    focus_pairs: set[str] = set()
    net_to_pair = {pair.neg_net: pair.key for pair in initial_pairs}
    net_to_pair.update({pair.pos_net: pair.key for pair in initial_pairs})
    for net in initial_candidate.missing_nets:
        key = net_to_pair.get(net)
        if key:
            focus_pairs.add(key)

    probe_fraction = min(args.deterministic_probe_fraction, 0.15)
    probe_limit = min(
        args.eval_budget,
        max(0, int(args.eval_budget * probe_fraction)),
    )
    seed_rng = random.Random(args.seed + 13579)
    seed_probe_limit = max(0, probe_limit // 2)
    probes = seed_swap_pair_probes(
        initial_pairs,
        layer_names,
        seed_rng,
        seed_probe_limit,
        set(fixed_pair_targets),
        args.seed_swap_mode,
    )
    remaining_probe_limit = max(0, probe_limit - len(probes))
    probes.extend(
        deterministic_action_pair_probes(
            initial_pairs,
            env,
            focus_pairs,
            initial_counts,
            remaining_probe_limit,
        )
    )
    for pairs, source in probes:
        if eval_used >= args.eval_budget:
            break
        if not same_layer_counts(pairs, layer_names, initial_counts):
            continue
        signature = eval_arc.signature(core.canonicalize_pairs(pairs, layer_names))
        if signature in population_signatures:
            continue
        individual, consumed = evaluate_individual(
            run_dir,
            footer_lines,
            layer_names,
            pairs,
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
        steps = adaptive_perturbation_steps(len(initial_pairs), eval_used, args.eval_budget, rng)
        pairs = mutate_pairs(initial_pairs, env, rng, steps, initial_counts)
        signature = eval_arc.signature(pairs)
        if signature in population_signatures:
            continue
        individual, consumed = evaluate_individual(
            run_dir,
            footer_lines,
            layer_names,
            pairs,
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
        next_signatures = {eval_arc.signature(individual.pairs) for individual in next_population}

        for elite in population[: args.elite_size]:
            if eval_used >= args.eval_budget:
                break
            if rng.random() > args.local_search_rate:
                continue
            steps = rng.randint(1, 2)
            pairs = mutate_pairs(elite.pairs, env, rng, steps, initial_counts)
            signature = eval_arc.signature(pairs)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
                layer_names,
                pairs,
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
                pairs = crossover_pairs(parent_a.pairs, parent_b.pairs, env, rng, initial_counts)
                source = f"ga_crossover:{parent_a.source}:{parent_b.source}"
            else:
                pairs = core.clone_pairs(parent_a.pairs)
                source = f"ga_clone:{parent_a.source}"

            if rng.random() < args.mutation_rate or eval_arc.signature(pairs) == eval_arc.signature(parent_a.pairs):
                steps = adaptive_perturbation_steps(len(pairs), eval_used, args.eval_budget, rng)
                pairs = mutate_pairs(pairs, env, rng, steps, initial_counts)
                source = f"{source}:mut{steps}"
            else:
                pairs = env.normalize_orders(pairs)

            if not same_layer_counts(pairs, layer_names, initial_counts):
                continue
            signature = eval_arc.signature(pairs)
            if signature in next_signatures:
                continue
            individual, consumed = evaluate_individual(
                run_dir,
                footer_lines,
                layer_names,
                pairs,
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
                base_pairs = initial_pairs if rng.random() < 0.65 else next_population[0].pairs
                steps = adaptive_perturbation_steps(len(base_pairs), eval_used, args.eval_budget, rng)
                steps += random_perturbation_steps(len(base_pairs), rng)
                pairs = mutate_pairs(base_pairs, env, rng, steps, initial_counts)
                if not same_layer_counts(pairs, layer_names, initial_counts):
                    continue
                if not env.fixed_constraints_preserved(pairs):
                    continue
                signature = eval_arc.signature(pairs)
                if signature in next_signatures:
                    continue
                individual, consumed = evaluate_individual(
                    run_dir,
                    footer_lines,
                    layer_names,
                    pairs,
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

    eval_arc.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval_arc.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval_arc.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = Summary(
        tag=args.tag,
        variant="ga_pair_layer_order_search",
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
        best_partial=eval_arc.candidate_to_json(best_partial),
        best_full=eval_arc.candidate_to_json(best_full),
        best_wire_full=eval_arc.candidate_to_json(best_wire),
        fixed_csv=str(args.fixed_csv) if args.fixed_csv is not None else None,
        fixed_pair_count=len(fixed_pair_targets),
        initial_order=str(args.initial_order) if args.initial_order is not None else None,
        initial_source=initial_source,
        primary_best_kind=primary_best_kind,
        primary_best_order_txt=primary_best_order_txt,
        initial_layer_pair_counts=initial_counts,
    )
    (run_dir / "summary.json").write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    print(json.dumps(asdict(summary), indent=2))
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, initial_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    print(f"Arc GA pair layer/order run written to: {run_dir}")
    return summary


def main() -> None:
    train(parse_args())


if __name__ == "__main__":
    main()
