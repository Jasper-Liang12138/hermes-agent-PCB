from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import math
import multiprocessing as mp
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence, Tuple

from matplotlib.collections import PatchCollection
from matplotlib.path import Path as MplPath
from matplotlib.patches import Polygon as MplPolygonPatch
from matplotlib.patches import Rectangle as MplRectanglePatch
import numpy as np

try:
    from ._board_parser import (
        BoardData,
        BoardRules,
        IntPoint,
        IntPolygon,
        PCB_UNITS_PER_MM,
        Pad,
        Segment,
        Via,
        Zone,
        mm_to_pcb_units,
        parse_kicad_pcb,
        pcb_units_point_to_mm,
        pcb_units_polygon_to_mm,
        pcb_units_to_mm,
    )
except ImportError:  # pragma: no cover - keeps the file runnable as a script.
    from kicad import (
        BoardData,
        BoardRules,
        IntPoint,
        IntPolygon,
        PCB_UNITS_PER_MM,
        Pad,
        Segment,
        Via,
        Zone,
        mm_to_pcb_units,
        parse_kicad_pcb,
        pcb_units_point_to_mm,
        pcb_units_polygon_to_mm,
        pcb_units_to_mm,
    )


GridEdge = Tuple[Tuple[int, int, int], Tuple[int, int, int]]


@dataclass
class VoxelizationResult:
    grid: np.ndarray
    layer_map: Dict[str, int]
    layer_names: List[str]
    origin_mm: Tuple[float, float]
    origin_units: IntPoint
    resolution_mm: float
    resolution_units: int
    board_bbox_mm: Tuple[float, float, float, float]
    board_bbox_units: Tuple[int, int, int, int]
    units_per_mm: int
    edges: List[GridEdge]


@dataclass
class VectorVisualizationResult:
    layer_names: List[str]
    layer_polygons_units: Dict[str, List[IntPolygon]]
    board_bbox_mm: Tuple[float, float, float, float]
    board_bbox_units: Tuple[int, int, int, int]
    units_per_mm: int


@dataclass
class BBoxNetHit:
    net_id: int
    net_name: str
    layers: List[str]
    segment_count: int = 0
    via_count: int = 0
    pad_count: int = 0
    zone_count: int = 0


@dataclass
class BBoxNetExtractionResult:
    bbox_mm: Tuple[float, float, float, float]
    bbox_units: Tuple[int, int, int, int]
    layers: List[str]
    hits: List[BBoxNetHit]


MISSING_NET_ID_RE = re.compile(r"(?P<net_id>\d+)（(?P<net_name>[^）]+)）\s*存在走线缺失")


def _require_matplotlib():
    try:
        from matplotlib.collections import PatchCollection
        from matplotlib.path import Path as MplPath
        from matplotlib.patches import Polygon as MplPolygonPatch
        from matplotlib.patches import Rectangle as MplRectanglePatch
    except ImportError as exc:
        raise RuntimeError("缺少 matplotlib。请先安装: python -m pip install matplotlib") from exc
    return PatchCollection, MplPath, MplPolygonPatch, MplRectanglePatch


def _require_pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("缺少 Pillow。请先安装: python -m pip install pillow") from exc
    return Image, ImageDraw, ImageFont


def _log_progress(message: str) -> None:
    print(f"[voxelizer] {message}")


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _layer_color_map(layer_names: Sequence[str]) -> Dict[str, str]:
    palette = [
        "#d6a300",  # KiCad-like amber
        "#2ca02c",  # green
        "#17becf",  # cyan
        "#9467bd",  # purple
        "#ff7f0e",  # orange
        "#8c564b",  # brown
        "#e377c2",  # pink
        "#bcbd22",  # olive
        "#7f7f7f",  # gray
        "#00a67d",  # teal
        "#b05cff",  # violet
        "#4c9aff",  # light blue
    ]
    explicit = {
        "F.Cu": "#c0392b",
        "Top": "#c0392b",
        "B.Cu": "#2b6cb0",
        "Bottom": "#2b6cb0",
    }
    result: Dict[str, str] = {}
    next_index = 0
    for layer_name in layer_names:
        if layer_name in explicit:
            result[layer_name] = explicit[layer_name]
        else:
            result[layer_name] = palette[next_index % len(palette)]
            next_index += 1
    return result


VISUAL_PAD_SCALE = 0.84
VISUAL_VIA_SCALE = 0.74


