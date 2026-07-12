import math


def _point_distance(p1, p2) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def _dot(ax, ay, bx, by) -> float:
    return ax * bx + ay * by


def _point_to_segment_distance(p, a, b) -> float:
    px, py = p
    ax, ay = a
    bx, by = b

    vx = bx - ax
    vy = by - ay

    wx = px - ax
    wy = py - ay

    length_sq = vx * vx + vy * vy

    if length_sq <= 1e-12:
        return _point_distance(p, a)

    t = _dot(wx, wy, vx, vy) / length_sq
    t = max(0.0, min(1.0, t))

    proj = (ax + t * vx, ay + t * vy)
    return _point_distance(p, proj)


def _orientation(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, c) -> bool:
    eps = 1e-9
    return (
        min(a[0], c[0]) - eps <= b[0] <= max(a[0], c[0]) + eps
        and min(a[1], c[1]) - eps <= b[1] <= max(a[1], c[1]) + eps
    )


def _segments_intersect(a, b, c, d) -> bool:
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)

    eps = 1e-9

    if abs(o1) < eps and _on_segment(a, c, b):
        return True
    if abs(o2) < eps and _on_segment(a, d, b):
        return True
    if abs(o3) < eps and _on_segment(c, a, d):
        return True
    if abs(o4) < eps and _on_segment(c, b, d):
        return True

    return (o1 * o2 < 0) and (o3 * o4 < 0)


def segment_to_segment_distance(seg1, seg2) -> float:
    """
    返回两条线段的最短距离，单位沿用 PCB 坐标单位，一般是 mm。
    """
    a = seg1.start
    b = seg1.end
    c = seg2.start
    d = seg2.end

    if _segments_intersect(a, b, c, d):
        return 0.0

    return min(
        _point_to_segment_distance(a, c, d),
        _point_to_segment_distance(b, c, d),
        _point_to_segment_distance(c, a, b),
        _point_to_segment_distance(d, a, b),
    )

def segment_edge_gap(seg1, seg2) -> float:
    """
    返回两条 segment 铜皮边到边的最小间距。
    如果结果 < 0，说明铜皮重叠/相交。
    """
    center_dist = segment_to_segment_distance(seg1, seg2)
    return center_dist - seg1.width / 2.0 - seg2.width / 2.0

def _vec(seg):
    return (
        seg.end[0] - seg.start[0],
        seg.end[1] - seg.start[1],
    )


def _vec_len(v):
    return math.hypot(v[0], v[1])


def segments_nearly_parallel(seg1, seg2, angle_tol_deg=10.0) -> bool:
    v1 = _vec(seg1)
    v2 = _vec(seg2)

    l1 = _vec_len(v1)
    l2 = _vec_len(v2)

    if l1 <= 1e-12 or l2 <= 1e-12:
        return False

    cross = abs(v1[0] * v2[1] - v1[1] * v2[0])
    sin_angle = cross / (l1 * l2)

    return sin_angle <= math.sin(math.radians(angle_tol_deg))


def _projection_interval(seg, axis):
    p1 = seg.start[0] * axis[0] + seg.start[1] * axis[1]
    p2 = seg.end[0] * axis[0] + seg.end[1] * axis[1]
    return min(p1, p2), max(p1, p2)


def segments_projection_overlap(seg1, seg2, min_overlap=0.0) -> bool:
    v1 = _vec(seg1)
    l1 = _vec_len(v1)

    if l1 <= 1e-12:
        return False

    axis = (v1[0] / l1, v1[1] / l1)

    a1, a2 = _projection_interval(seg1, axis)
    b1, b2 = _projection_interval(seg2, axis)

    overlap = min(a2, b2) - max(a1, b1)

    return overlap >= min_overlap

def segment_projection_overlap_length(seg1, seg2) -> float:
    v1 = _vec(seg1)
    l1 = _vec_len(v1)

    if l1 <= 1e-12:
        return 0.0

    axis = (v1[0] / l1, v1[1] / l1)

    a1, a2 = _projection_interval(seg1, axis)
    b1, b2 = _projection_interval(seg2, axis)

    return max(0.0, min(a2, b2) - max(a1, b1))


def is_valid_parallel_coupled_run(
    seg1,
    seg2,
    angle_tol_deg=10.0,
    min_overlap_mm=0.5,
) -> bool:
    if not segments_nearly_parallel(seg1, seg2, angle_tol_deg=angle_tol_deg):
        return False

    overlap = segment_projection_overlap_length(seg1, seg2)

    return overlap >= min_overlap_mm