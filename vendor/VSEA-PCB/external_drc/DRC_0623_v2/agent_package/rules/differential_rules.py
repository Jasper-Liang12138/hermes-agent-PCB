from collections import defaultdict
from typing import List, Dict, Tuple
import re
import math
from model import board
from model.issue import Issue
from model.board import Board
from collections import Counter
from rules.rule_helpers.spacing_geometry import (
    segment_edge_gap,
    is_valid_parallel_coupled_run,
    segment_projection_overlap_length,
)


DEFAULT_DIFF_RULE = {
    "name": "DEFAULT_DIFF_RULE",
    "layers": {
        "default": {
            "width_mm": 0.10,
            "width_tol_mm": 0.02,
            "pair_gap_mm": 0.15,
            "pair_gap_tol_mm": 0.03,
        }
    },
    "length_match": {
        "enabled": True,
        "tolerance_mm": 0.127,
    },
}

def _normalize_diff_pair_name(net_name: str):
    name = net_name.strip()

    patterns = [
        # DDR3_EDQSP_2 / DDR3_EDQSN_2 -> DDR3_EDQS_2
        (r"^(.*)P_(\d+)$", lambda m: (f"{m.group(1)}_{m.group(2)}", "P")),
        (r"^(.*)N_(\d+)$", lambda m: (f"{m.group(1)}_{m.group(2)}", "N")),

        # DSP2_SRIO_RXP0 / DSP2_SRIO_RXN0 -> DSP2_SRIO_RX0
        (r"^(.*_RX)P(\d+)$", lambda m: (f"{m.group(1)}{m.group(2)}", "P")),
        (r"^(.*_RX)N(\d+)$", lambda m: (f"{m.group(1)}{m.group(2)}", "N")),

        # DSP2_SRIO_TXP0 / DSP2_SRIO_TXN0 -> DSP2_SRIO_TX0
        (r"^(.*_TX)P(\d+)$", lambda m: (f"{m.group(1)}{m.group(2)}", "P")),
        (r"^(.*_TX)N(\d+)$", lambda m: (f"{m.group(1)}{m.group(2)}", "N")),

        # SATA_RXP / SATA_RXN -> SATA_RX
        (r"^(.*_RX)P$", lambda m: (m.group(1), "P")),
        (r"^(.*_RX)N$", lambda m: (m.group(1), "N")),

        # SATA_TXP / SATA_TXN -> SATA_TX
        (r"^(.*_TX)P$", lambda m: (m.group(1), "P")),
        (r"^(.*_TX)N$", lambda m: (m.group(1), "N")),

        # USB_DP / USB_DN -> USB_D
        (r"^(.*_D)P$", lambda m: (m.group(1), "P")),
        (r"^(.*_D)N$", lambda m: (m.group(1), "N")),

        # PCIE_TX0_P / PCIE_TX0_N -> PCIE_TX0
        (r"^(.*)_P$", lambda m: (m.group(1), "P")),
        (r"^(.*)_N$", lambda m: (m.group(1), "N")),

        # 兼容 + / -
        (r"^(.*)\+$", lambda m: (m.group(1), "P")),
        (r"^(.*)-$", lambda m: (m.group(1), "N")),
    ]

    for pattern, handler in patterns:
        m = re.match(pattern, name, flags=re.IGNORECASE)
        if m:
            base, side = handler(m)
            return base.upper(), side

    return None, None

def _build_via_count_map(board: Board) -> Dict[str, int]:
    counter = Counter()

    for via in board.vias:
        net_name = getattr(via, "net", "")
        if not net_name:
            continue
        counter[net_name] += 1

    return dict(counter)

def _segment_length(seg) -> float:
    x1, y1 = seg.start
    x2, y2 = seg.end
    return math.hypot(x2 - x1, y2 - y1)


def _net_routed_length(board: Board, net_name: str) -> float:
    total = 0.0

    for seg in board.segments:
        if getattr(seg, "net", "") != net_name:
            continue
        total += _segment_length(seg)

    return total

