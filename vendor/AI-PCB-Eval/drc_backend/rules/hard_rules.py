import time

from collections import defaultdict

from typing import List, Dict


from geometry.geometry_utils import (
    dist,
    near_point,
    segments_intersect,
    shared_endpoint,
    line_intersection_point,
)
from model.issue import Issue
from rules.rule_helpers.board_filters import _is_named_net, _is_signal_net, _get_valid_bga_pads
from rules.rule_helpers.connectivity import (
    _touching_segments_for_pad,
    _touching_vias_for_pad,
    _count_initial_escape_choices,
    _canonical_point,
    _pad_connected_segment_endpoints,
    _branch_reaches_outside_bbox,
    _build_via_spatial_index,
    _branch_has_fork_inside_bbox
)

from rules.rule_helpers.p7_geometry import (
    _build_segments_by_layer,
    _choose_grid_cell_size,
    _seg_bbox,
    _bbox_overlap,
    _grid_cells_for_bbox,
)

from rules.rule_helpers.connectivity import (
    _estimate_bga_pitch,
    _build_bga_bbox,
    _point_outside_bbox,
    _endpoint_is_connected,
    _collect_escape_endpoints_for_pad,
)


"""
def check_p1_all_named_bga_pads_escaped(board) -> List[Issue]:
    issues = []

    for pad in _get_valid_bga_pads(board):
        touching_segments = _touching_segments_for_pad(board, pad)
        touching_vias = _touching_vias_for_pad(board, pad)

        if not touching_segments and not touching_vias:
            issues.append(
                Issue(
                    rule="HR_CONNECT_PAD_NOT_ESCAPED",
                    severity="ERROR",
                    message=f"BGA pad {pad.id} on net {pad.net} has no initial escape connection.",
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=pad.x,
                    y=pad.y,
                )
            )

    return issues
"""
def check_p1_all_named_bga_pads_escaped(board, log_fn=None) -> List[Issue]:
    issues = []
    pads = _get_valid_bga_pads(board)

    if log_fn:
        log_fn(f"[P1] start: valid_bga_pads={len(pads)}")

    for pad in pads:
        touching_segments = _touching_segments_for_pad(board, pad)
        #touching_vias = _touching_vias_for_pad(board, pad)

        if log_fn:
            log_fn(
                f"[P1] pad={pad.id} comp={pad.component} net={pad.net} layer={pad.layer} "
                f"touching_segments={len(touching_segments)} "
            )

        if not touching_segments:
            issues.append(
                Issue(
                    rule="HR_CONNECT_PAD_NOT_ESCAPED",
                    severity="ERROR",
                    message=(
                        f"BGA pad {pad.id} on net {pad.net} has no initial escape connection."
                    ),
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=pad.x,
                    y=pad.y,
                    category="connectivity",
                    suggestion="Add a fanout segment or via starting from this BGA pad.",
                    extra={
                        "pad_id": pad.id,
                        "component": pad.component,
                        "pad_layer": pad.layer,
                        "touching_segment_count": len(touching_segments),
                       # "touching_via_count": len(touching_vias),
                    },
                )
            )

    if log_fn:
        log_fn(f"[P1] done: issues={len(issues)}")

    return issues


def check_p2_single_escape_per_bga_pad(board, log_fn=None) -> List[Issue]:
    issues = []
    """
    for pad in _get_valid_bga_pads(board):
        branch_count, touching_segments, touching_vias = _count_escape_branches(board, pad)

        if branch_count > 1:
            issues.append(
                Issue(
                    rule="HR_TOPO_MULTIPLE_ESCAPE",
                    severity="ERROR",
                    message=(
                        f"BGA pad {pad.id} on net {pad.net} has {branch_count} escape branches; "
                        f"only one is allowed."
                    ),
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=pad.x,
                    y=pad.y,
                )
            )
            """
    #via_index = _build_via_spatial_index(board)
    for pad in _get_valid_bga_pads(board):
        choice_count, touching_segments  = _count_initial_escape_choices(board, pad)
        if log_fn:
            seg_ids = [getattr(seg, "id", None) for seg in touching_segments]
            log_fn(
                "debug", 
                f"[P2] pad={pad.id} comp={pad.component} net={pad.net} "
                f"layer={pad.layer} choice_count={choice_count} "
                f"touching_segments={seg_ids}"
            )
        
        if choice_count > 1:
            issues.append(
                Issue(
                    rule="HR_TOPO_MULTIPLE_ESCAPE",
                    severity="ERROR",
                    message=(
                        f"BGA pad {pad.id} on net {pad.net} has {choice_count} initial escape choices "
                        f"on layer {pad.layer}; only one is allowed."
                    ),
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=pad.x,
                    y=pad.y,
                )
            )

    return issues


