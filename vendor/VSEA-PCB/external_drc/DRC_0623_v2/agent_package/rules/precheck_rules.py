from collections import Counter, defaultdict
from typing import Dict, List
from unittest import result

from model import board
from model.board import Board
from rules.rule_helpers.board_filters import (get_signal_net_summary,
                                              get_signal_net_summary_from_external_file,
                                              )

from rules.differential_rules import (
    _normalize_diff_pair_name,
    get_declared_diff_pairs,
)

import re
import json

def _count_bga_pads_by_component(board: Board) -> Dict[str, int]:
    counter = Counter()

    for pad in board.pads:
        if getattr(pad, "is_bga", False):
            counter[pad.component] += 1

    return dict(counter)

def resolve_target_bga(board, preferred_bga: str = "") -> dict:
    comp_counts = _count_bga_pads_by_component(board)

    if not comp_counts:
        board.target_bga = ""
        board.target_bga_pad_count = 0
        return {
            "target_bga": "",
            "target_bga_pad_count": 0,
            "candidate_bgas": [],
            "status": "warning",
            "reason": "No BGA component detected."
        }

    # 默认选 BGA pad 数最多的器件
    sorted_items = sorted(comp_counts.items(), key=lambda kv: kv[1], reverse=True)
    if preferred_bga:
        for comp, cnt in sorted_items:
            if comp.upper() == preferred_bga.upper():
                board.target_bga = comp
                board.target_bga_pad_count = cnt
                return {
                    "target_bga": comp,
                    "target_bga_pad_count": cnt,
                    "candidate_bgas": [
                        {"component": c, "bga_pad_count": n}
                        for c, n in sorted_items
                    ],
                    "status": "ok",
                    "reason": f"Preferred BGA selected: {comp}",
                }

        # 如果指定值不在候选里，先回退到默认最大 BGA
        candidates = ", ".join(comp for comp, _ in sorted_items)
        raise ValueError(
            f"Preferred BGA '{preferred_bga}' was not found among detected BGA components. "
            f"Available candidates: {candidates}"
        )

    # 默认逻辑：选 pad 数最多的 BGA
    target_bga, pad_count = sorted_items[0]
    board.target_bga = target_bga
    board.target_bga_pad_count = pad_count
    return {
        "target_bga": target_bga,
        "target_bga_pad_count": pad_count,
        "candidate_bgas": [
            {"component": c, "bga_pad_count": n}
            for c, n in sorted_items
        ],
        "status": "ok",
        "reason": "",
    }
    
   