def _get_diff_rule_for_pair(board: Board, pair_name: str) -> dict:
    diff_rule_by_pair = getattr(board, "constraint_indexes", {}).get("diff_rule_by_pair", {})
    rule_name = diff_rule_by_pair.get(pair_name, "")

    for rule in getattr(board, "diff_rules", []) or []:
        if rule.get("name", "") == rule_name:
            rule = dict(rule)
            rule["source"] = "external_routing_rules"
            return rule

    default_rule = dict(DEFAULT_DIFF_RULE)
    default_rule["source"] = "default_diff_rule"
    return default_rule

def _collect_target_bga_pad_nets(board: Board) -> List[str]:
    target_bga = getattr(board, "target_bga", "") or ""
    if not target_bga:
        return []

    nets = set()

    for pad in board.pads:
        if not getattr(pad, "is_bga", False):
            continue
        if pad.component != target_bga:
            continue
        if not getattr(pad, "net", ""):
            continue
        nets.add(pad.net)

    return sorted(nets)

def extract_target_bga_diff_pairs(board: Board) -> dict:
    target_nets = _collect_target_bga_pad_nets(board)
    pair_map = defaultdict(dict)

    for net_name in target_nets:
        base, side = _normalize_diff_pair_name(net_name)
        if not base or not side:
            continue
        pair_map[base][side] = net_name

    complete_pairs = []
    incomplete_pairs = []

    for base, sides in sorted(pair_map.items()):
        item = {
            "pair_name": base,
            "p_net": sides.get("P", ""),
            "n_net": sides.get("N", ""),
        }

        if item["p_net"] and item["n_net"]:
            complete_pairs.append(item)
        else:
            incomplete_pairs.append(item)

    return {
        "target_bga": getattr(board, "target_bga", ""),
        "target_bga_pad_net_count": len(target_nets),
        "target_bga_diff_pair_count": len(complete_pairs),
        "target_bga_diff_pairs": complete_pairs,
        "target_bga_incomplete_diff_pair_count": len(incomplete_pairs),
        "target_bga_incomplete_diff_pairs": incomplete_pairs,
    }

def get_declared_diff_pairs(board: Board) -> List[dict]:
    """
    合并外部 diff_pairs.json 与自动识别差分对。
    外部配置优先；自动识别用于补充外部没有声明的差分对。
    """

    checked = getattr(board, "checked_diff_pairs", []) or []
    if checked:
        return checked
    
    declared = getattr(board, "diff_pairs", []) or []

    merged = []
    seen_pair_names = set()
    seen_net_pairs = set()

    # 1. 外部 diff_pairs.json 优先
    for item in declared:
        pair_name = item.get("name", "") or item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        if not pair_name or not p_net or not n_net:
            continue

        key_name = pair_name.upper()
        key_nets = tuple(sorted([p_net, n_net]))

        merged.append({
            "pair_name": pair_name,
            "p_net": p_net,
            "n_net": n_net,
            "source": "external_diff_pairs",
        })

        seen_pair_names.add(key_name)
        seen_net_pairs.add(key_nets)

    # 2. 自动识别补充
    pair_info = extract_target_bga_diff_pairs(board)
    auto_pairs = pair_info.get("target_bga_diff_pairs", [])

    for item in auto_pairs:
        pair_name = item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        if not pair_name or not p_net or not n_net:
            continue

        key_name = pair_name.upper()
        key_nets = tuple(sorted([p_net, n_net]))

        # 外部已经声明过则跳过
        if key_name in seen_pair_names:
            continue
        if key_nets in seen_net_pairs:
            continue

        merged.append({
            "pair_name": pair_name,
            "p_net": p_net,
            "n_net": n_net,
            "source": "auto_target_bga",
        })

        seen_pair_names.add(key_name)
        seen_net_pairs.add(key_nets)

    return merged
def _count_vias_of_net(board: Board, net_name: str) -> int:
    count = 0

    for via in board.vias:
        if getattr(via, "net", "") == net_name:
            count += 1

    return count


def _get_layer_rule(layers_rule: dict, layer: str) -> dict:
    if not layers_rule:
        return {}

    if layer in layers_rule:
        return layers_rule[layer]

    return layers_rule.get("default", {})

