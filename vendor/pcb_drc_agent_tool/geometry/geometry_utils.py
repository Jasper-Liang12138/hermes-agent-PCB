import math
from typing import Optional, Tuple

Point = Tuple[float, float]

#定义距离
def dist(p1: Point, p2: Point) -> float:
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])

#定义点是否接近
def near_point(p1: Point, p2: Point, tol: float = 1e-3) -> bool:
    return dist(p1, p2) <= tol

#定义点的方向
def orientation(a: Point, b: Point, c: Point, eps: float = 1e-12) -> int:
    v = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
    if abs(v) < eps:
        return 0
    return 1 if v > 0 else 2

#定义点是否在线段上
def on_segment(a: Point, b: Point, c: Point, eps: float = 1e-9) -> bool:
    return (
        min(a[0], c[0]) - eps <= b[0] <= max(a[0], c[0]) + eps
        and min(a[1], c[1]) - eps <= b[1] <= max(a[1], c[1]) + eps
    )

#定义线段是否相交
def segments_intersect(p1: Point, q1: Point, p2: Point, q2: Point) -> bool:
    o1 = orientation(p1, q1, p2)
    o2 = orientation(p1, q1, q2)
    o3 = orientation(p2, q2, p1)
    o4 = orientation(p2, q2, q1)

    if o1 != o2 and o3 != o4:
        return True

    if o1 == 0 and on_segment(p1, p2, q1):
        return True
    if o2 == 0 and on_segment(p1, q2, q1):
        return True
    if o3 == 0 and on_segment(p2, p1, q2):
        return True
    if o4 == 0 and on_segment(p2, q1, q2):
        return True

    return False

#定义线段是否共线且重叠
def shared_endpoint(p1: Point, q1: Point, p2: Point, q2: Point, tol: float = 1e-4) -> bool:
    return (
        near_point(p1, p2, tol)
        or near_point(p1, q2, tol)
        or near_point(q1, p2, tol)
        or near_point(q1, q2, tol)
    )

#定义线段交点
def line_intersection_point(
    p1: Point, q1: Point, p2: Point, q2: Point, eps: float = 1e-12
) -> Optional[Point]:
    x1, y1 = p1
    x2, y2 = q1
    x3, y3 = p2
    x4, y4 = q2

    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < eps:
        return None

    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return (px, py)
#定义
def normalize_layer_name(layer: str) -> str:
    if not layer:
        return ""
    s = layer.strip().strip('"')

    if s in ("F.Cu", "Top", "TOP", "top"):
        return "Top"
    if s in ("B.Cu", "Bottom", "BOT", "bottom", "bot"):
        return "Bottom"

    return s

def is_top_layer(layer: str) -> bool:
    return normalize_layer_name(layer) == "Top"

def is_bottom_layer(layer: str) -> bool:
    return normalize_layer_name(layer) == "Bottom"