def check_p7_segment_crossing(board, log_fn=None) -> List[Issue]:
    """
    检查同层、异网 segment 是否发生交叉/重叠。
    当前版本不检查同网 segment 之间的几何关系。
    优化版：
    1) 按层分组
    2) 预计算 bbox
    3) 建立网格索引，减少候选对
    4) 候选对再做精确几何判断
    """
    issues = []

    t0_total = time.perf_counter()
    by_layer = _build_segments_by_layer(board)

    total_layers = len(by_layer)
    total_segments = sum(len(v) for v in by_layer.values())

    if log_fn:
        log_fn("debug", f"[P7] start segment crossing check: layers={total_layers}, segments={total_segments}")

    total_candidate_pairs = 0
    total_bbox_pass = 0
    total_exact_checks = 0

    for layer, layer_segments in sorted(by_layer.items(), key=lambda kv: kv[0]):
        t0_layer = time.perf_counter()

        if len(layer_segments) < 2:
            if log_fn:
                log_fn("debug", f"[P7] layer={layer}: segments={len(layer_segments)} skip(<2)")
            continue

        # 1) bbox cache
        bbox_map = {seg.id: _seg_bbox(seg) for seg in layer_segments}

        # 2) grid index
        cell_size = _choose_grid_cell_size(layer_segments)
        grid = defaultdict(list)

        t0_grid = time.perf_counter()
        for seg in layer_segments:
            bbox = bbox_map[seg.id]
            for cell in _grid_cells_for_bbox(bbox, cell_size):
                grid[cell].append(seg)
        t_grid = time.perf_counter() - t0_grid

        # 3) candidate pair generation
        seen_pairs = set()
        layer_candidate_pairs = 0
        layer_bbox_pass = 0
        layer_exact_checks = 0
        layer_issue_count = 0

        t0_cmp = time.perf_counter()

        for cell, segs_in_cell in grid.items():
            n = len(segs_in_cell)
            if n < 2:
                continue

            for i in range(n):
                s1 = segs_in_cell[i]
                for j in range(i + 1, n):
                    s2 = segs_in_cell[j]

                    if s1.layer != s2.layer:
                        continue

                    if s1.net == s2.net:
                        continue

                    pair_key = tuple(sorted((s1.id, s2.id)))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    layer_candidate_pairs += 1

                    b1 = bbox_map[s1.id]
                    b2 = bbox_map[s2.id]
                    if not _bbox_overlap(b1, b2):
                        continue
                    layer_bbox_pass += 1

                    layer_exact_checks += 1
                    if not segments_intersect(s1.start, s1.end, s2.start, s2.end):
                        continue

                    ip = line_intersection_point(s1.start, s1.end, s2.start, s2.end)

                    x = None
                    y = None
                    if ip is not None:
                        x, y = ip

                    # 如果是 shared endpoint 但没有明确交点，也给个坐标
                    if ip is None and shared_endpoint(s1.start, s1.end, s2.start, s2.end):
                        candidates = [s1.start, s1.end, s2.start, s2.end]
                        x, y = candidates[0]

                    issues.append(
                        Issue(
                            rule="HR_DRC_SEGMENT_CROSSING",
                            severity="ERROR",
                            message=(
                                f"Segments {s1.id} ({s1.net}) and {s2.id} ({s2.net}) "
                                f"cross or overlap on layer {layer}."
                            ),
                            obj1=s1.id,
                            obj2=s2.id,
                            net=f"{s1.net}|{s2.net}",
                            layer=layer,
                            x=x,
                            y=y,
                            category="drc",
                            suggestion="Reroute one of the segments so different nets do not cross on the same copper layer.",
                            extra={
                                "seg1_net": s1.net,
                                "seg2_net": s2.net,
                                "seg1_start": s1.start,
                                "seg1_end": s1.end,
                                "seg2_start": s2.start,
                                "seg2_end": s2.end,
                                "cell": cell,
                            },
                        )
                    )
                    layer_issue_count += 1

        t_cmp = time.perf_counter() - t0_cmp
        t_layer = time.perf_counter() - t0_layer

        total_candidate_pairs += layer_candidate_pairs
        total_bbox_pass += layer_bbox_pass
        total_exact_checks += layer_exact_checks

        if log_fn:
            log_fn(
                "debug", 
                f"[P7] layer={layer}: "
                f"segments={len(layer_segments)} "
                f"cell_size={cell_size:.3f} "
                f"grid_cells={len(grid)} "
                f"candidate_pairs={layer_candidate_pairs} "
                f"bbox_pass={layer_bbox_pass} "
                f"exact_checks={layer_exact_checks} "
                f"issues={layer_issue_count} "
                f"grid_time={t_grid:.3f}s "
                f"cmp_time={t_cmp:.3f}s "
                f"layer_time={t_layer:.3f}s"
            )

    total_time = time.perf_counter() - t0_total

    if log_fn:
        log_fn(
            "debug", 
            f"[P7] done: "
            f"layers={total_layers} "
            f"segments={total_segments} "
            f"candidate_pairs={total_candidate_pairs} "
            f"bbox_pass={total_bbox_pass} "
            f"exact_checks={total_exact_checks} "
            f"issues={len(issues)} "
            f"total_time={total_time:.3f}s"
        )

    return issues