def check_d1_pair_net_not_found(board: Board, log_fn=None) -> List[Issue]:
    issues = []

    pairs = get_declared_diff_pairs(board)
    board_net_names = {n.name for n in board.nets}

    if log_fn:
        log_fn(
            "info",
            f"[DIFF D1] check declared diff pair nets: pairs={len(pairs)}"
        )

    for item in pairs:
        pair_name = item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        if not p_net:
            issues.append(
                Issue(
                    rule="DR_DIFF_PAIR_NET_NOT_FOUND",
                    severity="ERROR",
                    message=(
                        f"Differential pair {pair_name} has empty p_net in external constraints."
                    ),
                    net=n_net,
                    category="constraint_validation",
                    suggestion="Fix diff_pairs.json: p_net must be provided.",
                    extra={
                        "pair_name": pair_name,
                        "p_net": p_net,
                        "n_net": n_net,
                        "missing_field": "p_net",
                    },
                )
            )
        elif p_net not in board_net_names:
            issues.append(
                Issue(
                    rule="DR_DIFF_PAIR_NET_NOT_FOUND",
                    severity="ERROR",
                    message=(
                        f"Differential pair {pair_name}: p_net '{p_net}' is not found in PCB."
                    ),
                    net=p_net,
                    category="constraint_validation",
                    suggestion="Check diff_pairs.json or PCB net naming.",
                    extra={
                        "pair_name": pair_name,
                        "side": "P",
                        "net": p_net,
                    },
                )
            )

        if not n_net:
            issues.append(
                Issue(
                    rule="DR_DIFF_PAIR_NET_NOT_FOUND",
                    severity="ERROR",
                    message=(
                        f"Differential pair {pair_name} has empty n_net in external constraints."
                    ),
                    net=p_net,
                    category="constraint_validation",
                    suggestion="Fix diff_pairs.json: n_net must be provided.",
                    extra={
                        "pair_name": pair_name,
                        "p_net": p_net,
                        "n_net": n_net,
                        "missing_field": "n_net",
                    },
                )
            )
        elif n_net not in board_net_names:
            issues.append(
                Issue(
                    rule="DR_DIFF_PAIR_NET_NOT_FOUND",
                    severity="ERROR",
                    message=(
                        f"Differential pair {pair_name}: n_net '{n_net}' is not found in PCB."
                    ),
                    net=n_net,
                    category="constraint_validation",
                    suggestion="Check diff_pairs.json or PCB net naming.",
                    extra={
                        "pair_name": pair_name,
                        "side": "N",
                        "net": n_net,
                    },
                )
            )

    return issues

def check_d2_pair_via_count_mismatch(board: Board, log_fn=None) -> List[Issue]:
    issues = []

    complete_pairs = get_declared_diff_pairs(board)
    via_count_map = _build_via_count_map(board)
    target_bga = getattr(board, "target_bga", "")

    if log_fn:
        log_fn(
            "info",
            f"[DIFF D2] declared_pairs={len(complete_pairs)} target_bga={target_bga}"
        )

    for item in complete_pairs:
        pair_name = item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        if not p_net or not n_net:
            continue

        p_via_count = via_count_map.get(p_net, 0)
        n_via_count = via_count_map.get(n_net, 0)

        if log_fn:
            log_fn(
                "info",
                f"[DIFF D2] pair={pair_name} "
                f"p_net={p_net} p_vias={p_via_count} "
                f"n_net={n_net} n_vias={n_via_count}"
            )

        if p_via_count == n_via_count:
            continue

        issues.append(
            Issue(
                rule="DR_PAIR_VIA_COUNT_MISMATCH",
                severity="WARNING",
                message=(
                    f"Differential pair {pair_name} has mismatched via counts: "
                    f"P-side={p_via_count}, N-side={n_via_count}."
                ),
                net=f"{p_net}|{n_net}",
                category="pair_symmetry",
                suggestion="Try to align the layer-transition strategy so both branches use matched via counts.",
                component=target_bga,
                extra={
                    "pair_name": pair_name,
                    "p_net": p_net,
                    "n_net": n_net,
                    "p_via_count": p_via_count,
                    "n_via_count": n_via_count,
                    "source": "external_constraints",
                },
            )
        )

    return issues


