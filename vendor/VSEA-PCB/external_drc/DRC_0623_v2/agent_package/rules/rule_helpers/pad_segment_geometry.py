import math
from types import SimpleNamespace
from typing import List, Tuple

from geometry.geometry_utils import segments_intersect

Point = Tuple[float, float]


def copper_layer_names(board) -> List[str]:
    name_to_id = getattr(board, "layers_table", {}).get("name_to_id", {})
    return [name for name in name_to_id if name.endswith(".Cu")]


def pad_copper_layers(board, pad) -> List[str]:
    layers = getattr(pad, "layers", []) or [getattr(pad, "layer", "")]
    if "*.Cu" in layers or "F&B.Cu" in layers or getattr(pad, "pad_type", "") != "smd":
        return copper_layer_names(board)
    return [layer for layer in layers if layer.endswith(".Cu")]


def pad_bbox(pad):
    half_x = max(pad.size_x, 0.0) / 2.0
    half_y = max(pad.size_y, 0.0) / 2.0
    angle = math.radians(getattr(pad, "rotation", 0.0))
    extent_x = abs(math.cos(angle)) * half_x + abs(math.sin(angle)) * half_y
    extent_y = abs(math.sin(angle)) * half_x + abs(math.cos(angle)) * half_y
    return (
        pad.x - extent_x,
        pad.y - extent_y,
        pad.x + extent_x,
        pad.y + extent_y,
    )


def segment_bbox(segment):
    radius = max(segment.width, 0.0) / 2.0
    return (
        min(segment.start[0], segment.end[0]) - radius,
        min(segment.start[1], segment.end[1]) - radius,
        max(segment.start[0], segment.end[0]) + radius,
        max(segment.start[1], segment.end[1]) + radius,
    )


def via_bbox(via):
    radius = max(via.size, 0.0) / 2.0
    return (via.x - radius, via.y - radius, via.x + radius, via.y + radius)


def via_touches_layer(board, via, layer: str) -> bool:
    if not layer.endswith(".Cu"):
        return False
    if (getattr(via, "type", "THROUGH") or "THROUGH").upper() == "THROUGH":
        return True

    name_to_id = getattr(board, "layers_table", {}).get("name_to_id", {})
    layer_id = name_to_id.get(layer, -1)
    start_id = getattr(via, "start_layer_id", -1)
    end_id = getattr(via, "end_layer_id", -1)
    if start_id < 0:
        start_id = name_to_id.get(getattr(via, "start_layer", ""), -1)
    if end_id < 0:
        end_id = name_to_id.get(getattr(via, "end_layer", ""), -1)
    if layer_id < 0 or start_id < 0 or end_id < 0:
        return layer in {getattr(via, "start_layer", ""), getattr(via, "end_layer", "")}
    return min(start_id, end_id) <= layer_id <= max(start_id, end_id)


def _arc_polyline(arc) -> List[Point]:
    start, mid, end = arc.start, arc.mid, arc.end
    ax, ay = start
    bx, by = mid
    cx, cy = end
    determinant = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) <= 1e-12:
        return [start, mid, end]

    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    center_x = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / determinant
    center_y = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / determinant
    radius = math.hypot(ax - center_x, ay - center_y)
    if radius <= 1e-12:
        return [start, mid, end]

    start_angle = math.atan2(ay - center_y, ax - center_x)
    mid_angle = math.atan2(by - center_y, bx - center_x)
    end_angle = math.atan2(cy - center_y, cx - center_x)
    tau = 2.0 * math.pi
    ccw_sweep = (end_angle - start_angle) % tau
    ccw_to_mid = (mid_angle - start_angle) % tau
    sweep = ccw_sweep if ccw_to_mid <= ccw_sweep + 1e-10 else ccw_sweep - tau

    # Limit both angle and chord length so narrow clearance violations are retained.
    max_angle = math.radians(2.0)
    max_chord_angle = 2.0 * math.asin(min(1.0, 0.01 / radius))
    step_angle = min(max_angle, max_chord_angle) if max_chord_angle > 1e-9 else max_angle
    steps = min(4096, max(2, int(math.ceil(abs(sweep) / step_angle))))
    points = []
    for index in range(steps + 1):
        angle = start_angle + sweep * index / steps
        points.append((center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)))
    points[0] = start
    points[-1] = end
    return points