"""
def check_h2_dangling_segment(board) -> List[Issue]:
    

    for pad in _get_valid_bga_pads(board):
        #bbox = _build_bga_bbox(board, pad.component)
        pitch = _estimate_bga_pitch(board, pad.component)
        margin = pitch * 0.5 if pitch else 0.0
        bbox = _build_bga_bbox(board, pad.component, margin=margin)
        segs = _touching_segments_for_pad(board, pad)

        for seg in segs:
            if not _is_named_net(seg.net):
                continue

            #start_ok = _endpoint_is_connected(board, seg, seg.start, pad)
            #end_ok = _endpoint_is_connected(board, seg, seg.end, pad)
            start_is_pad = near_point(seg.start, (pad.x, pad.y))
            end_is_pad = near_point(seg.end, (pad.x, pad.y))

            start_ok = True if start_is_pad else _endpoint_is_connected(board, seg, seg.start, pad)
            end_ok = True if end_is_pad else _endpoint_is_connected(board, seg, seg.end, pad)

            if start_ok and end_ok:
                continue

            #dangling_points = []
            #if not start_ok:
            #    dangling_points.append(seg.start)
            #if not end_ok:
            #   dangling_points.append(seg.end)

            dangling_points = []

            if not start_ok and not _point_outside_bbox(seg.start, bbox):
                dangling_points.append(seg.start)

            if not end_ok and not _point_outside_bbox(seg.end, bbox):
                dangling_points.append(seg.end)

            if not dangling_points:
                continue

            x, y = dangling_points[0]

            issues.append(
                Issue(
                    rule="HR_CONNECT_DANGLING_SEGMENT",
                    severity="ERROR",
                    message=(
                        f"Fanout segment {seg.id} for BGA pad {pad.id} has a dangling endpoint."
                    ),
                    obj1=pad.id,
                    obj2=seg.id,
                    net=pad.net,
                    layer=seg.layer,
                    x=x,
                    y=y,
                    category="connectivity",
                    suggestion="Connect the dangling segment endpoint to the intended fanout path or via.",
                    extra={
                        "pad_id": pad.id,
                        "segment_id": seg.id,
                        "component": pad.component,
                    },
                )
            )

    return issues
"""