def check_d3_diff_length_mismatch(board: Board, log_fn=None) -> List[Issue]:
    issues = []

    pairs = get_declared_diff_pairs(board)

    if log_fn:
        log_fn("info", f"[DIFF D3] length match check: pairs={len(pairs)}")

    for item in pairs:
        pair_name = item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        if not p_net or not n_net:
            continue

        rule = _get_diff_rule_for_pair(board, pair_name)
        length_rule = rule.get("length_match", {}) if rule else {}

        if not length_rule.get("enabled", False):
            if log_fn:
                log_fn("info", f"[DIFF D3] pair={pair_name} length_match disabled or missing")
            continue

        tolerance = float(length_rule.get("tolerance_mm", 0.0))

        p_len = _net_routed_length(board, p_net)
        n_len = _net_routed_length(board, n_net)
        delta = abs(p_len - n_len)

        if log_fn:
            log_fn(
                "info",
                f"[DIFF D3] pair={pair_name} "
                f"p_net={p_net} p_len={p_len:.4f} "
                f"n_net={n_net} n_len={n_len:.4f} "
                f"delta={delta:.4f} tol={tolerance:.4f}"
            )

        if delta <= tolerance:
            continue

        issues.append(
            Issue(
                rule="DR_DIFF_LENGTH_MISMATCH",
                severity="ERROR",
                message=(
                    f"Differential pair {pair_name} length mismatch: "
                    f"P={p_len:.4f} mm, N={n_len:.4f} mm, "
                    f"delta={delta:.4f} mm > tolerance={tolerance:.4f} mm."
                ),
                net=f"{p_net}|{n_net}",
                category="diff_length",
                suggestion="Tune the shorter side or reroute the pair so P/N routed lengths are matched.",
                extra={
                    "pair_name": pair_name,
                    "p_net": p_net,
                    "n_net": n_net,
                    "p_length_mm": round(p_len, 6),
                    "n_length_mm": round(n_len, 6),
                    "delta_mm": round(delta, 6),
                    "tolerance_mm": tolerance,
                    "rule_name": rule.get("name", ""),
                },
            )
        )

    return issues

def check_d4_diff_width_invalid(board: Board, log_fn=None) -> List[Issue]:
    issues = []

    pairs = get_declared_diff_pairs(board)

    if log_fn:
        log_fn("info", f"[DIFF D4] width check: pairs={len(pairs)}")

    for item in pairs:
        pair_name = item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        rule = _get_diff_rule_for_pair(board, pair_name)
        layer_rules = rule.get("layers", {}) if rule else {}

        for seg in board.segments:
            if seg.net not in {p_net, n_net}:
                continue

            layer_rule = _get_layer_rule(layer_rules, seg.layer)
            if not layer_rule:
                continue

            target = float(layer_rule.get("width_mm", 0.0))
            tol = float(layer_rule.get("width_tol_mm", 0.0))

            if target <= 0:
                continue

            delta = abs(seg.width - target)

            if log_fn:
                log_fn(
                    "debug",
                    f"[DIFF D4] pair={pair_name} seg={seg.id} net={seg.net} "
                    f"layer={seg.layer} width={seg.width:.4f} "
                    f"target={target:.4f} tol={tol:.4f}"
                )

            if delta <= tol:
                continue

            x = (seg.start[0] + seg.end[0]) / 2
            y = (seg.start[1] + seg.end[1]) / 2

            issues.append(
                Issue(
                    rule="DR_DIFF_WIDTH_INVALID",
                    severity="ERROR",
                    message=(
                        f"Differential pair {pair_name} segment {seg.id} on net {seg.net} "
                        f"has invalid width on layer {seg.layer}: "
                        f"{seg.width:.4f} mm, expected {target:.4f}±{tol:.4f} mm."
                    ),
                    obj1=seg.id,
                    net=seg.net,
                    layer=seg.layer,
                    x=x,
                    y=y,
                    category="diff_width",
                    suggestion="Adjust differential trace width according to routing_rules.json.",
                    extra={
                        "pair_name": pair_name,
                        "segment_id": seg.id,
                        "net": seg.net,
                        "actual_width_mm": seg.width,
                        "target_width_mm": target,
                        "tolerance_mm": tol,
                        "delta_mm": delta,
                        "rule_name": rule.get("name", ""),
                    },
                )
            )

    return issues