def arc_bbox(arc):
    points = _arc_polyline(arc)
    radius = max(arc.width, 0.0) / 2.0
    return (
        min(point[0] for point in points) - radius,
        min(point[1] for point in points) - radius,
        max(point[0] for point in points) + radius,
        max(point[1] for point in points) + radius,
    )


def bboxes_overlap(first, second, tolerance=1e-9):
    return not (
        first[2] < second[0] - tolerance
        or second[2] < first[0] - tolerance
        or first[3] < second[1] - tolerance
        or second[3] < first[1] - tolerance
    )


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-24:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _segment_distance(a1: Point, a2: Point, b1: Point, b2: Point) -> float:
    if segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(
        _point_segment_distance(a1, b1, b2),
        _point_segment_distance(a2, b1, b2),
        _point_segment_distance(b1, a1, a2),
        _point_segment_distance(b2, a1, a2),
    )


def _to_pad_local(pad, point: Point) -> Point:
    dx = point[0] - pad.x
    dy = point[1] - pad.y
    angle = math.radians(-getattr(pad, "rotation", 0.0))
    return (
        dx * math.cos(angle) - dy * math.sin(angle),
        dx * math.sin(angle) + dy * math.cos(angle),
    )


def _rectangle_edges(width: float, height: float):
    half_x = width / 2.0
    half_y = height / 2.0
    points = [
        (-half_x, -half_y),
        (half_x, -half_y),
        (half_x, half_y),
        (-half_x, half_y),
    ]
    return list(zip(points, points[1:] + points[:1]))


def _capsule_intersects_rectangle(start, end, radius, width, height):
    half_x = width / 2.0
    half_y = height / 2.0
    if (
        -half_x <= start[0] <= half_x
        and -half_y <= start[1] <= half_y
    ) or (
        -half_x <= end[0] <= half_x
        and -half_y <= end[1] <= half_y
    ):
        return True
    return any(
        _segment_distance(start, end, edge_start, edge_end) <= radius + 1e-9
        for edge_start, edge_end in _rectangle_edges(width, height)
    )


def segment_intersects_pad(segment, pad) -> bool:
    if pad.size_x <= 0 or pad.size_y <= 0:
        return False

    start = _to_pad_local(pad, segment.start)
    end = _to_pad_local(pad, segment.end)
    trace_radius = max(segment.width, 0.0) / 2.0
    shape = (pad.shape or "").lower()

    if shape == "circle" and abs(pad.size_x - pad.size_y) <= 1e-9:
        pad_radius = pad.size_x / 2.0
        return _point_segment_distance((0.0, 0.0), start, end) <= pad_radius + trace_radius + 1e-9

    if shape == "oval":
        pad_radius = min(pad.size_x, pad.size_y) / 2.0
        half_axis = max(pad.size_x, pad.size_y) / 2.0 - pad_radius
        if pad.size_x >= pad.size_y:
            pad_start = (-half_axis, 0.0)
            pad_end = (half_axis, 0.0)
        else:
            pad_start = (0.0, -half_axis)
            pad_end = (0.0, half_axis)
        return (
            _segment_distance(start, end, pad_start, pad_end)
            <= pad_radius + trace_radius + 1e-9
        )

    # Rect, roundrect, trapezoid and custom pads use their rotated bounding
    # rectangle here. This is conservative for non-rectangular custom shapes.
    return _capsule_intersects_rectangle(
        start,
        end,
        trace_radius,
        pad.size_x,
        pad.size_y,
    )


def via_intersects_pad(via, pad) -> bool:
    proxy = SimpleNamespace(
        start=(via.x, via.y),
        end=(via.x, via.y),
        width=max(via.size, 0.0),
    )
    return segment_intersects_pad(proxy, pad)


def segment_intersects_via(segment, via) -> bool:
    return _point_segment_distance(
        (via.x, via.y), segment.start, segment.end
    ) <= max(via.size, 0.0) / 2.0 + max(segment.width, 0.0) / 2.0 + 1e-9


def arc_intersects_pad(arc, pad) -> bool:
    points = _arc_polyline(arc)
    return any(
        segment_intersects_pad(
            SimpleNamespace(start=start, end=end, width=max(arc.width, 0.0)),
            pad,
        )
        for start, end in zip(points, points[1:])
    )


def arc_intersects_via(arc, via) -> bool:
    points = _arc_polyline(arc)
    limit = max(via.size, 0.0) / 2.0 + max(arc.width, 0.0) / 2.0 + 1e-9
    return any(
        _point_segment_distance((via.x, via.y), start, end) <= limit
        for start, end in zip(points, points[1:])
    )
