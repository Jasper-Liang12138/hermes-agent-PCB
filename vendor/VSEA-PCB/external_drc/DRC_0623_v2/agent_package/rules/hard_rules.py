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
from rules.rule_helpers.board_filters import (
    _get_valid_bga_pads,
    _is_named_net,
    _is_nc_pad,
    _is_no_connect_net,
    _is_signal_net,
)
from rules.rule_helpers.connectivity import (
    _touching_segments_for_pad,
    _touching_arcs_for_pad,
    _touching_vias_for_pad,
    _count_initial_escape_choices,
    _canonical_point,
    _pad_connected_segment_endpoints,
    _branch_reaches_outside_bbox,
    _classify_branch_escape,
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
from rules.rule_helpers.pad_segment_geometry import (
    arc_bbox,
    arc_intersects_pad,
    arc_intersects_via,
    bboxes_overlap,
    copper_layer_names,
    pad_bbox,
    pad_copper_layers,
    segment_bbox,
    segment_intersects_pad,
    segment_intersects_via,
    via_bbox,
    via_intersects_pad,
    via_touches_layer,
)

from rules.rule_helpers.connectivity import (
    _estimate_bga_pitch,
    _build_bga_bbox,
    _point_outside_bbox,
    _endpoint_is_connected,
    _collect_escape_endpoints_for_pad,
)


def _target_bga_bbox(board):
    component = getattr(board, "target_bga", "") or ""
    if not component:
        return None
    pitch = _estimate_bga_pitch(board, component)
    margin = pitch * 0.5 if pitch else 0.0
    return _build_bga_bbox(board, component, margin=margin)


def _point_inside_bga_bbox(point, bbox, tolerance=1e-9):
    if point is None or bbox is None:
        return False
    min_x, max_x, min_y, max_y = bbox
    return (
        min_x - tolerance <= point[0] <= max_x + tolerance
        and min_y - tolerance <= point[1] <= max_y + tolerance
    )


def _segment_bbox_overlaps_bga(seg_bbox, bga_bbox, tolerance=1e-9):
    min_x, min_y, max_x, max_y = seg_bbox
    bga_min_x, bga_max_x, bga_min_y, bga_max_y = bga_bbox
    return not (
        max_x < bga_min_x - tolerance
        or bga_max_x < min_x - tolerance
        or max_y < bga_min_y - tolerance
        or bga_max_y < min_y - tolerance
    )


def _build_bbox_grid(items, cell_size=1.0):
    grid = defaultdict(list)
    for item, item_bbox in items:
        for cell in _grid_cells_for_bbox(item_bbox, cell_size):
            grid[cell].append((item, item_bbox))
    return grid


def _query_bbox_grid(grid, query_bbox, cell_size=1.0):
    seen = set()
    for cell in _grid_cells_for_bbox(query_bbox, cell_size):
        for item, item_bbox in grid.get(cell, []):
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            yield item, item_bbox


def _segment_conflict_inside_bga(s1, s2, intersection, bga_bbox):
    if intersection is not None:
        return _point_inside_bga_bbox(intersection, bga_bbox)

    # Collinear overlap has no single intersection point. Its common bbox must
    # overlap the target BGA region.
    b1 = _seg_bbox(s1)
    b2 = _seg_bbox(s2)
    overlap = (
        max(b1[0], b2[0]),
        max(b1[1], b2[1]),
        min(b1[2], b2[2]),
        min(b1[3], b2[3]),
    )
    if overlap[0] > overlap[2] or overlap[1] > overlap[3]:
        return False
    return _segment_bbox_overlaps_bga(overlap, bga_bbox)


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
        touching_arcs = _touching_arcs_for_pad(board, pad)
        touching_vias = _touching_vias_for_pad(board, pad)

        if log_fn:
            log_fn(
                f"[P1] pad={pad.id} comp={pad.component} net={pad.net} layer={pad.layer} "
                f"touching_segments={len(touching_segments)} "
                f"touching_arcs={len(touching_arcs)} "
                f"touching_vias={len(touching_vias)} "
            )

        if not touching_segments and not touching_arcs and not touching_vias:
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
                        "touching_arc_count": len(touching_arcs),
                        "touching_via_count": len(touching_vias),
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
                    component=pad.component or "",
                    pad_id=pad.id or "",
                    extra={
                        "component": pad.component or "",
                        "pad_id": pad.id or "",
                    },
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
    bga_bbox = _target_bga_bbox(board)
    if bga_bbox is None:
        return issues

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
        layer_segments = [
            seg for seg in layer_segments
            if _segment_bbox_overlaps_bga(bbox_map[seg.id], bga_bbox)
        ]
        if len(layer_segments) < 2:
            continue

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
                    if not _segment_conflict_inside_bga(s1, s2, ip, bga_bbox):
                        continue

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
                                "target_bga": getattr(board, "target_bga", ""),
                                "bga_bbox": bga_bbox,
                                "seg1_net": s1.net,
                                "seg2_net": s2.net,
                                "seg1_start": s1.start,
                                "seg1_end": s1.end,
                                "seg2_start": s2.start,
                                "seg2_end": s2.end,
                                "cell": cell,
                                #"related_components": [comp1, comp2],
                                #"related_pad_ids": [pad1_id, pad2_id],
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
        seed_points.extend(
            (via.x, via.y) for via in _touching_vias_for_pad(board, pad)
        )
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

            status, endpoint = _classify_branch_escape(
                board, pad, seed, bbox, log_fn=log_fn
            )

            if log_fn:
                log_fn("debug", f"[H3] pad={pad.id} seed={seed} escape_status={status}")

            if status in ("outside", "pad"):
                continue

            if status == "via":
                via = endpoint
                issues.append(
                    Issue(
                        rule="HR_CONNECT_VIA_ONLY_ESCAPE",
                        severity="WARNING",
                        message=(
                            f"BGA pad {pad.id} on net {pad.net} reaches via {via.id} "
                            "but does not escape outside the BGA region or reach another pad."
                        ),
                        obj1=pad.id,
                        obj2=via.id,
                        net=pad.net,
                        layer=pad.layer,
                        x=via.x,
                        y=via.y,
                        category="connectivity",
                        suggestion=(
                            "Continue routing from this via outside the BGA region or to the intended pad."
                        ),
                        component=pad.component or "",
                        pad_id=pad.id or "",
                        extra={
                            "pad_id": pad.id,
                            "component": pad.component,
                            "via_id": via.id,
                            "seed_point": seed,
                            "escape_status": "via_only",
                        },
                    )
                )
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


def check_h6_pad_segment_crossing(board, log_fn=None) -> List[Issue]:
    """Check physical overlap between pads and segments on different nets."""
    issues = []
    target_bga = getattr(board, "target_bga", "") or ""
    bga_bbox = _target_bga_bbox(board)
    if not target_bga or bga_bbox is None:
        return issues

    segments_by_layer = defaultdict(list)
    for segment in board.segments:
        current_segment_bbox = segment_bbox(segment)
        if (
            _is_named_net(segment.net)
            and not _is_no_connect_net(segment.net)
            and _segment_bbox_overlaps_bga(current_segment_bbox, bga_bbox)
        ):
            segments_by_layer[segment.layer].append(
                (segment, current_segment_bbox)
            )

    candidate_count = 0
    for pad in board.pads:
        if (
            not getattr(pad, "is_bga", False)
            or pad.component != target_bga
            or _is_nc_pad(pad)
            or not _is_named_net(pad.net)
        ):
            continue

        current_pad_bbox = pad_bbox(pad)
        for layer in pad_copper_layers(board, pad):
            for segment, current_segment_bbox in segments_by_layer.get(layer, []):
                if segment.net == pad.net:
                    continue
                if not bboxes_overlap(current_pad_bbox, current_segment_bbox):
                    continue

                candidate_count += 1
                if not segment_intersects_pad(segment, pad):
                    continue

                issues.append(
                    Issue(
                        rule="HR_DRC_PAD_SEGMENT_CROSSING",
                        severity="ERROR",
                        issue_id=(
                            f"HR_DRC_PAD_SEGMENT_CROSSING_"
                            f"{pad.id}_{segment.id}_{layer}"
                        ),
                        message=(
                            f"Pad {pad.id} ({pad.net}) overlaps segment "
                            f"{segment.id} ({segment.net}) on layer {layer}."
                        ),
                        obj1=pad.id,
                        obj2=segment.id,
                        net=f"{pad.net}|{segment.net}",
                        layer=layer,
                        x=pad.x,
                        y=pad.y,
                        category="drc",
                        suggestion=(
                            "Reroute the segment so it does not overlap a pad on another net."
                        ),
                        component=pad.component or "",
                        pad_id=pad.id or "",
                        extra={
                            "target_bga": target_bga,
                            "bga_bbox": bga_bbox,
                            "pad_net": pad.net,
                            "pad_shape": pad.shape,
                            "pad_size": [pad.size_x, pad.size_y],
                            "pad_rotation": pad.rotation,
                            "segment_net": segment.net,
                            "segment_start": segment.start,
                            "segment_end": segment.end,
                            "segment_width": segment.width,
                        },
                    )
                )

    if log_fn:
        log_fn(
            "debug",
            f"[H6] pad-segment candidates={candidate_count} issues={len(issues)}",
        )

    return issues


def check_h7_pad_arc_crossing(board, log_fn=None) -> List[Issue]:
    """Check target-BGA pads against different-net copper arcs."""
    issues = []
    target_bga = getattr(board, "target_bga", "") or ""
    bga_bbox = _target_bga_bbox(board)
    if not target_bga or bga_bbox is None:
        return issues

    arcs_by_layer = defaultdict(list)
    for arc in board.arcs:
        current_bbox = arc_bbox(arc)
        if (
            _is_named_net(arc.net)
            and not _is_no_connect_net(arc.net)
            and _segment_bbox_overlaps_bga(current_bbox, bga_bbox)
        ):
            arcs_by_layer[arc.layer].append((arc, current_bbox))
    arc_grids = {layer: _build_bbox_grid(items) for layer, items in arcs_by_layer.items()}

    candidate_count = 0
    for pad in board.pads:
        if not (
            getattr(pad, "is_bga", False)
            and pad.component == target_bga
            and not _is_nc_pad(pad)
            and _is_named_net(pad.net)
        ):
            continue
        current_pad_bbox = pad_bbox(pad)
        for layer in pad_copper_layers(board, pad):
            for arc, current_arc_bbox in _query_bbox_grid(
                arc_grids.get(layer, {}), current_pad_bbox
            ):
                if arc.net == pad.net or not bboxes_overlap(current_pad_bbox, current_arc_bbox):
                    continue
                candidate_count += 1
                if not arc_intersects_pad(arc, pad):
                    continue
                issues.append(Issue(
                    rule="HR_DRC_PAD_ARC_CROSSING",
                    severity="ERROR",
                    issue_id=f"HR_DRC_PAD_ARC_CROSSING_{pad.id}_{arc.id}_{layer}",
                    message=f"Pad {pad.id} ({pad.net}) overlaps arc {arc.id} ({arc.net}) on layer {layer}.",
                    obj1=pad.id,
                    obj2=arc.id,
                    net=f"{pad.net}|{arc.net}",
                    layer=layer,
                    x=pad.x,
                    y=pad.y,
                    category="drc",
                    suggestion="Reroute the arc so it does not overlap a pad on another net.",
                    component=pad.component or "",
                    pad_id=pad.id or "",
                    extra={"target_bga": target_bga, "pad_net": pad.net, "arc_net": arc.net,
                           "arc_start": arc.start, "arc_mid": arc.mid, "arc_end": arc.end,
                           "arc_width": arc.width},
                ))
    if log_fn:
        log_fn("debug", f"[H7] pad-arc candidates={candidate_count} issues={len(issues)}")
    return issues


def check_h8_pad_via_crossing(board, log_fn=None) -> List[Issue]:
    """Check target-BGA pads against different-net via copper."""
    issues = []
    target_bga = getattr(board, "target_bga", "") or ""
    bga_bbox = _target_bga_bbox(board)
    if not target_bga or bga_bbox is None:
        return issues

    vias = []
    for via in board.vias:
        current_bbox = via_bbox(via)
        if (
            _is_named_net(via.net)
            and not _is_no_connect_net(via.net)
            and _segment_bbox_overlaps_bga(current_bbox, bga_bbox)
        ):
            vias.append((via, current_bbox))
    via_grid = _build_bbox_grid(vias)
    candidate_count = 0

    for pad in board.pads:
        if not (
            getattr(pad, "is_bga", False)
            and pad.component == target_bga
            and not _is_nc_pad(pad)
            and _is_named_net(pad.net)
        ):
            continue
        current_pad_bbox = pad_bbox(pad)
        for via, current_via_bbox in _query_bbox_grid(via_grid, current_pad_bbox):
            if via.net == pad.net or not bboxes_overlap(current_pad_bbox, current_via_bbox):
                continue
            common_layers = [
                layer for layer in pad_copper_layers(board, pad)
                if via_touches_layer(board, via, layer)
            ]
            if not common_layers:
                continue
            candidate_count += 1
            if not via_intersects_pad(via, pad):
                continue
            layer = ",".join(common_layers)
            issues.append(Issue(
                rule="HR_DRC_PAD_VIA_CROSSING",
                severity="ERROR",
                issue_id=f"HR_DRC_PAD_VIA_CROSSING_{pad.id}_{via.id}",
                message=f"Pad {pad.id} ({pad.net}) overlaps via {via.id} ({via.net}) on {layer}.",
                obj1=pad.id,
                obj2=via.id,
                net=f"{pad.net}|{via.net}",
                layer=layer,
                x=via.x,
                y=via.y,
                category="drc",
                suggestion="Move the via so its copper does not overlap a pad on another net.",
                component=pad.component or "",
                pad_id=pad.id or "",
                extra={"target_bga": target_bga, "pad_net": pad.net, "via_net": via.net,
                       "via_size": via.size, "via_type": via.type, "common_layers": common_layers},
            ))
    if log_fn:
        log_fn("debug", f"[H8] pad-via candidates={candidate_count} issues={len(issues)}")
    return issues


def check_h9_via_track_crossing(board, log_fn=None) -> List[Issue]:
    """Check vias in the target BGA region against different-net segments and arcs."""
    issues = []
    bga_bbox = _target_bga_bbox(board)
    if not getattr(board, "target_bga", "") or bga_bbox is None:
        return issues

    tracks_by_layer = defaultdict(list)
    for segment in board.segments:
        current_bbox = segment_bbox(segment)
        if (
            _is_named_net(segment.net)
            and not _is_no_connect_net(segment.net)
            and _segment_bbox_overlaps_bga(current_bbox, bga_bbox)
        ):
            tracks_by_layer[segment.layer].append((segment, current_bbox, "segment"))
    for arc in board.arcs:
        current_bbox = arc_bbox(arc)
        if (
            _is_named_net(arc.net)
            and not _is_no_connect_net(arc.net)
            and _segment_bbox_overlaps_bga(current_bbox, bga_bbox)
        ):
            tracks_by_layer[arc.layer].append((arc, current_bbox, "arc"))

    track_grids = {}
    track_kinds = {}
    for layer, entries in tracks_by_layer.items():
        track_grids[layer] = _build_bbox_grid([(track, bbox) for track, bbox, _ in entries])
        track_kinds[layer] = {id(track): kind for track, _, kind in entries}

    candidate_count = 0
    seen = set()
    for via in board.vias:
        if not _is_named_net(via.net) or _is_no_connect_net(via.net):
            continue
        current_via_bbox = via_bbox(via)
        if not _segment_bbox_overlaps_bga(current_via_bbox, bga_bbox):
            continue
        for layer in copper_layer_names(board):
            if not via_touches_layer(board, via, layer):
                continue
            for track, current_track_bbox in _query_bbox_grid(
                track_grids.get(layer, {}), current_via_bbox
            ):
                if track.net == via.net or not bboxes_overlap(current_via_bbox, current_track_bbox):
                    continue
                key = (via.id, track.id, layer)
                if key in seen:
                    continue
                seen.add(key)
                candidate_count += 1
                kind = track_kinds[layer][id(track)]
                intersects = (
                    segment_intersects_via(track, via)
                    if kind == "segment"
                    else arc_intersects_via(track, via)
                )
                if not intersects:
                    continue
                issues.append(Issue(
                    rule="HR_DRC_VIA_TRACK_CROSSING",
                    severity="ERROR",
                    issue_id=f"HR_DRC_VIA_TRACK_CROSSING_{via.id}_{track.id}_{layer}",
                    message=f"Via {via.id} ({via.net}) overlaps {kind} {track.id} ({track.net}) on layer {layer}.",
                    obj1=via.id,
                    obj2=track.id,
                    net=f"{via.net}|{track.net}",
                    layer=layer,
                    x=via.x,
                    y=via.y,
                    category="drc",
                    suggestion="Reroute the track or move the via to remove the different-net copper overlap.",
                    extra={"target_bga": board.target_bga, "via_net": via.net,
                           "track_net": track.net, "track_kind": kind,
                           "via_size": via.size, "track_width": track.width},
                ))
    if log_fn:
        log_fn("debug", f"[H9] via-track candidates={candidate_count} issues={len(issues)}")
    return issues
