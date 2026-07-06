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

from explain_135 import generate_explanation
import rl_135_core as core
import routing_eval_135 as eval135
from train_ga_135 import (
    action_priority,
    choose_primary_best,
    clone_entries,
    export_order_txt,
    layer_entry_counts,
    legal_count_preserving_actions,
    make_env,
    prepare_initial_state,
    same_layer_counts,
    update_best_135,
)

METHOD_ROOT = RULE_DIR / "search_runs" / "rule"


@dataclass
class Recipe:
    entries: list[core.OrderEntry]
    source: str
    depth: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rule-based optimizer for 135 exe layer/order routing.")
    parser.add_argument("--eval-budget", type=int, default=30)
    parser.add_argument("--stop-completion-rate", type=float, default=1.0, help="Stop once best completion_rate reaches this value; use 0 to disable early stop.")
    parser.add_argument("--seed", type=int, default=20260422)
    parser.add_argument("--tag", default="rule_135_exe")
    parser.add_argument("--output-root", type=Path, default=METHOD_ROOT)
    parser.add_argument("--beam-width", type=int, default=8)
    parser.add_argument("--max-chain-length", type=int, default=2)
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


def completion_target_reached(candidate: eval135.RouteCandidate | None, args: argparse.Namespace) -> bool:
    return args.stop_completion_rate > 0.0 and candidate is not None and candidate.stats.completion_rate >= args.stop_completion_rate


def source_for_action(env, flat_action: int) -> str:
    entry = env.entries[flat_action // env.n_ops]
    op = flat_action % env.n_ops
    return f"rule_action:{entry.net}:op{op}"


def generate_recipes(env, entries, expected_counts, focus_nets: set[str], args: argparse.Namespace, prefix: str | None = None, depth: int = 0) -> list[Recipe]:
    env.entries = env.normalize_orders(entries)
    actions = legal_count_preserving_actions(env, env.entries, expected_counts)
    actions.sort(key=lambda action: action_priority(env, action, focus_nets))
    recipes: list[Recipe] = []
    seen: set[tuple[tuple[str, str, int], ...]] = set()
    for action in actions:
        env.entries = env.normalize_orders(entries)
        candidate_entries = env.apply_action(action)
        if not same_layer_counts(candidate_entries, env.layer_names, expected_counts):
            continue
        signature = eval135.signature(candidate_entries)
        if signature in seen or signature == eval135.signature(entries):
            continue
        seen.add(signature)
        source = source_for_action(env, action)
        if prefix:
            source = f"{prefix} + {source}"
        recipes.append(Recipe(candidate_entries, source, depth + 1))
    return recipes


def trace_payload(index: int, recipe: Recipe, candidate: eval135.RouteCandidate | None) -> dict[str, Any]:
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
    run_dir = args.output_root / f"{args.tag}_rule_layer_order_seed{args.seed}_budget{args.eval_budget}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    initial_entries, footer_lines, layer_names, initial_candidate, board_data, fixed_targets, initial_source = prepare_initial_state(run_dir, args)
    expected_counts = layer_entry_counts(initial_entries, layer_names)
    env = make_env(run_dir, board_data, initial_entries, footer_lines, layer_names, initial_candidate.stats, fixed_targets)

    eval_used = 0
    first_full_eval = 0 if initial_candidate.stats.routed_nets == initial_candidate.stats.total_nets else None
    best_partial = initial_candidate
    best_full = initial_candidate if first_full_eval == 0 else None
    best_wire = best_full
    best_via = best_full
    early_stop_eval = 0 if completion_target_reached(best_partial, args) else None
    cache: dict[tuple[tuple[str, str, int], ...], eval135.RouteCandidate | None] = {eval135.signature(initial_entries): initial_candidate}
    queued: set[tuple[tuple[str, str, int], ...]] = set(cache)
    expanded: set[tuple[tuple[str, str, int], ...]] = set()

    focus_nets = set(initial_candidate.missing_nets) or {entry.net for entry in initial_entries[: min(12, len(initial_entries))]}
    queue = generate_recipes(env, initial_entries, expected_counts, focus_nets, args)
    queue = queue[: max(args.eval_budget * 4, 32)]
    for recipe in queue:
        queued.add(eval135.signature(recipe.entries))

    trace_path = run_dir / "rule_trace.jsonl"
    with trace_path.open("w", encoding="utf-8") as trace:
        while eval_used < args.eval_budget and queue and early_stop_eval is None:
            recipe = queue.pop(0)
            signature = eval135.signature(recipe.entries)
            if signature in cache:
                continue
            eval_used += 1
            candidate = eval135.evaluate_entries(run_dir, footer_lines, layer_names, recipe.entries, f"rule_{eval_used:05d}", recipe.source)
            cache[signature] = candidate
            trace.write(json.dumps(trace_payload(eval_used, recipe, candidate), ensure_ascii=False) + "\n")
            trace.flush()
            previous_key = eval135.partial_key(best_partial)
            best_partial, best_full, best_wire, best_via, first_full_eval = update_best_135(candidate, eval_used, best_partial, best_full, best_wire, best_via, first_full_eval)
            if early_stop_eval is None and completion_target_reached(best_partial, args):
                early_stop_eval = eval_used
            if candidate is None or early_stop_eval is not None:
                continue
            if recipe.depth >= args.max_chain_length or eval135.partial_key(candidate) < previous_key or signature in expanded:
                continue
            expanded.add(signature)
            next_focus = set(candidate.missing_nets) or focus_nets
            for followup in generate_recipes(env, candidate.entries, expected_counts, next_focus, args, recipe.source, recipe.depth)[: args.beam_width]:
                follow_sig = eval135.signature(followup.entries)
                if follow_sig in cache or follow_sig in queued:
                    continue
                queued.add(follow_sig)
                queue.append(followup)

    eval135.save_artifacts(best_partial, run_dir / "best_partial", run_final_turn=True)
    eval135.save_artifacts(best_full, run_dir / "best_full", run_final_turn=True)
    eval135.save_artifacts(best_wire, run_dir / "best_wire_full", run_final_turn=True)
    eval135.save_artifacts(best_via, run_dir / "best_via_full", run_final_turn=True)
    primary_best_kind, primary_best = choose_primary_best(best_full, best_partial)
    primary_best_order_txt = export_order_txt(primary_best, run_dir / "best_layer_order.txt")

    summary = {
        "tag": args.tag,
        "variant": "rule_layer_order_search",
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
        "best_partial": eval135.candidate_to_json(best_partial),
        "best_full": eval135.candidate_to_json(best_full),
        "best_wire_full": eval135.candidate_to_json(best_wire),
        "best_via_full": eval135.candidate_to_json(best_via),
        "fixed_csv": str(args.fixed_csv) if args.fixed_csv is not None else None,
        "fixed_net_count": len(fixed_targets),
        "initial_order": str(args.initial_order) if args.initial_order is not None else None,
        "initial_source": initial_source,
        "primary_best_kind": primary_best_kind,
        "primary_best_order_txt": primary_best_order_txt,
        "initial_layer_net_counts": expected_counts,
        "beam_width": args.beam_width,
        "max_chain_length": args.max_chain_length,
        "rule_trace": trace_path.name,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if not args.no_explain:
        explanation_path = generate_explanation(run_dir, summary, initial_candidate, primary_best)
        print(f"Explanation written to: {explanation_path}")
    print(f"135 exe rule layer/order run written to: {run_dir}")
    return summary


if __name__ == "__main__":
    train(parse_args())