def get_signal_net_summary_from_external_file(board, net_roles_path: str) -> dict:
    with open(net_roles_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    board_net_names = {n.name for n in board.nets}

    signal_nets = set(cfg.get("signal_nets", []))
    power_nets = set(cfg.get("power_nets", []))
    ground_nets = set(cfg.get("ground_nets", []))
    ignore_nets = set(cfg.get("ignore_nets", []))

    configured_nets = signal_nets | power_nets | ground_nets | ignore_nets

    unknown_configured = sorted(configured_nets - board_net_names)

    # 只保留PCB中真实存在的网络
    signal_nets = sorted(signal_nets & board_net_names)
    power_nets = sorted(power_nets & board_net_names)
    ground_nets = sorted(ground_nets & board_net_names)
    ignore_nets = sorted(ignore_nets & board_net_names)

    filtered_out_nets = sorted(set(power_nets) | set(ground_nets) | set(ignore_nets))

    # 如果外部文件没有完全列出所有网络，未配置网络可以走脚本兜底
    fallback = get_signal_net_summary(board)
    fallback_signal = set(fallback["signal_nets"])
    fallback_filtered = set(fallback["filtered_out_nets"])

    unconfigured_nets = board_net_names - configured_nets

    auto_signal = sorted(unconfigured_nets & fallback_signal)
    auto_filtered = sorted(unconfigured_nets & fallback_filtered)

    final_signal_nets = sorted(set(signal_nets) | set(auto_signal))
    final_filtered_out_nets = sorted(set(filtered_out_nets) | set(auto_filtered))

    return {
        "all_net_count": len(board_net_names),
        "signal_net_count": len(final_signal_nets),
        "filtered_out_net_count": len(final_filtered_out_nets),
        "signal_nets": final_signal_nets,
        "filtered_out_nets": final_filtered_out_nets,
        "power_nets": power_nets,
        "ground_nets": ground_nets,
        "ignore_nets": ignore_nets,
        "auto_signal_nets": auto_signal,
        "auto_filtered_out_nets": auto_filtered,
        "unknown_configured_nets": unknown_configured,
        "source": "external_file_with_fallback",
    }

def filter_signal_nets(board, net_roles_path: str = "") -> dict:
    if net_roles_path:
        result = get_signal_net_summary_from_external_file(board, net_roles_path)
    else:
        result = get_signal_net_summary(board)

    board.signal_nets = result["signal_nets"]
    board.filtered_out_nets = result["filtered_out_nets"]
    board.power_nets = result.get("power_nets", [])
    board.ground_nets = result.get("ground_nets", [])
    board.ignore_nets = result.get("ignore_nets", [])

    return result


def extract_candidate_diff_pairs(board: Board) -> dict:
    pair_map = defaultdict(dict)

    for net_name in getattr(board, "signal_nets", []):
        base, side = _normalize_diff_pair_name(net_name)
        if not base or not side:
            continue
        pair_map[base][side] = net_name

    pairs = []
    incomplete_pairs = []

    for base, sides in sorted(pair_map.items()):
        item = {
            "pair_name": base,
            "p_net": sides.get("P", ""),
            "n_net": sides.get("N", ""),
        }

        if item["p_net"] and item["n_net"]:
            pairs.append(item)
        else:
            incomplete_pairs.append(item)

    board.candidate_diff_pairs = pairs

    return {
        "candidate_diff_pair_count": len(pairs),
        "candidate_diff_pairs": pairs,
        "incomplete_diff_pair_count": len(incomplete_pairs),
        "incomplete_diff_pairs": incomplete_pairs,
    }

def run_precheck(board, preferred_bga: str = "", net_roles_path: str = "") -> dict:
    target_bga_info = resolve_target_bga(board, preferred_bga=preferred_bga)
    signal_net_info = filter_signal_nets(board, net_roles_path=net_roles_path)
    diff_pair_info = extract_candidate_diff_pairs(board)
    checked_diff_pairs = get_declared_diff_pairs(board)
    board.checked_diff_pairs = checked_diff_pairs

    summary = {
    "target_bga": target_bga_info.get("target_bga", ""),
    "target_bga_pad_count": target_bga_info.get("target_bga_pad_count", 0),

    "candidate_bgas": target_bga_info.get("candidate_bgas", []),

    "all_net_count": signal_net_info.get("all_net_count", 0),
    "signal_net_count": signal_net_info.get("signal_net_count", 0),
    "filtered_out_net_count": signal_net_info.get("filtered_out_net_count", 0),

    # 新增
    "power_net_count": len(signal_net_info.get("power_nets", [])),
    "ground_net_count": len(signal_net_info.get("ground_nets", [])),
    "ignore_net_count": len(signal_net_info.get("ignore_nets", [])),

    "net_filter_source": signal_net_info.get("source", "auto_script"),

    "candidate_diff_pair_count": diff_pair_info.get("candidate_diff_pair_count", 0),
    "candidate_diff_pairs": diff_pair_info.get("candidate_diff_pairs", []),
    "incomplete_diff_pair_count": diff_pair_info.get("incomplete_diff_pair_count", 0),
    "checked_diff_pair_count": len(checked_diff_pairs),
    "checked_diff_pairs": checked_diff_pairs,

    "precheck_status": "ok" if target_bga_info.get("target_bga") else "warning",
    }

    board.precheck_summary = summary
    return summary