def find_pcb_files(inputs: Sequence[str | Path]) -> List[Path]:
    files: List[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_file() and path.suffix == ".kicad_pcb":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.kicad_pcb")))
    return sorted(set(files))


def build_pair_key(pcb_file: Path) -> Tuple[str, str] | None:
    stem = pcb_file.stem
    if stem.startswith("output_bga.subopt."):
        return ("negative", stem[len("output_bga.subopt."):])
    if stem.startswith("output_bga."):
        return ("positive", stem[len("output_bga."):])
    return None


def find_pcb_pairs(inputs: Sequence[str | Path]) -> List[Dict[str, Path | str]]:
    pairs: Dict[str, Dict[str, Path]] = {}
    for pcb in find_pcb_files(inputs):
        info = build_pair_key(pcb)
        if info is None:
            continue
        role, key = info
        pairs.setdefault(key, {})[role] = pcb

    result: List[Dict[str, Path | str]] = []
    for key in sorted(pairs):
        item = pairs[key]
        if "positive" in item and "negative" in item:
            result.append({"key": key, "positive": item["positive"], "negative": item["negative"]})
    return result


def _edges_array(edges: Sequence[GridEdge]) -> np.ndarray:
    if not edges:
        return np.empty((0, 2, 3), dtype=np.int64)
    return np.asarray(edges, dtype=np.int64)


def iter_pads(board: BoardData) -> Iterator[Pad]:
    for pads in board.pads_by_net.values():
        yield from pads


def _ceil_div(num: int, den: int) -> int:
    return (num + den - 1) // den


def _rotate_points(points: np.ndarray, angle_deg: float) -> np.ndarray:
    angle = math.radians(angle_deg)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    rot = np.array(((cos_a, -sin_a), (sin_a, cos_a)), dtype=np.float64)
    return points @ rot.T


def _transform_points(points: np.ndarray, center: Tuple[float, float], angle_deg: float) -> np.ndarray:
    transformed = _rotate_points(points, angle_deg)
    transformed[:, 0] += center[0]
    transformed[:, 1] += center[1]
    return transformed


def _polygon_mm_array_to_units(polygon: np.ndarray) -> IntPolygon:
    return tuple((mm_to_pcb_units(x_mm), mm_to_pcb_units(y_mm)) for x_mm, y_mm in polygon.tolist())


def _polygon_units_to_mm_array(polygon_units: IntPolygon) -> np.ndarray:
    return np.asarray(pcb_units_polygon_to_mm(polygon_units), dtype=np.float64)


def _normalize_bbox_units(bbox_units: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    min_x, min_y, max_x, max_y = bbox_units
    return (min(min_x, max_x), min(min_y, max_y), max(min_x, max_x), max(min_y, max_y))


def _bbox_mm_to_units(bbox_mm: Tuple[float, float, float, float]) -> Tuple[int, int, int, int]:
    return _normalize_bbox_units(tuple(mm_to_pcb_units(value) for value in bbox_mm))


def _bbox_units_to_mm(bbox_units: Tuple[int, int, int, int]) -> Tuple[float, float, float, float]:
    return tuple(pcb_units_to_mm(value) for value in bbox_units)


def _normalize_bbox_mm(bbox_mm: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = bbox_mm
    return (min(min_x, max_x), min(min_y, max_y), max(min_x, max_x), max(min_y, max_y))


def _expand_bbox_mm(
    bbox_mm: Tuple[float, float, float, float],
    padding_mm: float,
    *,
    clip_bbox_mm: Tuple[float, float, float, float] | None = None,
) -> Tuple[float, float, float, float]:
    min_x, min_y, max_x, max_y = _normalize_bbox_mm(bbox_mm)
    expanded = (min_x - padding_mm, min_y - padding_mm, max_x + padding_mm, max_y + padding_mm)
    if clip_bbox_mm is None:
        return expanded
    clip_min_x, clip_min_y, clip_max_x, clip_max_y = _normalize_bbox_mm(clip_bbox_mm)
    return (
        max(expanded[0], clip_min_x),
        max(expanded[1], clip_min_y),
        min(expanded[2], clip_max_x),
        min(expanded[3], clip_max_y),
    )


def _is_noninteractive_matplotlib_backend() -> bool:
    import matplotlib

    backend = matplotlib.get_backend().lower()
    return backend in {"agg", "pdf", "pgf", "ps", "svg", "template", "cairo"}


def _finalize_matplotlib_figure(
    fig,
    *,
    output_path: Path | None = None,
    show: bool = True,
    dpi: int = 200,
    trim_whitespace: bool = True,
    pad_inches: float = 0.02,
    png_width: int = 0,
) -> None:
    import matplotlib.pyplot as plt

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        save_kwargs = {"dpi": dpi}
        if trim_whitespace:
            save_kwargs["bbox_inches"] = "tight"
            save_kwargs["pad_inches"] = max(0.0, pad_inches)
        fig.savefig(output_path, **save_kwargs)
        if trim_whitespace or png_width > 0:
            _postprocess_saved_image(output_path, trim_whitespace=trim_whitespace, png_width=png_width)
    if show:
        plt.show()
    plt.close(fig)


def _load_font(size: int = 32):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _fit_into_box(img, max_w: int, max_h: int):
    if img.width <= 0 or img.height <= 0:
        return img
    ratio = min(max_w / img.width, max_h / img.height)
    ratio = min(ratio, 1.0)
    new_w = max(1, int(round(img.width * ratio)))
    new_h = max(1, int(round(img.height * ratio)))
    if new_w == img.width and new_h == img.height:
        return img
    return img.resize((new_w, new_h))


def _trim_image_whitespace(img, bg_threshold: int = 250, padding: int = 12, edge_inset: int = 8):
    rgb = img.convert("RGB")
    px = rgb.load()
    width, height = rgb.size
    min_x, min_y = width, height
    max_x, max_y = -1, -1

    scan_left = min(edge_inset, max(0, width - 1))
    scan_top = min(edge_inset, max(0, height - 1))
    scan_right = max(scan_left, width - edge_inset)
    scan_bottom = max(scan_top, height - edge_inset)

    for y in range(scan_top, scan_bottom):
        for x in range(scan_left, scan_right):
            r, g, b = px[x, y]
            if r < bg_threshold or g < bg_threshold or b < bg_threshold:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)

    if max_x < min_x or max_y < min_y:
        return img
    left = max(0, min_x - padding)
    top = max(0, min_y - padding)
    right = min(width, max_x + padding + 1)
    bottom = min(height, max_y + padding + 1)
    return img.crop((left, top, right, bottom))


def _resize_image_to_width(img, target_width: int):
    if target_width <= 0 or img.width <= 0 or img.width == target_width:
        return img
    Image, _ImageDraw, _ImageFont = _require_pillow()
    resample = getattr(Image, "Resampling", Image).LANCZOS
    target_height = max(1, int(round(img.height * (target_width / img.width))))
    return img.resize((target_width, target_height), resample)


def _postprocess_saved_image(output_path: Path, *, trim_whitespace: bool, png_width: int = 0) -> None:
    if not output_path.exists():
        return
    Image, _ImageDraw, _ImageFont = _require_pillow()
    img = Image.open(output_path).convert("RGB")
    try:
        processed = img
        if trim_whitespace:
            processed = _trim_image_whitespace(processed)
        if png_width > 0:
            resized = _resize_image_to_width(processed, png_width)
            if resized is not processed:
                if processed is not img:
                    processed.close()
                processed = resized
        processed.save(output_path)
        if processed is not img:
            processed.close()
    finally:
        img.close()


def _polygon_bbox_units(polygon_units: IntPolygon) -> Tuple[int, int, int, int]:
    xs = [point[0] for point in polygon_units]
    ys = [point[1] for point in polygon_units]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_overlap_units(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _point_in_bbox_units(point_units: IntPoint, bbox_units: Tuple[int, int, int, int]) -> bool:
    x_units, y_units = point_units
    min_x, min_y, max_x, max_y = bbox_units
    return min_x <= x_units <= max_x and min_y <= y_units <= max_y


def _point_in_polygon_units(point_units: IntPoint, polygon_units: IntPolygon) -> bool:
    x_units, y_units = point_units
    inside = False
    for index in range(len(polygon_units)):
        x1, y1 = polygon_units[index]
        x2, y2 = polygon_units[(index + 1) % len(polygon_units)]
        if (y1 > y_units) == (y2 > y_units):
            continue
        x_intersection = (x2 - x1) * (y_units - y1) / (y2 - y1) + x1
        if x_units < x_intersection:
            inside = not inside
    return inside


def _orientation(a: IntPoint, b: IntPoint, c: IntPoint) -> int:
    value = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
    if value == 0:
        return 0
    return 1 if value > 0 else 2


def _on_segment(a: IntPoint, b: IntPoint, c: IntPoint) -> bool:
    return min(a[0], c[0]) <= b[0] <= max(a[0], c[0]) and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])


def _segments_intersect(a1: IntPoint, a2: IntPoint, b1: IntPoint, b2: IntPoint) -> bool:
    o1 = _orientation(a1, a2, b1)
    o2 = _orientation(a1, a2, b2)
    o3 = _orientation(b1, b2, a1)
    o4 = _orientation(b1, b2, a2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(a1, b1, a2):
        return True
    if o2 == 0 and _on_segment(a1, b2, a2):
        return True
    if o3 == 0 and _on_segment(b1, a1, b2):
        return True
    if o4 == 0 and _on_segment(b1, a2, b2):
        return True
    return False


def _polygon_intersects_bbox_units(polygon_units: IntPolygon, bbox_units: Tuple[int, int, int, int]) -> bool:
    if len(polygon_units) < 3:
        return False
    if not _bbox_overlap_units(_polygon_bbox_units(polygon_units), bbox_units):
        return False

    if any(_point_in_bbox_units(point_units, bbox_units) for point_units in polygon_units):
        return True

    min_x, min_y, max_x, max_y = bbox_units
    bbox_corners = [
        (min_x, min_y),
        (max_x, min_y),
        (max_x, max_y),
        (min_x, max_y),
    ]
    if any(_point_in_polygon_units(corner, polygon_units) for corner in bbox_corners):
        return True

    bbox_edges = list(zip(bbox_corners, bbox_corners[1:] + bbox_corners[:1]))
    poly_edges = list(zip(polygon_units, polygon_units[1:] + polygon_units[:1]))
    for poly_a, poly_b in poly_edges:
        for box_a, box_b in bbox_edges:
            if _segments_intersect(poly_a, poly_b, box_a, box_b):
                return True
    return False


def circle_polygon(center: Tuple[float, float], radius_mm: float, num_points: int = 32) -> np.ndarray:
    if radius_mm <= 0:
        return np.empty((0, 2), dtype=np.float64)
    angles = np.linspace(0.0, 2.0 * math.pi, num_points, endpoint=False)
    points = np.column_stack((np.cos(angles), np.sin(angles))) * radius_mm
    points[:, 0] += center[0]
    points[:, 1] += center[1]
    return points


def _capsule_local_polygon(line_length_mm: float, radius_mm: float, arc_points: int = 12) -> np.ndarray:
    if radius_mm <= 0:
        return np.empty((0, 2), dtype=np.float64)
    if line_length_mm <= 1e-12:
        return circle_polygon((0.0, 0.0), radius_mm, num_points=max(arc_points * 2, 24))

    right_arc = np.linspace(-math.pi / 2.0, math.pi / 2.0, arc_points, endpoint=True)
    left_arc = np.linspace(math.pi / 2.0, 3.0 * math.pi / 2.0, arc_points, endpoint=True)
    right = np.column_stack(
        (
            line_length_mm + radius_mm * np.cos(right_arc),
            radius_mm * np.sin(right_arc),
        )
    )
    left = np.column_stack(
        (
            radius_mm * np.cos(left_arc),
            radius_mm * np.sin(left_arc),
        )
    )
    return np.vstack((right, left))


def segment_to_polygon(
    start_mm: Tuple[float, float],
    end_mm: Tuple[float, float],
    width_mm: float,
    arc_points: int = 12,
) -> np.ndarray:
    radius_mm = width_mm / 2.0
    if radius_mm <= 0:
        return np.empty((0, 2), dtype=np.float64)
    dx = end_mm[0] - start_mm[0]
    dy = end_mm[1] - start_mm[1]
    length_mm = math.hypot(dx, dy)
    if length_mm <= 1e-12:
        return circle_polygon(start_mm, radius_mm, num_points=max(arc_points * 2, 24))
    theta_deg = math.degrees(math.atan2(dy, dx))
    local = _capsule_local_polygon(length_mm, radius_mm, arc_points=arc_points)
    return _transform_points(local, start_mm, theta_deg)


def rectangle_polygon(
    center_mm: Tuple[float, float],
    size_mm: Tuple[float, float],
    rotation_deg: float = 0.0,
) -> np.ndarray:
    half_x = size_mm[0] / 2.0
    half_y = size_mm[1] / 2.0
    local = np.array(
        [
            (-half_x, -half_y),
            (half_x, -half_y),
            (half_x, half_y),
            (-half_x, half_y),
        ],
        dtype=np.float64,
    )
    return _transform_points(local, center_mm, rotation_deg)


def oval_polygon(
    center_mm: Tuple[float, float],
    size_mm: Tuple[float, float],
    rotation_deg: float = 0.0,
    arc_points: int = 12,
    scale: float = 1.0,
) -> np.ndarray:
    width_mm, height_mm = size_mm
    width_mm *= scale
    height_mm *= scale
    if width_mm <= 0 or height_mm <= 0:
        return np.empty((0, 2), dtype=np.float64)
    if abs(width_mm - height_mm) <= 1e-9:
        return circle_polygon(center_mm, max(width_mm, height_mm) / 2.0, num_points=max(arc_points * 2, 24))
    if width_mm >= height_mm:
        centerline_mm = width_mm - height_mm
        local = _capsule_local_polygon(centerline_mm, height_mm / 2.0, arc_points=arc_points)
        local[:, 0] -= centerline_mm / 2.0
        return _transform_points(local, center_mm, rotation_deg)
    centerline_mm = height_mm - width_mm
    local = _capsule_local_polygon(centerline_mm, width_mm / 2.0, arc_points=arc_points)
    local[:, 0] -= centerline_mm / 2.0
    return _transform_points(local, center_mm, rotation_deg + 90.0)


def pad_to_polygon(pad: Pad, scale: float = 1.0) -> np.ndarray:
    center = (pad.x_mm, pad.y_mm)
    shape = pad.shape.lower()
    if shape == "circle":
        return circle_polygon(center, (max(pad.size_mm) * scale) / 2.0)
    if shape == "oval":
        return oval_polygon(center, pad.size_mm, rotation_deg=pad.rotation_deg, scale=scale)
    if shape in {"rect", "roundrect", "trapezoid", "custom"}:
        return rectangle_polygon(center, pad.size_mm, rotation_deg=pad.rotation_deg)
    return rectangle_polygon(center, pad.size_mm, rotation_deg=pad.rotation_deg)


def via_to_polygon(via: Via, diameter_mm: float, scale: float = 1.0) -> np.ndarray:
    return circle_polygon(via.at, (diameter_mm * scale) / 2.0)


def _polygon_bbox_mm(polygon: np.ndarray) -> Tuple[float, float, float, float]:
    return (
        float(np.min(polygon[:, 0])),
        float(np.min(polygon[:, 1])),
        float(np.max(polygon[:, 0])),
        float(np.max(polygon[:, 1])),
    )


def _grid_window_from_bbox(
    bbox_mm: Tuple[float, float, float, float],
    origin_mm: Tuple[float, float],
    resolution_mm: float,
    height: int,
    width: int,
) -> Tuple[int, int, int, int]:
    min_x, min_y, max_x, max_y = bbox_mm
    col_start = max(int(math.floor((min_x - origin_mm[0]) / resolution_mm)) - 1, 0)
    row_start = max(int(math.floor((min_y - origin_mm[1]) / resolution_mm)) - 1, 0)
    col_stop = min(int(math.ceil((max_x - origin_mm[0]) / resolution_mm)) + 1, width)
    row_stop = min(int(math.ceil((max_y - origin_mm[1]) / resolution_mm)) + 1, height)
    return row_start, row_stop, col_start, col_stop


def _grid_center_coordinates(
    row_start: int,
    row_stop: int,
    col_start: int,
    col_stop: int,
    origin_mm: Tuple[float, float],
    resolution_mm: float,
) -> Tuple[np.ndarray, np.ndarray]:
    xs = origin_mm[0] + (np.arange(col_start, col_stop, dtype=np.float64) + 0.5) * resolution_mm
    ys = origin_mm[1] + (np.arange(row_start, row_stop, dtype=np.float64) + 0.5) * resolution_mm
    return np.meshgrid(xs, ys)


def rasterize_polygon(
    polygon: np.ndarray,
    grid: np.ndarray,
    layer_index: int,
    origin_mm: Tuple[float, float],
    resolution_mm: float,
) -> None:
    if polygon.size == 0:
        return
    height, width, _num_layers = grid.shape
    bbox_mm = _polygon_bbox_mm(polygon)
    row_start, row_stop, col_start, col_stop = _grid_window_from_bbox(
        bbox_mm,
        origin_mm,
        resolution_mm,
        height,
        width,
    )
    if row_start >= row_stop or col_start >= col_stop:
        return

    xx, yy = _grid_center_coordinates(
        row_start,
        row_stop,
        col_start,
        col_stop,
        origin_mm,
        resolution_mm,
    )
    sample_points = np.column_stack((xx.ravel(), yy.ravel()))
    path = MplPath(polygon, closed=True)
    mask = path.contains_points(sample_points, radius=1e-12).reshape(xx.shape)
    if np.any(mask):
        grid[row_start:row_stop, col_start:col_stop, layer_index] |= mask.astype(np.uint8)


def _layer_indices(layer_names: Iterable[str], layer_map: Dict[str, int]) -> List[int]:
    result: List[int] = []
    for layer_name in layer_names:
        layer_index = layer_map.get(layer_name)
        if layer_index is None or layer_index in result:
            continue
        result.append(layer_index)
    return result


def units_to_grid_index(
    point_units: IntPoint,
    origin_units: IntPoint,
    resolution_units: int,
    height: int,
    width: int,
) -> Tuple[int, int]:
    gx = (point_units[0] - origin_units[0]) // resolution_units
    gy = (point_units[1] - origin_units[1]) // resolution_units
    gx = min(max(gx, 0), width - 1)
    gy = min(max(gy, 0), height - 1)
    return gx, gy


def rasterize_segment(
    grid: np.ndarray,
    segment: Segment,
    layer_map: Dict[str, int],
    origin_mm: Tuple[float, float],
    resolution_mm: float,
    rules: BoardRules | None = None,
) -> None:
    layer_index = layer_map.get(segment.layer)
    if layer_index is None:
        return
    width_mm = segment.width_mm
    if width_mm <= 0 and rules is not None:
        width_mm = max(rules.trace_width_mm, rules.trace_min_mm)
    polygon = segment_to_polygon(segment.start, segment.end, width_mm)
    rasterize_polygon(polygon, grid, layer_index, origin_mm, resolution_mm)


def rasterize_via(
    grid: np.ndarray,
    via: Via,
    layer_map: Dict[str, int],
    origin_mm: Tuple[float, float],
    origin_units: IntPoint,
    resolution_mm: float,
    resolution_units: int,
    rules: BoardRules | None = None,
) -> List[GridEdge]:
    diameter_mm = via.size_mm
    if diameter_mm <= 0 and rules is not None:
        diameter_mm = rules.via_size_mm
    if diameter_mm <= 0:
        return []
    polygon = via_to_polygon(via, diameter_mm)
    for layer_index in _layer_indices(via.layers, layer_map):
        rasterize_polygon(polygon, grid, layer_index, origin_mm, resolution_mm)

    height, width, _ = grid.shape
    gx, gy = units_to_grid_index(via.at_units, origin_units, resolution_units, height, width)
    layer_indices = _layer_indices(via.layers, layer_map)
    return [((gx, gy, a), (gx, gy, b)) for a, b in zip(layer_indices, layer_indices[1:])]


def rasterize_pad(
    grid: np.ndarray,
    pad: Pad,
    layer_map: Dict[str, int],
    origin_mm: Tuple[float, float],
    resolution_mm: float,
) -> None:
    polygon = pad_to_polygon(pad)
    pad_layers = pad.copper_layers or ((pad.layer,) if pad.layer else ())
    for layer_index in _layer_indices(pad_layers, layer_map):
        rasterize_polygon(polygon, grid, layer_index, origin_mm, resolution_mm)


def rasterize_zone(
    grid: np.ndarray,
    zone: Zone,
    layer_map: Dict[str, int],
    origin_mm: Tuple[float, float],
    resolution_mm: float,
) -> None:
    layer_indices = _layer_indices(zone.layers, layer_map)
    for polygon_points in zone.polygons:
        polygon = np.asarray(polygon_points, dtype=np.float64)
        if polygon.ndim != 2 or polygon.shape[0] < 3:
            continue
        for layer_index in layer_indices:
            rasterize_polygon(polygon, grid, layer_index, origin_mm, resolution_mm)


def vectorize_board_geometry(
    board: BoardData,
    include_zones: bool = True,
    *,
    visual_pad_scale: float = 1.0,
    visual_via_scale: float = 1.0,
) -> VectorVisualizationResult:
    layer_polygons_units: Dict[str, List[IntPolygon]] = {layer_name: [] for layer_name in board.copper_layers}

    for segment in board.segments:
        polygon_mm = segment_to_polygon(
            segment.start,
            segment.end,
            segment.width_mm if segment.width_mm > 0 else max(board.rules.trace_width_mm, board.rules.trace_min_mm),
        )
        if polygon_mm.size and segment.layer in layer_polygons_units:
            layer_polygons_units[segment.layer].append(_polygon_mm_array_to_units(polygon_mm))

    for via in board.vias:
        diameter_mm = via.size_mm if via.size_mm > 0 else board.rules.via_size_mm
        polygon_mm = via_to_polygon(via, diameter_mm, scale=visual_via_scale)
        if not polygon_mm.size:
            continue
        polygon_units = _polygon_mm_array_to_units(polygon_mm)
        for layer_name in via.layers:
            if layer_name in layer_polygons_units:
                layer_polygons_units[layer_name].append(polygon_units)

    for pad in iter_pads(board):
        polygon_mm = pad_to_polygon(pad, scale=visual_pad_scale)
        if not polygon_mm.size:
            continue
        polygon_units = _polygon_mm_array_to_units(polygon_mm)
        for layer_name in (pad.copper_layers or ((pad.layer,) if pad.layer else ())):
            if layer_name in layer_polygons_units:
                layer_polygons_units[layer_name].append(polygon_units)

    if include_zones:
        for zone in board.zones:
            for polygon_units in zone.polygons_units:
                if len(polygon_units) < 3:
                    continue
                for layer_name in zone.layers:
                    if layer_name in layer_polygons_units:
                        layer_polygons_units[layer_name].append(polygon_units)

    return VectorVisualizationResult(
        layer_names=list(board.copper_layers),
        layer_polygons_units=layer_polygons_units,
        board_bbox_mm=board.bbox_mm,
        board_bbox_units=board.bbox_units,
        units_per_mm=board.units_per_mm,
    )


def extract_nets_in_bbox(
    board: BoardData,
    bbox_mm: Tuple[float, float, float, float] | None = None,
    *,
    bbox_units: Tuple[int, int, int, int] | None = None,
    layers: Sequence[str] | None = None,
    include_zones: bool = True,
) -> BBoxNetExtractionResult:
    if bbox_mm is None and bbox_units is None:
        raise ValueError("bbox_mm 和 bbox_units 至少需要提供一个")
    if bbox_units is None:
        bbox_units = _bbox_mm_to_units(bbox_mm)  # type: ignore[arg-type]
    else:
        bbox_units = _normalize_bbox_units(bbox_units)
    if bbox_mm is None:
        bbox_mm = _bbox_units_to_mm(bbox_units)

    active_layers = [layer for layer in (layers or board.copper_layers) if layer in board.copper_layers]
    active_layer_set = set(active_layers)
    hits_by_net: Dict[int, Dict[str, object]] = {}

    def register_hit(net_id: int, layer_names: Iterable[str], primitive_kind: str) -> None:
        if net_id <= 0:
            return
        filtered_layers = sorted({layer for layer in layer_names if layer in active_layer_set})
        if not filtered_layers:
            return
        entry = hits_by_net.setdefault(
            net_id,
            {
                "net_name": board.nets.get(net_id, ""),
                "layers": set(),
                "segment_count": 0,
                "via_count": 0,
                "pad_count": 0,
                "zone_count": 0,
            },
        )
        entry["layers"].update(filtered_layers)  # type: ignore[union-attr]
        entry[f"{primitive_kind}_count"] += 1  # type: ignore[index]

    for segment in board.segments:
        if segment.layer not in active_layer_set:
            continue
        width_mm = segment.width_mm if segment.width_mm > 0 else max(board.rules.trace_width_mm, board.rules.trace_min_mm)
        polygon_units = _polygon_mm_array_to_units(segment_to_polygon(segment.start, segment.end, width_mm))
        if _polygon_intersects_bbox_units(polygon_units, bbox_units):
            register_hit(segment.net_id, (segment.layer,), "segment")

    for via in board.vias:
        via_layers = [layer for layer in via.layers if layer in active_layer_set]
        if not via_layers:
            continue
        diameter_mm = via.size_mm if via.size_mm > 0 else board.rules.via_size_mm
        polygon_units = _polygon_mm_array_to_units(via_to_polygon(via, diameter_mm))
        if _polygon_intersects_bbox_units(polygon_units, bbox_units):
            register_hit(via.net_id, via_layers, "via")

    for pad in iter_pads(board):
        pad_layers = [layer for layer in (pad.copper_layers or ((pad.layer,) if pad.layer else ())) if layer in active_layer_set]
        if not pad_layers:
            continue
        polygon_units = _polygon_mm_array_to_units(pad_to_polygon(pad))
        if _polygon_intersects_bbox_units(polygon_units, bbox_units):
            register_hit(pad.net_id, pad_layers, "pad")

    if include_zones:
        for zone in board.zones:
            zone_layers = [layer for layer in zone.layers if layer in active_layer_set]
            if not zone_layers:
                continue
            for polygon_units in zone.polygons_units:
                if _polygon_intersects_bbox_units(polygon_units, bbox_units):
                    register_hit(zone.net_id, zone_layers, "zone")

    hits = [
        BBoxNetHit(
            net_id=net_id,
            net_name=entry["net_name"],  # type: ignore[index]
            layers=sorted(entry["layers"]),  # type: ignore[arg-type]
            segment_count=entry["segment_count"],  # type: ignore[index]
            via_count=entry["via_count"],  # type: ignore[index]
            pad_count=entry["pad_count"],  # type: ignore[index]
            zone_count=entry["zone_count"],  # type: ignore[index]
        )
        for net_id, entry in sorted(hits_by_net.items())
    ]

    return BBoxNetExtractionResult(
        bbox_mm=bbox_mm,
        bbox_units=bbox_units,
        layers=active_layers,
        hits=hits,
    )


def build_grid(
    board: BoardData,
    resolution_mm: float = 0.1,
    include_zones: bool = True,
) -> VoxelizationResult:
    if resolution_mm <= 0:
        raise ValueError("resolution_mm 必须大于 0")
    if not board.copper_layers:
        raise ValueError("板子中没有可用的铜层")

    resolution_units = mm_to_pcb_units(resolution_mm)
    if resolution_units <= 0:
        raise ValueError("resolution_mm 太小，换算到内部整数单位后为 0")

    min_x_units, min_y_units, max_x_units, max_y_units = board.bbox_units
    width = max(1, _ceil_div(max_x_units - min_x_units, resolution_units))
    height = max(1, _ceil_div(max_y_units - min_y_units, resolution_units))
    layer_names = list(board.copper_layers)
    layer_map = {layer_name: index for index, layer_name in enumerate(layer_names)}
    grid = np.zeros((height, width, len(layer_names)), dtype=np.uint8)
    edges: List[GridEdge] = []
    origin_units = (min_x_units, min_y_units)
    origin_mm = pcb_units_point_to_mm(origin_units)
    resolution_mm_exact = pcb_units_to_mm(resolution_units)

    for segment in board.segments:
        rasterize_segment(grid, segment, layer_map, origin_mm, resolution_mm_exact, rules=board.rules)
    for via in board.vias:
        edges.extend(
            rasterize_via(
                grid,
                via,
                layer_map,
                origin_mm,
                origin_units,
                resolution_mm_exact,
                resolution_units,
                rules=board.rules,
            )
        )
    for pad in iter_pads(board):
        rasterize_pad(grid, pad, layer_map, origin_mm, resolution_mm_exact)
    if include_zones:
        for zone in board.zones:
            rasterize_zone(grid, zone, layer_map, origin_mm, resolution_mm_exact)

    return VoxelizationResult(
        grid=grid,
        layer_map=layer_map,
        layer_names=layer_names,
        origin_mm=origin_mm,
        origin_units=origin_units,
        resolution_mm=resolution_mm_exact,
        resolution_units=resolution_units,
        board_bbox_mm=board.bbox_mm,
        board_bbox_units=board.bbox_units,
        units_per_mm=board.units_per_mm,
        edges=edges,
    )


def _build_board_subset_for_net_ids(board: BoardData, net_ids: Sequence[int]) -> BoardData:
    target_net_ids = {int(net_id) for net_id in net_ids}
    return BoardData(
        path=board.path,
        nets={net_id: net_name for net_id, net_name in board.nets.items() if net_id in target_net_ids},
        layers=dict(board.layers),
        copper_layers=list(board.copper_layers),
        layer_order=dict(board.layer_order),
        copper_layer_map=dict(board.copper_layer_map),
        width_mm=board.width_mm,
        height_mm=board.height_mm,
        bbox_mm=board.bbox_mm,
        rules=board.rules,
        segments=[segment for segment in board.segments if segment.net_id in target_net_ids],
        vias=[via for via in board.vias if via.net_id in target_net_ids],
        pads_by_net={net_id: list(board.pads_by_net.get(net_id, [])) for net_id in sorted(target_net_ids)},
        zones=[zone for zone in board.zones if zone.net_id in target_net_ids],
        units_per_mm=board.units_per_mm,
        width_units=board.width_units,
        height_units=board.height_units,
        bbox_units=board.bbox_units,
    )


def build_highlight_grid(
    board: BoardData,
    net_ids: Sequence[int],
    *,
    resolution_mm: float,
    include_zones: bool = True,
) -> VoxelizationResult:
    subset = _build_board_subset_for_net_ids(board, net_ids)
    return build_grid(subset, resolution_mm=resolution_mm, include_zones=include_zones)


def parse_missing_net_ids_from_json(path: str | Path) -> List[int]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [int(row["net_id"]) for row in raw if "net_id" in row]
    if isinstance(raw, dict):
        text = str(raw.get("设计信息", ""))
        return [int(match.group("net_id")) for match in MISSING_NET_ID_RE.finditer(text)]
    raise ValueError(f"Unsupported missing json format: {path}")


def voxelize_kicad_pcb(
    pcb_file: str | Path,
    resolution_mm: float = 0.1,
    include_zones: bool = True,
) -> VoxelizationResult:
    board = parse_kicad_pcb(pcb_file)
    return build_grid(board, resolution_mm=resolution_mm, include_zones=include_zones)


def make_synthetic_example_board() -> BoardData:
    copper_layers = ["F.Cu", "B.Cu"]
    bbox_units = (0, 0, mm_to_pcb_units(10.0), mm_to_pcb_units(10.0))
    return BoardData(
        path=Path("<synthetic>"),
        nets={1: "N1"},
        layers={0: "F.Cu", 31: "B.Cu"},
        copper_layers=copper_layers,
        layer_order={"F.Cu": 0, "B.Cu": 31},
        copper_layer_map={"F.Cu": 0, "B.Cu": 1},
        width_mm=10.0,
        height_mm=10.0,
        bbox_mm=(0.0, 0.0, 10.0, 10.0),
        rules=BoardRules(
            trace_width_mm=0.6,
            trace_min_mm=0.6,
            via_size_mm=1.0,
            via_drill_mm=0.4,
            trace_width_units=mm_to_pcb_units(0.6),
            trace_min_units=mm_to_pcb_units(0.6),
            via_size_units=mm_to_pcb_units(1.0),
            via_drill_units=mm_to_pcb_units(0.4),
        ),
        segments=[
            Segment(
                start=(2.0, 5.0),
                end=(5.0, 5.0),
                width_mm=0.6,
                layer="F.Cu",
                net_id=1,
                start_units=(mm_to_pcb_units(2.0), mm_to_pcb_units(5.0)),
                end_units=(mm_to_pcb_units(5.0), mm_to_pcb_units(5.0)),
                width_units=mm_to_pcb_units(0.6),
            ),
        ],
        vias=[
            Via(
                at=(5.0, 5.0),
                size_mm=1.0,
                drill_mm=0.4,
                layers=("F.Cu", "B.Cu"),
                net_id=1,
                at_units=(mm_to_pcb_units(5.0), mm_to_pcb_units(5.0)),
                size_units=mm_to_pcb_units(1.0),
                drill_units=mm_to_pcb_units(0.4),
            ),
        ],
        pads_by_net={},
        zones=[],
        units_per_mm=PCB_UNITS_PER_MM,
        width_units=mm_to_pcb_units(10.0),
        height_units=mm_to_pcb_units(10.0),
        bbox_units=bbox_units,
    )


def visualize_grid(
    result: VoxelizationResult,
    title: str = "Copper Occupancy",
    *,
    highlight_result: VoxelizationResult | None = None,
    bbox_mm: Tuple[float, float, float, float] | None = None,
    zoom_to_bbox: bool = False,
    bbox_padding_mm: float = 1.0,
    output_path: Path | None = None,
    show: bool = True,
    dpi: int = 200,
    trim_whitespace: bool = True,
    pad_inches: float = 0.02,
    clean_image: bool = False,
    visible_layers: Sequence[str] | None = None,
    png_width: int = 0,
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    display_layers = [layer for layer in (visible_layers or result.layer_names) if layer in result.layer_names]
    if not display_layers:
        display_layers = list(result.layer_names)

    num_layers = len(display_layers)
    layer_colors = _layer_color_map(result.layer_names)
    fig, axes = plt.subplots(1, num_layers, figsize=(4 * num_layers, 4), squeeze=False)
    extent = (
        result.origin_mm[0],
        result.origin_mm[0] + result.grid.shape[1] * result.resolution_mm,
        result.origin_mm[1] + result.grid.shape[0] * result.resolution_mm,
        result.origin_mm[1],
    )
    overlay_bbox_mm = _normalize_bbox_mm(bbox_mm) if bbox_mm is not None else None
    view_bbox_mm = (
        _expand_bbox_mm(overlay_bbox_mm, bbox_padding_mm, clip_bbox_mm=result.board_bbox_mm)
        if zoom_to_bbox and overlay_bbox_mm is not None
        else result.board_bbox_mm
    )
    for display_index, layer_name in enumerate(display_layers):
        ax = axes[0, display_index]
        layer_index = result.layer_map[layer_name]
        ax.imshow(
            result.grid[:, :, layer_index],
            cmap=ListedColormap(["#ffffff", layer_colors.get(layer_name, "#2f2f2f")]),
            interpolation="nearest",
            extent=extent,
            vmin=0,
            vmax=1,
        )
        if highlight_result is not None:
            highlight_layer = highlight_result.grid[:, :, layer_index]
            if np.any(highlight_layer):
                masked = np.ma.masked_where(highlight_layer == 0, highlight_layer)
                ax.imshow(
                    masked,
                    cmap="autumn",
                    interpolation="nearest",
                    extent=extent,
                    alpha=0.78,
                    vmin=0,
                    vmax=1,
                )
        if overlay_bbox_mm is not None:
            min_x, min_y, max_x, max_y = overlay_bbox_mm
            ax.add_patch(
                MplRectanglePatch(
                    (min_x, min_y),
                    max_x - min_x,
                    max_y - min_y,
                    fill=False,
                    linewidth=1.6,
                    linestyle="--",
                    edgecolor="#c83f2f",
                )
            )
        min_x, min_y, max_x, max_y = view_bbox_mm
        ax.set_xlim(min_x, max_x)
        ax.set_ylim(max_y, min_y)
        if clean_image:
            ax.set_axis_off()
        else:
            ax.set_title(layer_name)
            ax.set_xlabel("x (mm)")
            ax.set_ylabel("y (mm)")
    if clean_image:
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1, wspace=0.01, hspace=0.0)
    else:
        fig.suptitle(title)
        fig.tight_layout()
    _finalize_matplotlib_figure(
        fig,
        output_path=output_path,
        show=show,
        dpi=dpi,
        trim_whitespace=trim_whitespace,
        pad_inches=pad_inches,
        png_width=png_width,
    )


def visualize_geometry(
    geometry: VectorVisualizationResult,
    title: str = "Copper Geometry",
    *,
    highlight_geometry: VectorVisualizationResult | None = None,
    bbox_mm: Tuple[float, float, float, float] | None = None,
    zoom_to_bbox: bool = False,
    bbox_padding_mm: float = 1.0,
    visible_layers: Sequence[str] | None = None,
    output_path: Path | None = None,
    show: bool = True,
    dpi: int = 200,
    trim_whitespace: bool = True,
    pad_inches: float = 0.02,
    clean_image: bool = False,
    png_width: int = 0,
) -> None:
    import matplotlib.pyplot as plt

    display_layers = [layer for layer in (visible_layers or geometry.layer_names) if layer in geometry.layer_names]
    if not display_layers:
        display_layers = list(geometry.layer_names)

    num_layers = max(1, len(display_layers))
    layer_colors = _layer_color_map(geometry.layer_names)
    fig, axes = plt.subplots(1, num_layers, figsize=(4 * num_layers, 4), squeeze=False)
    overlay_bbox_mm = _normalize_bbox_mm(bbox_mm) if bbox_mm is not None else None
    overlay_bbox_units = _bbox_mm_to_units(overlay_bbox_mm) if overlay_bbox_mm is not None else None
    view_bbox_mm = (
        _expand_bbox_mm(overlay_bbox_mm, bbox_padding_mm, clip_bbox_mm=geometry.board_bbox_mm)
        if zoom_to_bbox and overlay_bbox_mm is not None
        else geometry.board_bbox_mm
    )

    for index, layer_name in enumerate(display_layers):
        ax = axes[0, index]
        highlight_patches_all: List[MplPolygonPatch] = []
        if highlight_geometry is not None:
            for polygon_units in highlight_geometry.layer_polygons_units.get(layer_name, []):
                highlight_patches_all.append(MplPolygonPatch(_polygon_units_to_mm_array(polygon_units), closed=True))
        if overlay_bbox_units is None:
            patches = [
                MplPolygonPatch(_polygon_units_to_mm_array(polygon_units), closed=True)
                for polygon_units in geometry.layer_polygons_units.get(layer_name, [])
            ]
            if patches:
                collection = PatchCollection(
                    patches,
                    facecolor=layer_colors.get(layer_name, "#2f2f2f"),
                    edgecolor=layer_colors.get(layer_name, "#2f2f2f"),
                    linewidths=0.2,
                )
                ax.add_collection(collection)
            if highlight_patches_all:
                ax.add_collection(
                    PatchCollection(
                        highlight_patches_all,
                        facecolor="#1f6feb",
                        edgecolor="#1f6feb",
                        linewidths=0.25,
                        alpha=0.9,
                    )
                )
        else:
            background_patches: List[MplPolygonPatch] = []
            highlight_patches: List[MplPolygonPatch] = []
            for polygon_units in geometry.layer_polygons_units.get(layer_name, []):
                patch = MplPolygonPatch(_polygon_units_to_mm_array(polygon_units), closed=True)
                if _polygon_intersects_bbox_units(polygon_units, overlay_bbox_units):
                    highlight_patches.append(patch)
                else:
                    background_patches.append(patch)
            if background_patches:
                ax.add_collection(
                    PatchCollection(
                        background_patches,
                        facecolor="#d7d1c3",
                        edgecolor="#d7d1c3",
                        linewidths=0.1,
                        alpha=0.45,
                    )
                )
            if highlight_patches:
                ax.add_collection(
                    PatchCollection(
                        highlight_patches,
                        facecolor=layer_colors.get(layer_name, "#2f2f2f"),
                        edgecolor=layer_colors.get(layer_name, "#2f2f2f"),
                        linewidths=0.25,
                    )
                )
            if highlight_patches_all:
                ax.add_collection(
                    PatchCollection(
                        highlight_patches_all,
                        facecolor="#1f6feb",
                        edgecolor="#1f6feb",
                        linewidths=0.25,
                        alpha=0.9,
                    )
                )
            min_x, min_y, max_x, max_y = overlay_bbox_mm
            ax.add_patch(
                MplRectanglePatch(
                    (min_x, min_y),
                    max_x - min_x,
                    max_y - min_y,
                    fill=False,
                    linewidth=1.6,
                    linestyle="--",
                    edgecolor="#c83f2f",
                )
            )
        min_x, min_y, max_x, max_y = view_bbox_mm
        ax.set_xlim(min_x, max_x)
        ax.set_ylim(max_y, min_y)
        ax.set_aspect("equal", adjustable="box")
        ax.set_facecolor("#f7f5ef")
        if clean_image:
            ax.set_axis_off()
        else:
            ax.set_title(layer_name)
            ax.set_xlabel("x (mm)")
            ax.set_ylabel("y (mm)")
    if clean_image:
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1, wspace=0.01, hspace=0.0)
    else:
        fig.suptitle(title)
        fig.tight_layout()
    _finalize_matplotlib_figure(
        fig,
        output_path=output_path,
        show=show,
        dpi=dpi,
        trim_whitespace=trim_whitespace,
        pad_inches=pad_inches,
        png_width=png_width,
    )


def visualize_geometry_overview(
    geometry: VectorVisualizationResult,
    title: str = "Copper Geometry Overview",
    *,
    highlight_geometry: VectorVisualizationResult | None = None,
    bbox_mm: Tuple[float, float, float, float] | None = None,
    zoom_to_bbox: bool = False,
    bbox_padding_mm: float = 1.0,
    visible_layers: Sequence[str] | None = None,
    output_path: Path | None = None,
    show: bool = True,
    dpi: int = 200,
    trim_whitespace: bool = True,
    pad_inches: float = 0.02,
    clean_image: bool = False,
    png_width: int = 0,
) -> None:
    import matplotlib.pyplot as plt

    display_layers = [layer for layer in (visible_layers or geometry.layer_names) if layer in geometry.layer_names]
    if not display_layers:
        display_layers = list(geometry.layer_names)

    fig, ax = plt.subplots(1, 1, figsize=(6, 6))
    layer_colors = _layer_color_map(geometry.layer_names)
    overlay_bbox_mm = _normalize_bbox_mm(bbox_mm) if bbox_mm is not None else None
    overlay_bbox_units = _bbox_mm_to_units(overlay_bbox_mm) if overlay_bbox_mm is not None else None
    view_bbox_mm = (
        _expand_bbox_mm(overlay_bbox_mm, bbox_padding_mm, clip_bbox_mm=geometry.board_bbox_mm)
        if zoom_to_bbox and overlay_bbox_mm is not None
        else geometry.board_bbox_mm
    )

    all_patches_by_layer: Dict[str, List[MplPolygonPatch]] = {layer_name: [] for layer_name in display_layers}
    background_patches: List[MplPolygonPatch] = []
    highlight_patches_by_layer: Dict[str, List[MplPolygonPatch]] = {layer_name: [] for layer_name in display_layers}
    highlight_patches_all: List[MplPolygonPatch] = []

    for layer_name in display_layers:
        if highlight_geometry is not None:
            for polygon_units in highlight_geometry.layer_polygons_units.get(layer_name, []):
                highlight_patches_all.append(MplPolygonPatch(_polygon_units_to_mm_array(polygon_units), closed=True))
        for polygon_units in geometry.layer_polygons_units.get(layer_name, []):
            patch = MplPolygonPatch(_polygon_units_to_mm_array(polygon_units), closed=True)
            if overlay_bbox_units is None:
                all_patches_by_layer[layer_name].append(patch)
            elif _polygon_intersects_bbox_units(polygon_units, overlay_bbox_units):
                highlight_patches_by_layer[layer_name].append(patch)
            else:
                background_patches.append(patch)

    if overlay_bbox_units is None:
        for layer_name in display_layers:
            layer_patches = all_patches_by_layer[layer_name]
            if layer_patches:
                ax.add_collection(
                    PatchCollection(
                        layer_patches,
                        facecolor=layer_colors.get(layer_name, "#2f2f2f"),
                        edgecolor=layer_colors.get(layer_name, "#2f2f2f"),
                        linewidths=0.2,
                        alpha=0.9,
                    )
                )
    else:
        if background_patches:
            ax.add_collection(
                PatchCollection(
                    background_patches,
                    facecolor="#d7d1c3",
                    edgecolor="#d7d1c3",
                    linewidths=0.1,
                    alpha=0.45,
                )
            )
        for layer_name in display_layers:
            layer_patches = highlight_patches_by_layer[layer_name]
            if layer_patches:
                ax.add_collection(
                    PatchCollection(
                        layer_patches,
                        facecolor=layer_colors.get(layer_name, "#2f2f2f"),
                        edgecolor=layer_colors.get(layer_name, "#2f2f2f"),
                        linewidths=0.25,
                        alpha=0.9,
                    )
                )
        min_x, min_y, max_x, max_y = overlay_bbox_mm
        ax.add_patch(
            MplRectanglePatch(
                (min_x, min_y),
                max_x - min_x,
                max_y - min_y,
                fill=False,
                linewidth=1.6,
                linestyle="--",
                edgecolor="#c83f2f",
            )
        )
    if highlight_patches_all:
        ax.add_collection(
            PatchCollection(
                highlight_patches_all,
                facecolor="#1f6feb",
                edgecolor="#1f6feb",
                linewidths=0.25,
                alpha=0.9,
            )
        )

    min_x, min_y, max_x, max_y = view_bbox_mm
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(max_y, min_y)
    ax.set_aspect("equal", adjustable="box")
    ax.set_facecolor("#f7f5ef")
    if clean_image:
        ax.set_axis_off()
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    else:
        fig.suptitle(title)
        ax.set_xlabel("x (mm)")
        ax.set_ylabel("y (mm)")
        fig.tight_layout()
    _finalize_matplotlib_figure(
        fig,
        output_path=output_path,
        show=show,
        dpi=dpi,
        trim_whitespace=trim_whitespace,
        pad_inches=pad_inches,
        png_width=png_width,
    )


def visualize_grid_overview(
    result: VoxelizationResult,
    title: str = "Copper Occupancy Overview",
    *,
    highlight_result: VoxelizationResult | None = None,
    bbox_mm: Tuple[float, float, float, float] | None = None,
    zoom_to_bbox: bool = False,
    bbox_padding_mm: float = 1.0,
    visible_layers: Sequence[str] | None = None,
    output_path: Path | None = None,
    show: bool = True,
    dpi: int = 200,
    trim_whitespace: bool = True,
    pad_inches: float = 0.02,
    clean_image: bool = False,
    png_width: int = 0,
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap

    display_layers = [layer for layer in (visible_layers or result.layer_names) if layer in result.layer_names]
    if not display_layers:
        display_layers = list(result.layer_names)

    layer_indices = [result.layer_map[layer_name] for layer_name in display_layers]
    layer_colors = _layer_color_map(result.layer_names)
    fig, ax = plt.subplots(1, 1, figsize=(6, 6))
    extent = (
        result.origin_mm[0],
        result.origin_mm[0] + result.grid.shape[1] * result.resolution_mm,
        result.origin_mm[1] + result.grid.shape[0] * result.resolution_mm,
        result.origin_mm[1],
    )
    for layer_name, layer_index in zip(display_layers, layer_indices):
        masked = np.ma.masked_where(result.grid[:, :, layer_index] == 0, result.grid[:, :, layer_index])
        ax.imshow(
            masked,
            cmap=ListedColormap(["#ffffff", layer_colors.get(layer_name, "#2f2f2f")]),
            interpolation="nearest",
            extent=extent,
            alpha=0.9,
            vmin=0,
            vmax=1,
        )
    if highlight_result is not None:
        highlight_combined = np.max(highlight_result.grid[:, :, layer_indices], axis=2)
        if np.any(highlight_combined):
            masked = np.ma.masked_where(highlight_combined == 0, highlight_combined)
            ax.imshow(masked, cmap="autumn", interpolation="nearest", extent=extent, alpha=0.78, vmin=0, vmax=1)
    overlay_bbox_mm = _normalize_bbox_mm(bbox_mm) if bbox_mm is not None else None
    view_bbox_mm = (
        _expand_bbox_mm(overlay_bbox_mm, bbox_padding_mm, clip_bbox_mm=result.board_bbox_mm)
        if zoom_to_bbox and overlay_bbox_mm is not None
        else result.board_bbox_mm
    )
    if overlay_bbox_mm is not None:
        min_x, min_y, max_x, max_y = overlay_bbox_mm
        ax.add_patch(
            MplRectanglePatch(
                (min_x, min_y),
                max_x - min_x,
                max_y - min_y,
                fill=False,
                linewidth=1.6,
                linestyle="--",
                edgecolor="#c83f2f",
            )
        )
    min_x, min_y, max_x, max_y = view_bbox_mm
    ax.set_xlim(min_x, max_x)
    ax.set_ylim(max_y, min_y)
    if clean_image:
        ax.set_axis_off()
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    else:
        fig.suptitle(title)
        ax.set_xlabel("x (mm)")
        ax.set_ylabel("y (mm)")
        fig.tight_layout()
    _finalize_matplotlib_figure(
        fig,
        output_path=output_path,
        show=show,
        dpi=dpi,
        trim_whitespace=trim_whitespace,
        pad_inches=pad_inches,
        png_width=png_width,
    )


def compose_pair_grid(
    positive_paths: Sequence[Path],
    negative_paths: Sequence[Path],
    layer_labels: Sequence[str],
    out_file: Path,
    *,
    row1_label: str = "Positive",
    row2_label: str = "Negative",
    max_cell_width: int = 1200,
    max_cell_height: int = 1200,
) -> None:
    from PIL import Image, ImageDraw

    if not positive_paths or not negative_paths or len(positive_paths) != len(negative_paths):
        return

    pos_imgs = [_trim_image_whitespace(Image.open(path).convert("RGB")) for path in positive_paths]
    neg_imgs = [_trim_image_whitespace(Image.open(path).convert("RGB")) for path in negative_paths]

    cell_w = max(max(img.width for img in pos_imgs), max(img.width for img in neg_imgs))
    cell_h = max(max(img.height for img in pos_imgs), max(img.height for img in neg_imgs))
    if max_cell_width > 0:
        cell_w = min(cell_w, max_cell_width)
    if max_cell_height > 0:
        cell_h = min(cell_h, max_cell_height)

    header_h = 72
    row_label_w = 180
    pad = 12
    cols = len(layer_labels)
    canvas_w = row_label_w + cols * (cell_w + pad) + pad
    canvas_h = header_h + 2 * (cell_h + pad) + pad
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = _load_font(40)
    row_font = _load_font(52)

    for idx, label in enumerate(layer_labels):
        x = row_label_w + pad + idx * (cell_w + pad) + cell_w // 2
        draw.text((x, 26), label, fill="black", anchor="mm", font=title_font)

    draw.text((row_label_w // 2, header_h + cell_h // 2), row1_label, fill="black", anchor="mm", font=row_font)
    draw.text((row_label_w // 2, header_h + pad + cell_h + cell_h // 2), row2_label, fill="black", anchor="mm", font=row_font)

    for idx, img in enumerate(pos_imgs):
        fitted = _fit_into_box(img, cell_w, cell_h)
        x = row_label_w + pad + idx * (cell_w + pad) + (cell_w - fitted.width) // 2
        y = header_h + (cell_h - fitted.height) // 2
        canvas.paste(fitted, (x, y))

    for idx, img in enumerate(neg_imgs):
        fitted = _fit_into_box(img, cell_w, cell_h)
        x = row_label_w + pad + idx * (cell_w + pad) + (cell_w - fitted.width) // 2
        y = header_h + pad + cell_h + (cell_h - fitted.height) // 2
        canvas.paste(fitted, (x, y))

    out_file.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_file)
    for img in list(pos_imgs) + list(neg_imgs):
        img.close()


def compose_overview_pair(
    positive_png: Path,
    negative_png: Path,
    out_file: Path,
    *,
    row1_label: str = "Positive",
    row2_label: str = "Negative",
    max_cell_width: int = 1200,
    max_cell_height: int = 1200,
) -> None:
    from PIL import Image, ImageDraw

    pos = _trim_image_whitespace(Image.open(positive_png).convert("RGB"))
    neg = _trim_image_whitespace(Image.open(negative_png).convert("RGB"))
    cell_w = max(pos.width, neg.width)
    cell_h = max(pos.height, neg.height)
    if max_cell_width > 0:
        cell_w = min(cell_w, max_cell_width)
    if max_cell_height > 0:
        cell_h = min(cell_h, max_cell_height)

    pad = 12
    label_w = 180
    header_h = 24
    canvas = Image.new("RGB", (label_w + cell_w + 2 * pad, header_h + 2 * cell_h + 3 * pad), "white")
    draw = ImageDraw.Draw(canvas)
    row_font = _load_font(52)

    draw.text((label_w // 2, header_h + pad + cell_h // 2), row1_label, fill="black", anchor="mm", font=row_font)
    draw.text((label_w // 2, header_h + 2 * pad + cell_h + cell_h // 2), row2_label, fill="black", anchor="mm", font=row_font)

    pos_fit = _fit_into_box(pos, cell_w, cell_h)
    neg_fit = _fit_into_box(neg, cell_w, cell_h)
    canvas.paste(pos_fit, (label_w + pad + (cell_w - pos_fit.width) // 2, header_h + pad + (cell_h - pos_fit.height) // 2))
    canvas.paste(neg_fit, (label_w + pad + (cell_w - neg_fit.width) // 2, header_h + 2 * pad + cell_h + (cell_h - neg_fit.height) // 2))

    out_file.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_file)
    pos.close()
    neg.close()


def export_single_board_plot_set(
    pcb_file: str | Path,
    out_root: Path,
    *,
    plot_mode: str,
    resolution_mm: float | None,
    include_zones: bool,
    bbox_mm: Tuple[float, float, float, float] | None,
    bbox_padding_mm: float,
    visible_layers: Sequence[str] | None,
    highlight_net_ids: Sequence[int],
    dpi: int,
    trim_whitespace: bool,
    pad_inches: float,
    clean_image: bool,
    png_width: int,
    subdir_name: str | None = None,
) -> Dict[str, object]:
    board = parse_kicad_pcb(pcb_file)
    export_layers = [layer for layer in (visible_layers or board.copper_layers) if layer in board.copper_layers]
    if not export_layers:
        export_layers = list(board.copper_layers)
    if not export_layers:
        raise ValueError(f"板子中没有可导出的铜层: {pcb_file}")
    _log_progress(
        f"board={Path(pcb_file).name} mode={plot_mode} layers={len(export_layers)} out={subdir_name or Path(pcb_file).stem}"
    )

    board_out = out_root / (subdir_name or Path(pcb_file).stem)
    png_out_dir = board_out / "png"
    overview_out_dir = board_out / "overview"
    png_out_dir.mkdir(parents=True, exist_ok=True)
    overview_out_dir.mkdir(parents=True, exist_ok=True)

    highlight_board = _build_board_subset_for_net_ids(board, highlight_net_ids) if highlight_net_ids else None
    layer_results: List[Dict[str, object]] = []

    if plot_mode == "vector":
        geometry = vectorize_board_geometry(
            board,
            include_zones=include_zones,
            visual_pad_scale=VISUAL_PAD_SCALE,
            visual_via_scale=VISUAL_VIA_SCALE,
        )
        highlight_geometry = (
            vectorize_board_geometry(
                highlight_board,
                include_zones=include_zones,
                visual_pad_scale=VISUAL_PAD_SCALE,
                visual_via_scale=VISUAL_VIA_SCALE,
            )
            if highlight_board is not None
            else None
        )
        for index, layer_name in enumerate(export_layers, start=1):
            _log_progress(f"board={Path(pcb_file).name} exporting layer {index}/{len(export_layers)} {layer_name}")
            png_file = png_out_dir / f"{index:02d}_{safe_name(layer_name)}.png"
            visualize_geometry(
                geometry,
                title=f"{Path(pcb_file).name} {layer_name}",
                highlight_geometry=highlight_geometry,
                bbox_mm=bbox_mm,
                zoom_to_bbox=bbox_mm is not None,
                bbox_padding_mm=bbox_padding_mm,
                visible_layers=[layer_name],
                output_path=png_file,
                show=False,
                dpi=dpi,
                trim_whitespace=trim_whitespace,
                pad_inches=pad_inches,
                clean_image=clean_image,
                png_width=png_width,
            )
            layer_results.append({"layer_name": layer_name, "display_name": layer_name, "png": png_file})

        overview_png = overview_out_dir / "overview_all_layers.png"
        _log_progress(f"board={Path(pcb_file).name} exporting overview(all layers merged)")
        visualize_geometry_overview(
            geometry,
            title=f"{Path(pcb_file).name} overview",
            highlight_geometry=highlight_geometry,
            bbox_mm=bbox_mm,
            zoom_to_bbox=bbox_mm is not None,
            bbox_padding_mm=bbox_padding_mm,
            visible_layers=export_layers,
            output_path=overview_png,
            show=False,
            dpi=dpi,
            trim_whitespace=trim_whitespace,
            pad_inches=pad_inches,
            clean_image=clean_image,
            png_width=png_width,
        )
    else:
        if resolution_mm is None:
            raise ValueError("grid 模式导出层图片时必须提供 --resolution")
        result = build_grid(board, resolution_mm=resolution_mm, include_zones=include_zones)
        highlight_result = build_highlight_grid(board, highlight_net_ids, resolution_mm=resolution_mm, include_zones=include_zones) if highlight_net_ids else None
        for index, layer_name in enumerate(export_layers, start=1):
            _log_progress(f"board={Path(pcb_file).name} exporting layer {index}/{len(export_layers)} {layer_name}")
            png_file = png_out_dir / f"{index:02d}_{safe_name(layer_name)}.png"
            visualize_grid(
                result,
                title=f"{Path(pcb_file).name} {layer_name}",
                highlight_result=highlight_result,
                bbox_mm=bbox_mm,
                zoom_to_bbox=bbox_mm is not None,
                bbox_padding_mm=bbox_padding_mm,
                output_path=png_file,
                show=False,
                dpi=dpi,
                trim_whitespace=trim_whitespace,
                pad_inches=pad_inches,
                clean_image=clean_image,
                visible_layers=[layer_name],
                png_width=png_width,
            )
            layer_results.append({"layer_name": layer_name, "display_name": layer_name, "png": png_file})

        overview_png = overview_out_dir / "overview_all_layers.png"
        _log_progress(f"board={Path(pcb_file).name} exporting overview(all layers merged)")
        visualize_grid_overview(
            result,
            title=f"{Path(pcb_file).name} overview",
            highlight_result=highlight_result,
            bbox_mm=bbox_mm,
            zoom_to_bbox=bbox_mm is not None,
            bbox_padding_mm=bbox_padding_mm,
            output_path=overview_png,
            show=False,
            dpi=dpi,
            trim_whitespace=trim_whitespace,
            pad_inches=pad_inches,
            clean_image=clean_image,
            visible_layers=export_layers,
            png_width=png_width,
        )
    _log_progress(f"board={Path(pcb_file).name} done")

    return {
        "pcb": Path(pcb_file),
        "board_out": board_out,
        "layers": export_layers,
        "layer_results": layer_results,
        "overview_png": overview_png,
    }


def _export_single_board_plot_set_worker(task: Dict[str, object]) -> Dict[str, object]:
    return export_single_board_plot_set(**task)


def _export_pair_plot_set_worker(task: Dict[str, object]) -> str:
    pair_key = str(task["pair_key"])
    pair_root = Path(task["pair_root"])

    _log_progress(f"pair={pair_key} exporting positive board")
    positive_result = export_single_board_plot_set(
        task["positive"],
        pair_root,
        plot_mode=str(task["plot_mode"]),
        resolution_mm=task["resolution_mm"],
        include_zones=bool(task["include_zones"]),
        bbox_mm=task["bbox_mm"],
        bbox_padding_mm=float(task["bbox_padding_mm"]),
        visible_layers=task["visible_layers"],
        highlight_net_ids=task["highlight_net_ids"],
        dpi=int(task["dpi"]),
        trim_whitespace=bool(task["trim_whitespace"]),
        pad_inches=float(task["pad_inches"]),
        clean_image=bool(task["clean_image"]),
        png_width=int(task["png_width"]),
        subdir_name="positive",
    )

    _log_progress(f"pair={pair_key} exporting negative board")
    negative_result = export_single_board_plot_set(
        task["negative"],
        pair_root,
        plot_mode=str(task["plot_mode"]),
        resolution_mm=task["resolution_mm"],
        include_zones=bool(task["include_zones"]),
        bbox_mm=task["bbox_mm"],
        bbox_padding_mm=float(task["bbox_padding_mm"]),
        visible_layers=task["visible_layers"],
        highlight_net_ids=task["highlight_net_ids"],
        dpi=int(task["dpi"]),
        trim_whitespace=bool(task["trim_whitespace"]),
        pad_inches=float(task["pad_inches"]),
        clean_image=bool(task["clean_image"]),
        png_width=int(task["png_width"]),
        subdir_name="negative",
    )

    pos_layers = positive_result["layer_results"]
    neg_layers = negative_result["layer_results"]
    common_count = min(len(pos_layers), len(neg_layers))
    if common_count > 0:
        _log_progress(f"pair={pair_key} composing layers comparison")
        compose_pair_grid(
            [item["png"] for item in pos_layers[:common_count]],
            [item["png"] for item in neg_layers[:common_count]],
            [str(item["display_name"]) for item in pos_layers[:common_count]],
            pair_root / "comparison" / "layers_comparison.png",
            max_cell_width=int(task["compare_cell_width"]),
            max_cell_height=int(task["compare_cell_height"]),
        )
    if positive_result["overview_png"] and negative_result["overview_png"]:
        _log_progress(f"pair={pair_key} composing overview comparison")
        compose_overview_pair(
            positive_result["overview_png"],
            negative_result["overview_png"],
            pair_root / "comparison" / "overview_comparison.png",
            max_cell_width=int(task["compare_cell_width"]),
            max_cell_height=int(task["compare_cell_height"]),
        )
    _log_progress(f"pair={pair_key} done")
    return pair_key


def _get_multiprocessing_context():
    start_methods = mp.get_all_start_methods()
    if "fork" in start_methods:
        return mp.get_context("fork")
    return mp.get_context()


def export_plot_sets_for_inputs(
    inputs: Sequence[str | Path],
    out_root: Path,
    *,
    plot_mode: str,
    resolution_mm: float | None,
    include_zones: bool,
    bbox_mm: Tuple[float, float, float, float] | None,
    bbox_padding_mm: float,
    visible_layers: Sequence[str] | None,
    highlight_net_ids: Sequence[int],
    dpi: int,
    trim_whitespace: bool,
    pad_inches: float,
    clean_image: bool,
    png_width: int,
    pair_subopt: bool,
    compare_cell_width: int,
    compare_cell_height: int,
    jobs: int,
) -> None:
    out_root.mkdir(parents=True, exist_ok=True)

    if pair_subopt:
        pairs = find_pcb_pairs(inputs)
        if not pairs:
            raise ValueError("没有找到成对的正负样本 .kicad_pcb 文件")
        _log_progress(f"pair_subopt mode: found {len(pairs)} positive/negative pairs")
        tasks = [
            {
                "pair_key": str(pair["key"]),
                "pair_root": out_root / str(pair["key"]),
                "positive": pair["positive"],
                "negative": pair["negative"],
                "plot_mode": plot_mode,
                "resolution_mm": resolution_mm,
                "include_zones": include_zones,
                "bbox_mm": bbox_mm,
                "bbox_padding_mm": bbox_padding_mm,
                "visible_layers": list(visible_layers) if visible_layers is not None else None,
                "highlight_net_ids": list(highlight_net_ids),
                "dpi": dpi,
                "trim_whitespace": trim_whitespace,
                "pad_inches": pad_inches,
                "clean_image": clean_image,
                "png_width": png_width,
                "compare_cell_width": compare_cell_width,
                "compare_cell_height": compare_cell_height,
            }
            for pair in pairs
        ]
        if jobs <= 1 or len(tasks) <= 1:
            for task in tasks:
                _export_pair_plot_set_worker(task)
        else:
            _log_progress(f"pair_subopt mode: using {jobs} worker processes")
            with ProcessPoolExecutor(max_workers=jobs, mp_context=_get_multiprocessing_context()) as executor:
                futures = [executor.submit(_export_pair_plot_set_worker, task) for task in tasks]
                for future in as_completed(futures):
                    future.result()
        return

    pcb_files = find_pcb_files(inputs)
    if not pcb_files:
        raise ValueError("没有找到 .kicad_pcb 文件")
    _log_progress(f"single/batch mode: found {len(pcb_files)} board files")
    tasks = [
        {
            "pcb_file": pcb_file,
            "out_root": out_root,
            "plot_mode": plot_mode,
            "resolution_mm": resolution_mm,
            "include_zones": include_zones,
            "bbox_mm": bbox_mm,
            "bbox_padding_mm": bbox_padding_mm,
            "visible_layers": list(visible_layers) if visible_layers is not None else None,
            "highlight_net_ids": list(highlight_net_ids),
            "dpi": dpi,
            "trim_whitespace": trim_whitespace,
            "pad_inches": pad_inches,
            "clean_image": clean_image,
            "png_width": png_width,
            "subdir_name": None,
        }
        for pcb_file in pcb_files
    ]
    if jobs <= 1 or len(tasks) <= 1:
        for task in tasks:
            _export_single_board_plot_set_worker(task)
        return

    _log_progress(f"single/batch mode: using {jobs} worker processes")
    with ProcessPoolExecutor(max_workers=jobs, mp_context=_get_multiprocessing_context()) as executor:
        futures = [executor.submit(_export_single_board_plot_set_worker, task) for task in tasks]
        for future in as_completed(futures):
            future.result()


def run_synthetic_demo(resolution_mm: float = 0.1, show: bool = True) -> VoxelizationResult:
    board = make_synthetic_example_board()
    result = build_grid(board, resolution_mm=resolution_mm, include_zones=False)
    if show:
        visualize_grid(result, title="Synthetic KiCad Voxelization Demo")
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Voxelize KiCad PCB copper into a (H, W, L) occupancy grid.")
    parser.add_argument("inputs", nargs="*", help="Path(s) to .kicad_pcb file(s) or directories. Omit together with --demo to run the synthetic example.")
    parser.add_argument("--resolution", type=float, default=None, help="Grid resolution in millimetres per pixel. Only needed for grid rasterization.")
    parser.add_argument("--exclude-zones", action="store_true", help="Skip copper zone rasterization.")
    parser.add_argument("--demo", action="store_true", help="Run the built-in 2-layer synthetic example.")
    parser.add_argument("--plot", action="store_true", help="Display a matplotlib visualization.")
    parser.add_argument(
        "--export-layer-images",
        action="store_true",
        help="Batch export one saved image per copper layer plus an overview image for every input board.",
    )
    parser.add_argument(
        "--export-out",
        type=Path,
        default=Path("voxelized_layer_exports"),
        help="Output directory for --export-layer-images. Default: voxelized_layer_exports",
    )
    parser.add_argument(
        "--pair-subopt",
        action="store_true",
        help="Treat output_bga.* and output_bga.subopt.* as positive/negative pairs and build comparison overviews.",
    )
    parser.add_argument(
        "--compare-cell-width",
        type=int,
        default=1200,
        help="Maximum width of each tile inside pair comparison images.",
    )
    parser.add_argument(
        "--compare-cell-height",
        type=int,
        default=1200,
        help="Maximum height of each tile inside pair comparison images.",
    )
    parser.add_argument(
        "--plot-mode",
        choices=("grid", "vector"),
        default="grid",
        help="Visualization mode: 'grid' shows rasterized occupancy, 'vector' shows exact mm-space copper polygons.",
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional .npz output path for grid data.")
    parser.add_argument(
        "--plot-output",
        type=Path,
        default=None,
        help="Optional image output path for the matplotlib figure, useful on headless servers.",
    )
    parser.add_argument(
        "--plot-dpi",
        type=int,
        default=200,
        help="Output DPI for saved plot images. Increase this to export a sharper PNG.",
    )
    parser.add_argument(
        "--png-width",
        type=int,
        default=0,
        help="Resize saved PNG output to the target width in pixels after export, for example --png-width 4096.",
    )
    parser.add_argument(
        "--plot-pad-inches",
        type=float,
        default=0.02,
        help="Outer padding to keep when trimming saved plot whitespace with bbox_inches=tight.",
    )
    parser.add_argument(
        "--trim-whitespace",
        action="store_true",
        help="Explicitly trim surrounding whitespace after saving PNG output.",
    )
    parser.add_argument(
        "--no-trim-whitespace",
        action="store_true",
        help="Disable automatic whitespace trimming when saving plot images.",
    )
    parser.add_argument(
        "--clean-plot",
        action="store_true",
        help="Hide axes, labels, and titles to generate a tighter image with less surrounding blank area.",
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Optional bbox in millimetres. If provided, print every net that intersects this region.",
    )
    parser.add_argument(
        "--bbox-layers",
        nargs="*",
        default=None,
        metavar="LAYER",
        help="Optional copper-layer filter for bbox net extraction, for example: --bbox-layers Top Bottom",
    )
    parser.add_argument(
        "--bbox-padding",
        type=float,
        default=1.0,
        help="Extra margin in millimetres when plotting a bbox zoom view.",
    )
    parser.add_argument(
        "--highlight-net-id",
        type=int,
        action="append",
        default=None,
        help="Highlight one or more net IDs in the visualization. Repeat this flag to highlight multiple nets.",
    )
    parser.add_argument(
        "--highlight-missing-json",
        type=Path,
        default=None,
        help="Parse a training-corpus missing-net JSON and highlight all missing net IDs.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 1) - 1),
        help="Worker process count for batch export. On Linux this uses multiprocessing with fork.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    show_plot = args.plot
    if args.plot and args.plot_output is None and _is_noninteractive_matplotlib_backend():
        print("note=matplotlib backend is non-interactive; no GUI window will appear. Use --plot-output to save the figure.")
    trim_whitespace = False if args.no_trim_whitespace else True
    if args.trim_whitespace:
        trim_whitespace = True
    highlight_net_ids: List[int] = []
    if args.highlight_net_id:
        highlight_net_ids.extend(int(value) for value in args.highlight_net_id)
    if args.highlight_missing_json is not None:
        highlight_net_ids.extend(parse_missing_net_ids_from_json(args.highlight_missing_json))
    highlight_net_ids = sorted(set(highlight_net_ids))
    bbox_mm = tuple(args.bbox) if args.bbox is not None else None

    if args.export_layer_images:
        if not args.inputs:
            parser.error("--export-layer-images 需要至少提供一个 .kicad_pcb 文件或目录")
        export_plot_sets_for_inputs(
            args.inputs,
            args.export_out,
            plot_mode=args.plot_mode,
            resolution_mm=args.resolution,
            include_zones=not args.exclude_zones,
            bbox_mm=bbox_mm,
            bbox_padding_mm=args.bbox_padding,
            visible_layers=args.bbox_layers,
            highlight_net_ids=highlight_net_ids,
            dpi=args.plot_dpi,
            trim_whitespace=trim_whitespace,
            pad_inches=args.plot_pad_inches,
            clean_image=args.clean_plot,
            png_width=args.png_width,
            pair_subopt=args.pair_subopt,
            compare_cell_width=args.compare_cell_width,
            compare_cell_height=args.compare_cell_height,
            jobs=max(1, args.jobs),
        )
        print(f"export_out={args.export_out}")
        print("export_mode=layer_images")
        return

    if args.demo or not args.inputs:
        resolution_mm = args.resolution if args.resolution is not None else 0.1
        result = run_synthetic_demo(resolution_mm=resolution_mm, show=False)
        source_name = "<synthetic>"
        board = make_synthetic_example_board()
        if args.plot:
            if args.plot_mode == "vector":
                geometry = vectorize_board_geometry(
                    board,
                    include_zones=False,
                    visual_pad_scale=VISUAL_PAD_SCALE,
                    visual_via_scale=VISUAL_VIA_SCALE,
                )
                visualize_geometry(
                    geometry,
                    title="Synthetic KiCad Geometry Demo",
                    bbox_mm=bbox_mm,
                    zoom_to_bbox=args.bbox is not None,
                    bbox_padding_mm=args.bbox_padding,
                    visible_layers=args.bbox_layers,
                    output_path=args.plot_output,
                    show=show_plot,
                    dpi=args.plot_dpi,
                    trim_whitespace=trim_whitespace,
                    pad_inches=args.plot_pad_inches,
                    clean_image=args.clean_plot,
                    png_width=args.png_width,
                )
            else:
                visualize_grid(
                    result,
                    title="Synthetic KiCad Voxelization Demo",
                    bbox_mm=bbox_mm,
                    zoom_to_bbox=args.bbox is not None,
                    bbox_padding_mm=args.bbox_padding,
                    output_path=args.plot_output,
                    show=show_plot,
                    dpi=args.plot_dpi,
                    trim_whitespace=trim_whitespace,
                    pad_inches=args.plot_pad_inches,
                    clean_image=args.clean_plot,
                    png_width=args.png_width,
                )
    else:
        if len(args.inputs) != 1:
            parser.error("普通模式只接受一个 .kicad_pcb 文件；批量处理请使用 --export-layer-images")
        board = parse_kicad_pcb(args.inputs[0])
        source_name = args.inputs[0]
        if args.plot_mode == "vector":
            geometry = vectorize_board_geometry(
                board,
                include_zones=not args.exclude_zones,
                visual_pad_scale=VISUAL_PAD_SCALE,
                visual_via_scale=VISUAL_VIA_SCALE,
            )
            highlight_geometry = None
            if highlight_net_ids:
                highlight_board = _build_board_subset_for_net_ids(board, highlight_net_ids)
                highlight_geometry = vectorize_board_geometry(
                    highlight_board,
                    include_zones=not args.exclude_zones,
                    visual_pad_scale=VISUAL_PAD_SCALE,
                    visual_via_scale=VISUAL_VIA_SCALE,
                )
            if args.plot:
                visualize_geometry(
                    geometry,
                    title=f"Exact Geometry {Path(args.inputs[0]).name}",
                    highlight_geometry=highlight_geometry,
                    bbox_mm=bbox_mm,
                    zoom_to_bbox=args.bbox is not None,
                    bbox_padding_mm=args.bbox_padding,
                    visible_layers=args.bbox_layers,
                    output_path=args.plot_output,
                    show=show_plot,
                    dpi=args.plot_dpi,
                    trim_whitespace=trim_whitespace,
                    pad_inches=args.plot_pad_inches,
                    clean_image=args.clean_plot,
                    png_width=args.png_width,
                )
            result = None
        else:
            if args.resolution is None:
                parser.error("grid 模式需要 --resolution；如果你想无损可视化，请改用 --plot --plot-mode vector")
            result = build_grid(
                board,
                resolution_mm=args.resolution,
                include_zones=not args.exclude_zones,
            )
            highlight_result = None
            if highlight_net_ids:
                highlight_result = build_highlight_grid(
                    board,
                    highlight_net_ids,
                    resolution_mm=args.resolution,
                    include_zones=not args.exclude_zones,
                )
            if args.plot:
                visualize_grid(
                    result,
                    title=f"Voxelized {Path(args.inputs[0]).name}",
                    highlight_result=highlight_result,
                    bbox_mm=bbox_mm,
                    zoom_to_bbox=args.bbox is not None,
                    bbox_padding_mm=args.bbox_padding,
                    output_path=args.plot_output,
                    show=show_plot,
                    dpi=args.plot_dpi,
                    trim_whitespace=trim_whitespace,
                    pad_inches=args.plot_pad_inches,
                    clean_image=args.clean_plot,
                    png_width=args.png_width,
                )

    bbox_result = None
    if args.bbox is not None:
        bbox_result = extract_nets_in_bbox(
            board,
            bbox_mm=tuple(args.bbox),
            layers=args.bbox_layers,
            include_zones=not args.exclude_zones,
        )

    if args.output is not None:
        if result is None:
            parser.error("vector 模式下没有栅格输出，不能使用 --output；如需保存栅格，请使用 --plot-mode grid 并提供 --resolution")
        np.savez_compressed(
            args.output,
            grid=result.grid,
            layer_names=np.asarray(result.layer_names, dtype=object),
            origin_mm=np.asarray(result.origin_mm, dtype=np.float64),
            origin_units=np.asarray(result.origin_units, dtype=np.int64),
            resolution_mm=np.asarray([result.resolution_mm], dtype=np.float64),
            resolution_units=np.asarray([result.resolution_units], dtype=np.int64),
            board_bbox_mm=np.asarray(result.board_bbox_mm, dtype=np.float64),
            board_bbox_units=np.asarray(result.board_bbox_units, dtype=np.int64),
            units_per_mm=np.asarray([result.units_per_mm], dtype=np.int64),
            edges=_edges_array(result.edges),
        )

    print(f"source={source_name}")
    if result is None:
        print("plot_mode=vector")
        print(f"board_bbox_mm={board.bbox_mm}")
        print(f"board_bbox_units={board.bbox_units}")
        print(f"units_per_mm={board.units_per_mm}")
        print(f"layers={board.copper_layers}")
        print(f"segments={len(board.segments)}")
        print(f"vias={len(board.vias)}")
        print(f"pads={sum(len(v) for v in board.pads_by_net.values())}")
        print(f"zones={len(board.zones)}")
        if args.highlight_missing_json is not None or args.highlight_net_id:
            highlight_ids: List[int] = []
            if args.highlight_net_id:
                highlight_ids.extend(int(value) for value in args.highlight_net_id)
            if args.highlight_missing_json is not None:
                highlight_ids.extend(parse_missing_net_ids_from_json(args.highlight_missing_json))
            highlight_ids = sorted(set(highlight_ids))
            print(f"highlight_net_ids={highlight_ids}")
    else:
        print("plot_mode=grid")
        print(f"grid_shape={result.grid.shape}")
        print(f"origin_mm={result.origin_mm}")
        print(f"origin_units={result.origin_units}")
        print(f"resolution_mm={result.resolution_mm}")
        print(f"resolution_units={result.resolution_units}")
        print(f"units_per_mm={result.units_per_mm}")
        print(f"layers={result.layer_names}")
        print(f"occupied_voxels={int(result.grid.sum())}")
        print(f"via_edges={len(result.edges)}")
        if args.highlight_missing_json is not None or args.highlight_net_id:
            highlight_ids: List[int] = []
            if args.highlight_net_id:
                highlight_ids.extend(int(value) for value in args.highlight_net_id)
            if args.highlight_missing_json is not None:
                highlight_ids.extend(parse_missing_net_ids_from_json(args.highlight_missing_json))
            highlight_ids = sorted(set(highlight_ids))
            print(f"highlight_net_ids={highlight_ids}")

    if bbox_result is not None:
        print(f"bbox_mm={bbox_result.bbox_mm}")
        print(f"bbox_units={bbox_result.bbox_units}")
        print(f"bbox_layers={bbox_result.layers}")
        print(f"bbox_net_count={len(bbox_result.hits)}")
        for hit in bbox_result.hits:
            print(
                "net_hit "
                f"id={hit.net_id} "
                f"name={hit.net_name!r} "
                f"layers={hit.layers} "
                f"segments={hit.segment_count} "
                f"vias={hit.via_count} "
                f"pads={hit.pad_count} "
                f"zones={hit.zone_count}"
            )


if __name__ == "__main__":
    main()