def check_d5_diff_pair_gap_invalid(board: Board, log_fn=None) -> List[Issue]:

    issues = []

    pairs = get_declared_diff_pairs(board)
    reported_pairs = set()

    if log_fn:
        log_fn("info", f"[DIFF D5] pair gap check: pairs={len(pairs)}")

    for item in pairs:
        pair_name = item.get("pair_name", "")
        p_net = item.get("p_net", "")
        n_net = item.get("n_net", "")

        if not p_net or not n_net:
            continue

        rule = _get_diff_rule_for_pair(board, pair_name)
        layer_rules = rule.get("layers", {}) if rule else {}

        p_segments = [s for s in board.segments if s.net == p_net]
        n_segments = [s for s in board.segments if s.net == n_net]

        checked_count = 0

        for p_seg in p_segments:
            layer_rule = _get_layer_rule(layer_rules, p_seg.layer)
            if not layer_rule:
                continue

            target_gap = float(layer_rule.get("pair_gap_mm", 0.0))
            gap_tol = float(layer_rule.get("pair_gap_tol_mm", 0.0))

            if target_gap <= 0:
                continue

            for n_seg in n_segments:
                if p_seg.layer != n_seg.layer:
                    continue

                overlap_len = segment_projection_overlap_length(p_seg, n_seg)

                if not is_valid_parallel_coupled_run(
                    p_seg,
                    n_seg,
                    angle_tol_deg=10.0,
                    min_overlap_mm=0.5,
                ):
                    continue

                gap = segment_edge_gap(p_seg, n_seg)
                delta = abs(gap - target_gap)
                checked_count += 1

                if log_fn:
                    log_fn(
                        "debug",
                        f"[DIFF D5] pair={pair_name} "
                        f"p_seg={p_seg.id} n_seg={n_seg.id} "
                        f"layer={p_seg.layer} gap={gap:.4f} "
                        f"target={target_gap:.4f} tol={gap_tol:.4f}"
                    )

                if delta <= gap_tol:
                    continue

                x = (
                    p_seg.start[0]
                    + p_seg.end[0]
                    + n_seg.start[0]
                    + n_seg.end[0]
                ) / 4.0
                y = (
                    p_seg.start[1]
                    + p_seg.end[1]
                    + n_seg.start[1]
                    + n_seg.end[1]
                ) / 4.0

                report_key = (
                    pair_name,
                    p_seg.id,
                    n_seg.id,
                    p_seg.layer,
                )

                if report_key in reported_pairs:
                    continue

                reported_pairs.add(report_key)

                issues.append(
                    Issue(
                        rule="DR_DIFF_PAIR_GAP_INVALID",
                        severity="ERROR",
                        message=(
                            f"Differential pair {pair_name} gap invalid on layer {p_seg.layer}: "
                            f"p_seg={p_seg.id}, n_seg={n_seg.id}, "
                            f"{gap:.4f} mm, expected {target_gap:.4f}±{gap_tol:.4f} mm."
                        ),
                        obj1=p_seg.id,
                        obj2=n_seg.id,
                        net=f"{p_net}|{n_net}",
                        layer=p_seg.layer,
                        x=x,
                        y=y,
                        category="diff_gap",
                        suggestion="Adjust spacing between P/N traces according to routing_rules.json.",
                        extra={
                            "pair_name": pair_name,
                            "p_net": p_net,
                            "n_net": n_net,
                            "p_segment_id": p_seg.id,
                            "n_segment_id": n_seg.id,
                            "p_start": p_seg.start,
                            "p_end": p_seg.end,
                            "n_start": n_seg.start,
                            "n_end": n_seg.end,
                            "report_xy_meaning": "average of p/n segment endpoints",
                            "actual_gap_mm": round(gap, 6),
                            "target_gap_mm": target_gap,
                            "tolerance_mm": gap_tol,
                            "delta_mm": round(delta, 6),
                            "rule_name": rule.get("name", ""),
                            "overlap_length_mm": round(overlap_len, 6),
                        },
                    )
                )

        if log_fn:
            log_fn(
                "info",
                f"[DIFF D5] pair={pair_name} checked_parallel_overlaps={checked_count}"
            )

    return issues