def check_h3_branch_escape_incomplete(board, log_fn=None):
    """Check for branches from BGA pads that do not reach a valid escape endpoint outside the BGA region."""
    issues = []

    for pad in _get_valid_bga_pads(board):
        pitch = _estimate_bga_pitch(board, pad.component)
        margin = pitch * 0.5 if pitch else 0.0
        bbox = _build_bga_bbox(board, pad.component, margin=margin)

        if log_fn:
            log_fn(
                "debug",
                f"[H3] pad={pad.id} net={pad.net} comp={pad.component} "
                f"pad_xy=({pad.x:.4f},{pad.y:.4f}) pitch={pitch} margin={margin:.4f} bbox={bbox}"
            )


        seed_points = _pad_connected_segment_endpoints(board, pad)
        if not seed_points:
            continue

        unique_seeds = []
        seen = set()
        for seed in seed_points:
            cseed = _canonical_point(seed)
            if cseed in seen:
                continue
            seen.add(cseed)
            unique_seeds.append(seed)
            if log_fn:
                log_fn("debug", f"[H3] pad={pad.id} unique_seeds={unique_seeds}")


        for seed in unique_seeds:
            if log_fn:
                log_fn("debug", f"[H3] pad={pad.id} checking seed={seed}")

            ok = _branch_reaches_outside_bbox(board, pad, seed, bbox, log_fn=log_fn)

            if log_fn:
                log_fn("debug", f"[H3] pad={pad.id} seed={seed} escape_ok={ok}")

            if ok:
                continue

            if log_fn:
                log_fn("error",
                       f"[H3] failed: pad={pad.id} net={pad.net} comp={pad.component} "
                       f"seed={seed} did not escape outside bbox"

                )

            issues.append(
                Issue(
                    rule="HR_CONNECT_BRANCH_INCOMPLETE",
                    severity="ERROR",
                    message=(
                        f"BGA pad {pad.id} on net {pad.net} has a branch "
                        f"that does not continuously escape outside the BGA region."
                    ),
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=seed[0],
                    y=seed[1],
                    category="connectivity",
                    suggestion=(
                        "Extend this branch continuously so it escapes outside the BGA region without breaking."
                    ),
                    extra={
                        "pad_id": pad.id,
                        "component": pad.component,
                        "seed_point": seed,
                    },
                )
            )

    return issues

def check_h4_endpoint_not_unique(board):
    """Check for BGA pads that reach multiple escape endpoints, which may indicate a branching issue."""
    issues = []

    for pad in _get_valid_bga_pads(board):
        pitch = _estimate_bga_pitch(board, pad.component)
        margin = pitch * 0.5 if pitch else 0.0
        bbox = _build_bga_bbox(board, pad.component, margin=margin)

        endpoints = _collect_escape_endpoints_for_pad(board, pad, bbox)

        if len(endpoints) <= 1:
            continue

        endpoint_ids = sorted(endpoints.keys())
        first_ep = endpoints[endpoint_ids[0]]

        issues.append(
            Issue(
                rule="HR_TOPO_ENDPOINT_NOT_UNIQUE",
                severity="ERROR",
                message=(
                    f"BGA pad {pad.id} on net {pad.net} reaches multiple escape endpoints: "
                    f"{', '.join(endpoint_ids)}"
                ),
                obj1=pad.id,
                net=pad.net,
                layer=pad.layer,
                x=first_ep.x,
                y=first_ep.y,
                category="topology",
                suggestion="Ensure this BGA pad escapes to exactly one external pad endpoint.",
                extra={
                    "pad_id": pad.id,
                    "component": pad.component,
                    "endpoint_ids": endpoint_ids,
                },
            )
        )

    return issues


def check_h5_escape_path_no_fork(board) -> List[Issue]:
    """Check that the escape path from each BGA pad does not have forks (branches)."""
    issues = []

    for pad in _get_valid_bga_pads(board):
        pitch = _estimate_bga_pitch(board, pad.component)
        margin = pitch * 0.5 if pitch else 0.0
        bbox = _build_bga_bbox(board, pad.component, margin=margin)

        seed_points = _pad_connected_segment_endpoints(board, pad)
        if not seed_points:
            continue

        unique_seeds = []
        seen = set()
        for seed in seed_points:
            cseed = _canonical_point(seed)
            if cseed in seen:
                continue
            seen.add(cseed)
            unique_seeds.append(seed)

        for seed in unique_seeds:
            has_fork = _branch_has_fork_inside_bbox(board, pad, seed, bbox)

            if not has_fork:
                continue

            issues.append(
                Issue(
                    rule="HR_CONNECT_ESCAPE_PATH_FORK",
                    severity="ERROR",
                    message=(
                        f"BGA pad {pad.id} on net {pad.net} has a branching escape path inside the BGA region. "
                    ),
                    obj1=pad.id,
                    net=pad.net,
                    layer=pad.layer,
                    x=seed[0],
                    y=seed[1],
                    category="topology",
                    suggestion="Reroute the escape path so it remains a single continuous route inside the BGA region." ,
                    extra={
                        "pad_id": pad.id,
                        "component": pad.component,
                        "seed_point": seed,
                    },
                )
            )


    return issues