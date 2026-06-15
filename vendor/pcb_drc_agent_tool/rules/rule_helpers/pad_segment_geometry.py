import math
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
