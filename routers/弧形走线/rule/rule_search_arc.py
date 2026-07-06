#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

RULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = RULE_DIR.parent
RL_DIR = PROJECT_DIR / "rl"
GA_DIR = PROJECT_DIR / "ga"
sys.path.insert(0, str(RL_DIR))
sys.path.insert(0, str(GA_DIR))

from explain_arc import generate_explanation
import rl_arc_core as core
import routing_eval_arc as eval_arc
from train_ga_arc import (
    action_priority,
    choose_primary_best,
    export_order_txt,
    layer_pair_counts,
    legal_count_preserving_actions,
    make_env,
    prepare_initial_state,
    same_layer_counts,
    update_best_arc,
)

METHOD_ROOT = RULE_DIR / "search_runs" / "rule"


@dataclass
class Recipe:
    pairs: list[core.PairEntry]
    source: str
    depth: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rule-based optimizer for arc exe pair layer/order routing.")
    parser.add_argument("--eval-budget", type=int, default=30)
    parser.add_argument("--stop-completion-rate", type=float, default=1.0, help="Stop once best completion_rate reaches this value; use 0 to disable early stop.")
    parser.add_argument("--seed", type=int, default=20260425)
    parser.add_argument("--tag", default="rule_arc_exe")
    parser.add_argument("--output-root", type=Path, default=METHOD_ROOT)
    parser.add_argument("--beam-width", type=int, default=8)
    parser.add_argument("--max-chain-length", type=int, default=3)
    parser.add_argument("--fixed-csv", type=Path, default=None)
    parser.add_argument("--initial-order", type=Path, default=None)
    parser.add_argument("--no-explain", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.eval_budget < 0:
        raise ValueError("--eval-budget must be non-negative")
    if args.stop_completion_rate < 0.0 or args.stop_completion_rate > 1.0:
        raise ValueError("--stop-completion-rate must be between 0 and 1")
    if args.beam_width < 0:
        raise ValueError("--beam-width must be non-negative")
    if args.max_chain_length <= 0:
        raise ValueError("--max-chain-length must be positive")


def completion_target_reached(candidate: eval_arc.RouteCandidate | None, args: argparse.Namespace) -> bool:
    return args.stop_completion_rate > 0.0 and candidate is not None and candidate.stats.completion_rate >= args.stop_completion_rate


def source_for_action(env, flat_action: int) -> str:
    pair = env.pairs[flat_action // env.n_ops]
    op = flat_action % env.n_ops
    return f"rule_action:{pair.key}:op{op}"


def generate_recipes(env, pairs, expected_counts, focus_pairs: set[str], args: argparse.Namespace, prefix: str | None = None, depth: int = 0) -> list[Recipe]:
    env.pairs = env.normalize_orders(pairs)
    actions = legal_count_preserving_actions(env, env.pairs, expected_counts)
    actions.sort(key=lambda action: action_priority(env, action, focus_pairs))
    recipes: list[Recipe] = []
    seen: set[tuple[tuple[str, str, int], ...]] = set()
    for action in actions:
        env.pairs = env.normalize_orders(pairs)
        candidate_pairs = env.apply_action(action)
        if not same_layer_counts(candidate_pairs, env.layer_names, expected_counts):
            continue
        signature = eval_arc.signature(candidate_pairs)
        if signature in seen or signature == eval_arc.signature(pairs):
            continue
        seen.add(signature)
        source = source_for_action(env, action)
        if prefix:
            source = f"{prefix} + {source}"
        recipes.append(Recipe(candidate_pairs, source, depth + 1))
    return recipes


def trace_payload(index: int, recipe: Recipe, candidate: eval_arc.RouteCandidate | None) -> dict[str, Any]:
    return {
        "eval_index": index,
        "source": recipe.source,
        "depth": recipe.depth,
        "stats": asdict(candidate.stats) if candidate is not None else None,
        "missing_nets": list(candidate.missing_nets) if candidate is not None else None,
        "run_name": candidate.name if candidate is not None else None,
    }


def train(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    start_time = time.monotonic()
    run_dir = args.output_root / f"{args.tag}_rule_pair_layer_order_seed{args.seed}_budget{args.eval_budget}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    initial_pairs, footer_lines, layer_names, initial_candidate, board_data, fixed_pair_targets, initial_source = prepare_initial_state(run_dir, args)
    expected_counts = layer_pair_counts(initial_pairs, layer_names)
    env = make_env(run_dir, board_data, initial_pairs, footer_lines, layer_names, initial_candidate.stats, fixed_pair_targets)

    eval_used = 0
    first_full_eval = 0 if initial_candidate.stats.routed_nets == initial_candidate.stats.total_nets else None
    best_partial = initial_candidate
    best_full = initial_candidate if first_full_eval == 0 else None
    best_wire = best_full
    early_stop_eval = 0 if completion_target_reached(best_partial, args) else None
    cache: dict[tuple[tuple[str, str, int], ...], eval_arc.RouteCandidate | None] = {eval_arc.signature(initial_pairs): initial_candidate}
    queued: set[tuple[tuple[str, str, int], ...]] = set(cache)
    expanded: set[tuple[tuple[str, str, int], ...]] = set()

    net_to_pair = {pair.neg_net: pair.key for pair in initial_pairs}
    net_to_pair.update({pair.pos_net: pair.key for pair in initial_pairs})
    focus_pairs = {net_to_pair[net] for net in initial_candidate.missing_nets if net in net_to_pair}
    if not focus_pairs:
        focus_pairs = {pair.key for pair in initial_pairs[: min(12, len(initial_pairs))]}
    queue = generate_recipes(env, initial_pairs, expected_counts, focus_pairs, args)
    queue = queue[: max(args.eval_budget * 4, 32)]
    for recipe in queue:
        queued.add(eval_arc.signature(recipe.pairs))

    trace_path = run_dir / "rule_trace.jsonl"
    with trace_path.open("w", encoding="utf-8") as trace:
        while eval_used < args.eval_budget and queue and early_stop_eval is None:
            recipe = queue.pop(0)
            signature = eval_arc.signature(recipe.pairs)
            if signature in cache:
                continue
            eval_used += 1
            candidate = eval_arc.evaluate_pairs(run_dir, footer_lines, layer_names, recipe.pairs, f"rule_{eval_used:05d}", recipe.source)
            cache[signature] = candidate
            trace.write(json.dumps(trace_payload(eval_used, recipe, candidate), ensure_ascii=False) + "\n")
            trace.flush()
            previous_key = eval_arc.partial_key(best_partial)
            best_partial, best_full, best_wire, first_full_eval = update_best_arc(candidate, eval_used, best_partial, best_full, best_wire, first_full_eval)
            if early_stop_eval is None and completion_target_reached(best_partial, args):
                early_stop_eval = eval_used
            if candidate is None or early_stop_eval is not None:
                continue
            if recipe.depth >= args.max_chain_length or eval_arc.partial_key(candidate) < previous_key or signature in expanded:
                continue
            expanded.add(signature)
            next_focus = {net_to_pair[net] for net in candidate.missing_nets if net in net_to_pair} or focus_pairs
            for followup in generate_recipes(env, candidate.pairs, expected_counts, next_focus, args, recipe.source, recipe.depth)[: args.beam_width]:
                follow_sig = eval_arc.signature(followup.pairs)
                if follow_sig in cache or follow_sig in queued:
                    continue
                queued.add(follow_sig)
                queue.append(followup)

    eval_arc.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval_arc.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval_arc.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = {
        "tag": args.tag,
        "variant": "rule_pair_layer_order_search",
        "seed": args.seed,
        "algorithm": "rule",
        "eval_budget": args.eval_budget,
        "eval_used": eval_used,
        "stop_completion_rate": args.stop_completion_rate,
        "early_stop_reached": early_stop_eval is not None,
        "early_stop_eval": early_stop_eval,
        "elapsed_seconds": time.monotonic() - start_time,
        "reached_full": best_full is not None,
        "first_full_eval": first_full_eval,
        "best_partial": eval_arc.candidate_to_json(best_partial),
        "best_full": eval_arc.candidate_to_json(best_full),
        "best_wire_full": eval_arc.candidate_to_json(best_wire),
        "fixed_csv": str(args.fixed_csv) if args.fixed_csv is not None else None,
        "fixed_pair_count": len(fixed_pair_targets),
        "initial_order": str(args.initial_order) if args.initial_order is not None else None,
        "initial_source": initial_source,
        "primary_best_kind": primary_best_kind,
        "primary_best_order_txt": primary_best_order_txt,
        "initial_layer_pair_counts": expected_counts,
        "beam_width": args.beam_width,
        "max_chain_length": args.max_chain_length,
        "rule_trace": trace_path.name,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, initial_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    print(f"Arc exe rule pair layer/order run written to: {run_dir}")
    return summary


if __name__ == "__main__":
    train(parse_args())
