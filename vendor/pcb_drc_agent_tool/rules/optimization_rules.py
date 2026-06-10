
from typing import List

from geometry.geometry_utils import dist
from model.issue import Issue
from rules.rule_helpers.board_filters import _get_valid_bga_pads
from rules.rule_helpers.bga_geometry import (
    _build_bga_component_meta,
    _is_inner_pad,
    _is_outer_pad,
    _build_bga_pitch_map,
    _build_bga_cell_centers,
    _pad_ring_level,
)
from rules.rule_helpers.connectivity import (
    _find_fanout_via_for_pad,
    _first_top_fanout_segments_for_pad,
    _nearest_center_distance,
)

MIL_TO_MM = 0.0254
WIDTH_RULES = {}
VIA_RULES = {}
CENTER_RATIO = 0.20
DIAG_RATIO = 0.15
VIA_SIZE_TOL = 0.02

def check_p3_inner_pad_must_use_via(board) -> List[Issue]:
    """检查内层 BGA pad 是否使用了 via 进行逃逸（即第一跳必须是 via）。优化规则"""
    issues = []
    RING_THRESHOLD = 2

    bga_meta = _build_bga_component_meta(board)

    for pad in _get_valid_bga_pads(board):
        ring = _pad_ring_level(pad, bga_meta)
        if ring is None:
            continue
        if ring < RING_THRESHOLD:
            continue
      #  if not _is_inner_pad(pad, bga_meta):
      #      continue

        fanout_via = _find_fanout_via_for_pad(board, pad)

        if fanout_via is None:
            issues.append(
                Issue(
                    rule="OR_INNER_PAD_PREFER_VIA",
                    severity="WARNING",
                    message=(
                        f"Inner BGA pad {pad.id} on net {pad.net} does not use a reachable fanout via."
                    ),
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=pad.x,
                    y=pad.y,
                    category="routing_preference",
                    suggestion="Prefer a via-based fanout strategy for deeper-ring BGA pads.",
                    extra={
                        "pad_id": pad.id,
                        "component": pad.component,
                        "ring_level": ring,
                        "ring_threshold": RING_THRESHOLD,
                    }
                )
            )

    return issues

def check_p4_via_at_cell_center(board) -> List[Issue]: 
    issues = []

    bga_meta = _build_bga_component_meta(board)
    pitch_map = _build_bga_pitch_map(board)
    centers_map = _build_bga_cell_centers(board)

    for pad in _get_valid_bga_pads(board):
        if not _is_inner_pad(pad, bga_meta):
            continue

        pitch = pitch_map.get(pad.component)
        if pitch is None:
            continue

        via = _find_fanout_via_for_pad(board, pad)
        if via is None:
            continue

        centers = centers_map.get(pad.component, [])
        dmin = _nearest_center_distance(centers, via)
        if dmin is None:
            continue

        tol = pitch * CENTER_RATIO
        if dmin > tol:
            issues.append(
                Issue(
                    rule="OR_VIA_NOT_AT_CELL_CENTER",
                    severity="ERROR",
                    message=(
                        f"Fanout via {via.id} for inner BGA pad {pad.id} is too far from any 4-pad cell center."
                    ),
                    obj1=pad.id,
                    obj2=via.id,
                    net=pad.net,
                    x=via.x,
                    y=via.y,
                )
            )

    return issues


def check_p5_via_45deg(board) -> List[Issue]:
    issues = []

    bga_meta = _build_bga_component_meta(board)
    pitch_map = _build_bga_pitch_map(board)

    for pad in _get_valid_bga_pads(board):
        if not _is_inner_pad(pad, bga_meta):
            continue

        pitch = pitch_map.get(pad.component)
        if pitch is None:
            continue

        via = _find_fanout_via_for_pad(board, pad)
        if via is None:
            continue

        dx = via.x - pad.x
        dy = via.y - pad.y
        tol = pitch * DIAG_RATIO

        if abs(abs(dx) - abs(dy)) > tol:
            issues.append(
                Issue(
                    rule="OR_VIA_NOT_45_DEG",
                    severity="ERROR",
                    message=(
                        f"Fanout via {via.id} for inner BGA pad {pad.id} does not follow the 45-degree escape direction."
                    ),
                    obj1=pad.id,
                    obj2=via.id,
                    net=pad.net,
                    x=via.x,
                    y=via.y,
                )
            )

    return issues


def check_w123_top_fanout_width(board) -> List[Issue]:
    issues = []
    pitch_map = _build_bga_pitch_map(board)

    for pad in _get_valid_bga_pads(board):
        pitch = pitch_map.get(pad.component)
        if pitch not in WIDTH_RULES:
            continue

        rule_name, min_width = WIDTH_RULES[pitch]
        top_segs = _first_top_fanout_segments_for_pad(board, pad)

        for seg in top_segs:
            if seg.width < min_width:
                issues.append(
                    Issue(
                        rule=rule_name,
                        severity="ERROR",
                        message=(
                            f"Top fanout segment {seg.id} for BGA pad {pad.id} is too narrow: "
                            f"{seg.width:.4f} mm < required {min_width:.4f} mm."
                        ),
                        obj1=pad.id,
                        obj2=seg.id,
                        net=pad.net,
                        layer=seg.layer,
                    )
                )

    return issues


def check_w9_w10_w11_fanout_via_size(board) -> List[Issue]:
    issues = []

    bga_meta = _build_bga_component_meta(board)
    pitch_map = _build_bga_pitch_map(board)

    for pad in _get_valid_bga_pads(board):
        pitch = pitch_map.get(pad.component)
        if pitch is None:
            continue

        via = _find_fanout_via_for_pad(board, pad)
        if via is None:
            continue

        if pitch == 0.65:
            if _is_outer_pad(pad, bga_meta):
                rule_name = "OR_FANOUT_VIA_SIZE_INVALID"
                target_drill = 8 * MIL_TO_MM
                target_size = 16 * MIL_TO_MM
            else:
                rule_name = "OR_FANOUT_VIA_SIZE_INVALID"
                target_drill = 8 * MIL_TO_MM
                target_size = 14 * MIL_TO_MM
        elif pitch in VIA_RULES:
            rule_name, target_drill, target_size = VIA_RULES[pitch]
        else:
            continue

        if abs(via.drill - target_drill) > VIA_SIZE_TOL or abs(via.size - target_size) > VIA_SIZE_TOL:
            issues.append(
                Issue(
                rule=rule_name,
                severity="ERROR",
                message=(
                    f"Fanout via {via.id} for BGA pad {pad.id} has invalid size: "
                    f"drill={via.drill:.4f} mm, size={via.size:.4f} mm. "
                    f"(Detected BGA pitch={pitch:.2f} mm, "
                    f"expected drill={target_drill:.4f} mm, size={target_size:.4f} mm)"
                ),
                obj1=pad.id,
                obj2=via.id,
                net=pad.net,
                x=via.x,
                y=via.y,
                )
        )

    return issues

