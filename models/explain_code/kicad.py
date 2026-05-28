from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


Token = str
SExpr = List[Any]
Point = Tuple[float, float]
Polygon = Tuple[Point, ...]
IntPoint = Tuple[int, int]
IntPolygon = Tuple[IntPoint, ...]

PCB_UNITS_PER_MM = 1_000_000
PCB_UNIT_NAME = "nm"
_PCB_UNITS_PER_MM_DECIMAL = Decimal(PCB_UNITS_PER_MM)
_ZERO_DECIMAL = Decimal("0")
_PLAIN_DECIMAL_RE = re.compile(r"([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))")


def _decode_token(token: str) -> str:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        body = token[1:-1]
        return bytes(body, "utf-8").decode("unicode_escape")
    return token


def tokenize_sexpr(text: str) -> Iterator[Token]:
    token_re = re.compile(r'\s*(\(|\)|"(?:\\.|[^"\\])*"|[^\s()]+)')
    pos = 0
    text_len = len(text)
    while pos < text_len:
        match = token_re.match(text, pos)
        if not match:
            if text[pos:].strip() == "":
                break
            tail = text[pos : pos + 80]
            raise ValueError(f"无法解析 KiCad s-expression，位置 {pos}: {tail!r}")
        token = match.group(1)
        pos = match.end()
        if token:
            yield token


def parse_sexpr(text: str) -> SExpr:
    stack: List[SExpr] = []
    root: SExpr = []
    current = root
    for token in tokenize_sexpr(text):
        if token == "(":
            child: SExpr = []
            current.append(child)
            stack.append(current)
            current = child
        elif token == ")":
            if not stack:
                raise ValueError("KiCad s-expression 括号不匹配")
            current = stack.pop()
        else:
            current.append(_decode_token(token))
    if stack:
        raise ValueError("KiCad s-expression 括号未闭合")
    if len(root) != 1 or not isinstance(root[0], list):
        raise ValueError("KiCad 文件根节点异常")
    return root[0]


def _head(expr: Sequence[Any]) -> str:
    return expr[0] if expr and isinstance(expr[0], str) else ""


def _children(expr: Sequence[Any], head: str) -> List[SExpr]:
    return [child for child in expr[1:] if isinstance(child, list) and _head(child) == head]


def _first_child(expr: Sequence[Any], head: str) -> Optional[SExpr]:
    for child in expr[1:]:
        if isinstance(child, list) and _head(child) == head:
            return child
    return None


def _atom(expr: Sequence[Any], index: int, default: Optional[str] = None) -> Optional[str]:
    if len(expr) > index and isinstance(expr[index], str):
        return expr[index]
    return default


def _decimal_from_value(value: str | float | Decimal | int) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


@lru_cache(maxsize=32768)
def _mm_string_to_pcb_units(value: str) -> Optional[int]:
    stripped = value.strip()
    if not stripped:
        return None
    match = _PLAIN_DECIMAL_RE.fullmatch(stripped)
    if not match:
        return None

    sign, int_part, frac_part_a, frac_part_b = match.groups()
    whole_units = int(int_part or "0") * PCB_UNITS_PER_MM
    frac_digits = frac_part_a if frac_part_a is not None else (frac_part_b or "")
    frac_head = frac_digits[:6].ljust(6, "0")
    frac_units = int(frac_head) if frac_head else 0
    discarded = frac_digits[6:]
    if discarded and discarded[0] >= "5":
        frac_units += 1
        if frac_units >= PCB_UNITS_PER_MM:
            whole_units += PCB_UNITS_PER_MM
            frac_units -= PCB_UNITS_PER_MM

    total_units = whole_units + frac_units
    return -total_units if sign == "-" else total_units


def mm_to_pcb_units(value: str | float | Decimal | int, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value * PCB_UNITS_PER_MM
    if isinstance(value, str):
        parsed_units = _mm_string_to_pcb_units(value)
        if parsed_units is not None:
            return parsed_units
    if isinstance(value, float):
        scaled = value * PCB_UNITS_PER_MM
        if math.isfinite(scaled):
            return math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    decimal_value = _decimal_from_value(value)
    scaled = decimal_value * _PCB_UNITS_PER_MM_DECIMAL
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def pcb_units_to_mm(value: int) -> float:
    return value / PCB_UNITS_PER_MM


def pcb_units_point_to_mm(point: IntPoint) -> Point:
    return (pcb_units_to_mm(point[0]), pcb_units_to_mm(point[1]))


def pcb_units_size_to_mm(size: Tuple[int, int]) -> Tuple[float, float]:
    return (pcb_units_to_mm(size[0]), pcb_units_to_mm(size[1]))


def pcb_units_polygon_to_mm(polygon: IntPolygon) -> Polygon:
    return tuple(pcb_units_point_to_mm(point) for point in polygon)


def _to_float(value: Optional[str], default: float = 0.0) -> float:
    if value is None:
        return default
    return float(_decimal_from_value(value))


def _to_int(value: Optional[str], default: int = 0) -> int:
    if value is None:
        return default
    return int(float(value))


@dataclass
class Segment:
    start: Point
    end: Point
    width_mm: float
    layer: str
    net_id: int
    start_units: IntPoint = (0, 0)
    end_units: IntPoint = (0, 0)
    width_units: int = 0


@dataclass
class Via:
    at: Point
    size_mm: float
    drill_mm: float
    layers: Tuple[str, ...]
    net_id: int
    at_units: IntPoint = (0, 0)
    size_units: int = 0
    drill_units: int = 0


@dataclass
class Pad:
    name: str
    x_mm: float
    y_mm: float
    layer: str
    net_id: int
    net_name: str
    size_mm: Tuple[float, float]
    module_ref: str = ""
    module_name: str = ""
    shape: str = "rect"
    kind: str = ""
    rotation_deg: float = 0.0
    copper_layers: Tuple[str, ...] = ()
    drill_mm: Tuple[float, float] = (0.0, 0.0)
    roundrect_rratio: float = 0.0
    x_units: int = 0
    y_units: int = 0
    size_units: Tuple[int, int] = (0, 0)
    drill_units: Tuple[int, int] = (0, 0)


@dataclass
class Zone:
    layers: Tuple[str, ...]
    net_id: int
    net_name: str
    polygons: List[Polygon] = field(default_factory=list)
    polygons_units: List[IntPolygon] = field(default_factory=list)


@dataclass
class BoardRules:
    trace_clearance_mm: float = 0.0
    trace_width_mm: float = 0.0
    trace_min_mm: float = 0.0
    via_size_mm: float = 0.0
    via_drill_mm: float = 0.0
    trace_clearance_units: int = 0
    trace_width_units: int = 0
    trace_min_units: int = 0
    via_size_units: int = 0
    via_drill_units: int = 0


@dataclass
class BoardData:
    path: Path
    nets: Dict[int, str]
    layers: Dict[int, str]
    copper_layers: List[str]
    layer_order: Dict[str, int]
    copper_layer_map: Dict[str, int]
    width_mm: float
    height_mm: float
    bbox_mm: Tuple[float, float, float, float]
    rules: BoardRules
    segments: List[Segment] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    pads_by_net: Dict[int, List[Pad]] = field(default_factory=dict)
    zones: List[Zone] = field(default_factory=list)
    units_per_mm: int = PCB_UNITS_PER_MM
    coordinate_unit_name: str = PCB_UNIT_NAME
    width_units: int = 0
    height_units: int = 0
    bbox_units: Tuple[int, int, int, int] = (0, 0, 0, 0)


def _parse_layers(root: SExpr) -> Tuple[Dict[int, str], List[str], Dict[str, int], Dict[str, int]]:
    layers_expr = _first_child(root, "layers")
    layer_names: Dict[int, str] = {}
    copper: List[Tuple[int, str]] = []
    if layers_expr:
        for item in layers_expr[1:]:
            if not isinstance(item, list) or len(item) < 3:
                continue
            index = _to_int(_atom(item, 0))
            name = _atom(item, 1, "") or ""
            kind = _atom(item, 2, "") or ""
            layer_names[index] = name
            if kind in {"signal", "power", "mixed"}:
                copper.append((index, name))
    # KiCad numeric layer ids do not reflect copper stack order because B.Cu
    # often has a low numeric id. Keep F.Cu first, inner layers in numeric order,
    # and B.Cu last so through vias expand across all copper layers correctly.
    def copper_sort_key(pair: Tuple[int, str]) -> Tuple[int, int]:
        index, name = pair
        if name == "F.Cu":
            return (0, index)
        if name == "B.Cu":
            return (2, index)
        return (1, index)

    copper.sort(key=copper_sort_key)
    copper_names = [name for _, name in copper]
    layer_order = {name: physical_index for physical_index, name in copper}
    copper_layer_map = {name: dense_index for dense_index, name in enumerate(copper_names)}
    return layer_names, copper_names, layer_order, copper_layer_map


def _parse_rules(root: SExpr) -> BoardRules:
    setup = _first_child(root, "setup")
    if not setup:
        return BoardRules()

    def read_number(field: str, fallback: float = 0.0) -> float:
        expr = _first_child(setup, field)
        if expr and len(expr) >= 2 and isinstance(expr[1], str):
            return float(_decimal_from_value(expr[1]))
        return fallback

    def read_units(field: str, fallback: int = 0) -> int:
        expr = _first_child(setup, field)
        if expr and len(expr) >= 2 and isinstance(expr[1], str):
            return mm_to_pcb_units(expr[1])
        return fallback

    trace_width_mm = read_number("last_trace_width", 0.0)
    trace_width_units = read_units("last_trace_width", 0)
    if not trace_width_mm:
        trace_width_mm = read_number("segment_width", 0.0)
    if not trace_width_units:
        trace_width_units = read_units("segment_width", 0)

    trace_min_mm = read_number("trace_min", trace_width_mm)
    trace_min_units = read_units("trace_min", trace_width_units)

    return BoardRules(
        trace_clearance_mm=read_number("trace_clearance", 0.0),
        trace_width_mm=trace_width_mm,
        trace_min_mm=trace_min_mm,
        via_size_mm=read_number("via_size", 0.0),
        via_drill_mm=read_number("via_drill", 0.0),
        trace_clearance_units=read_units("trace_clearance", 0),
        trace_width_units=trace_width_units,
        trace_min_units=trace_min_units,
        via_size_units=read_units("via_size", 0),
        via_drill_units=read_units("via_drill", 0),
    )


def _parse_nets(root: SExpr) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for item in root[1:]:
        if isinstance(item, list) and _head(item) == "net" and len(item) >= 3:
            result[_to_int(_atom(item, 1))] = _atom(item, 2, "") or ""
    return result


def _module_reference(module_expr: SExpr) -> str:
    for fp_text in _children(module_expr, "fp_text"):
        if _atom(fp_text, 1) == "reference":
            return _atom(fp_text, 2, "") or ""
    return ""


def _rotate(x_mm: float, y_mm: float, angle_deg: float) -> Point:
    angle = math.radians(angle_deg)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    return (
        x_mm * cos_a - y_mm * sin_a,
        x_mm * sin_a + y_mm * cos_a,
    )


def _normalize_layer_name(layer_name: str, copper_layers: Sequence[str]) -> str:
    copper_set = set(copper_layers)
    if layer_name in copper_set:
        return layer_name
    aliases = {
        "F.Cu": "Top",
        "B.Cu": "Bottom",
        "Top": "F.Cu",
        "Bottom": "B.Cu",
    }
    alias = aliases.get(layer_name, layer_name)
    return alias if alias in copper_set else layer_name


def _expand_copper_layers(layer_tokens: Iterable[str], copper_layers: Sequence[str]) -> Tuple[str, ...]:
    copper_set = set(copper_layers)
    result: List[str] = []
    for token in layer_tokens:
        if token == "*.Cu":
            for layer in copper_layers:
                if layer not in result:
                    result.append(layer)
            continue
        if token == "F&B.Cu":
            for candidate in ("F.Cu", "Top", "B.Cu", "Bottom"):
                normalized = _normalize_layer_name(candidate, copper_layers)
                if normalized in copper_set and normalized not in result:
                    result.append(normalized)
            continue
        if token == "In*.Cu":
            for layer in copper_layers[1:-1]:
                if layer not in result:
                    result.append(layer)
            continue
        normalized = _normalize_layer_name(token, copper_layers)
        if normalized in copper_set and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _expand_via_layers(layer_tokens: Iterable[str], copper_layers: Sequence[str], layer_order: Dict[str, int]) -> Tuple[str, ...]:
    exact_layers = _expand_copper_layers(layer_tokens, copper_layers)
    if len(exact_layers) >= 2:
        copper_index = {layer: index for index, layer in enumerate(copper_layers)}
        ordered_indices = [copper_index[layer] for layer in exact_layers if layer in copper_index]
        if len(ordered_indices) >= 2:
            lo = min(ordered_indices)
            hi = max(ordered_indices)
            return tuple(copper_layers[lo : hi + 1])
    return exact_layers


def _first_copper_layer(layer_tokens: Iterable[str], copper_layers: Sequence[str], fallback: str) -> str:
    expanded = _expand_copper_layers(layer_tokens, copper_layers)
    if expanded:
        return expanded[0]
    return _normalize_layer_name(fallback, copper_layers)


def _module_is_bottom(module_layer: str) -> bool:
    return module_layer in {"Bottom", "B.Cu"}


def _pad_global_position_units(
    pad_dx_units: int,
    pad_dy_units: int,
    module_x_units: int,
    module_y_units: int,
    module_angle: float,
    mirrored: bool,
) -> IntPoint:
    local_x_units = -pad_dx_units if mirrored else pad_dx_units
    local_y_units = pad_dy_units
    rot_x_mm, rot_y_mm = _rotate(
        pcb_units_to_mm(local_x_units),
        pcb_units_to_mm(local_y_units),
        module_angle,
    )
    return (
        mm_to_pcb_units(pcb_units_to_mm(module_x_units) + rot_x_mm),
        mm_to_pcb_units(pcb_units_to_mm(module_y_units) + rot_y_mm),
    )


def _pad_rotation_deg(module_angle: float, pad_angle: float, mirrored: bool) -> float:
    return module_angle - pad_angle if mirrored else module_angle + pad_angle


def _parse_pad_drill(pad_expr: SExpr) -> Tuple[Tuple[float, float], Tuple[int, int]]:
    drill_expr = _first_child(pad_expr, "drill")
    if not drill_expr:
        return (0.0, 0.0), (0, 0)
    numbers = [
        token
        for token in drill_expr[1:]
        if isinstance(token, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", token)
    ]
    if not numbers:
        return (0.0, 0.0), (0, 0)
    if len(numbers) == 1:
        units = mm_to_pcb_units(numbers[0])
        return (pcb_units_to_mm(units), pcb_units_to_mm(units)), (units, units)
    units_x = mm_to_pcb_units(numbers[0])
    units_y = mm_to_pcb_units(numbers[1])
    return (pcb_units_to_mm(units_x), pcb_units_to_mm(units_y)), (units_x, units_y)


def _parse_pts_units(expr: Sequence[Any]) -> IntPolygon:
    pts_expr = expr if _head(expr) == "pts" else _first_child(expr, "pts")
    if not pts_expr:
        return ()
    points: List[IntPoint] = []
    for child in pts_expr[1:]:
        if isinstance(child, list) and _head(child) == "xy":
            points.append(
                (
                    mm_to_pcb_units(_atom(child, 1)),
                    mm_to_pcb_units(_atom(child, 2)),
                )
            )
    return tuple(points)


def _all_pads(pads_by_net: Dict[int, List[Pad]]) -> List[Pad]:
    pads: List[Pad] = []
    for pad_list in pads_by_net.values():
        pads.extend(pad_list)
    return pads


def _parse_pads(root: SExpr, nets: Dict[int, str], copper_layers: Sequence[str]) -> Dict[int, List[Pad]]:
    pads_by_net: Dict[int, List[Pad]] = {}
    module_heads = {"module", "footprint"}
    for item in root[1:]:
        if not isinstance(item, list) or _head(item) not in module_heads:
            continue
        module_name = _atom(item, 1, "") or ""
        module_ref = _module_reference(item)
        at_expr = _first_child(item, "at")
        module_x_units = mm_to_pcb_units(_atom(at_expr or [], 1))
        module_y_units = mm_to_pcb_units(_atom(at_expr or [], 2))
        module_angle = _to_float(_atom(at_expr or [], 3), 0.0)
        module_layer = _normalize_layer_name(_atom(_first_child(item, "layer") or [], 1, "Top") or "Top", copper_layers)
        mirrored = _module_is_bottom(module_layer)
        for pad_expr in _children(item, "pad"):
            pad_name = _atom(pad_expr, 1, "") or ""
            pad_kind = _atom(pad_expr, 2, "") or ""
            pad_shape = (_atom(pad_expr, 3, "rect") or "rect").lower()
            pad_at = _first_child(pad_expr, "at")
            if not pad_at:
                continue
            pad_dx_units = mm_to_pcb_units(_atom(pad_at, 1))
            pad_dy_units = mm_to_pcb_units(_atom(pad_at, 2))
            pad_angle = _to_float(_atom(pad_at, 3), 0.0)
            pad_x_units, pad_y_units = _pad_global_position_units(
                pad_dx_units,
                pad_dy_units,
                module_x_units,
                module_y_units,
                module_angle,
                mirrored,
            )
            net_expr = _first_child(pad_expr, "net")
            net_id = _to_int(_atom(net_expr or [], 1), 0)
            layers_expr = _first_child(pad_expr, "layers")
            layer_tokens = [token for token in (layers_expr[1:] if layers_expr else []) if isinstance(token, str)]
            if not layer_tokens:
                layer_tokens = [module_layer]
            pad_layers = _expand_copper_layers(layer_tokens, copper_layers)
            pad_layer = pad_layers[0] if pad_layers else _first_copper_layer(layer_tokens, copper_layers, module_layer)
            size_expr = _first_child(pad_expr, "size")
            size_units = (
                mm_to_pcb_units(_atom(size_expr or [], 1)),
                mm_to_pcb_units(_atom(size_expr or [], 2)),
            )
            size_mm = pcb_units_size_to_mm(size_units)
            roundrect_rratio = _to_float(_atom(_first_child(pad_expr, "roundrect_rratio") or [], 1), 0.0)
            drill_mm, drill_units = _parse_pad_drill(pad_expr)
            pad = Pad(
                name=pad_name,
                x_mm=pcb_units_to_mm(pad_x_units),
                y_mm=pcb_units_to_mm(pad_y_units),
                layer=pad_layer,
                net_id=net_id,
                net_name=nets.get(net_id, ""),
                size_mm=size_mm,
                module_ref=module_ref,
                module_name=module_name,
                shape=pad_shape,
                kind=pad_kind,
                rotation_deg=_pad_rotation_deg(module_angle, pad_angle, mirrored),
                copper_layers=pad_layers,
                drill_mm=drill_mm,
                roundrect_rratio=roundrect_rratio,
                x_units=pad_x_units,
                y_units=pad_y_units,
                size_units=size_units,
                drill_units=drill_units,
            )
            pads_by_net.setdefault(net_id, []).append(pad)
    return pads_by_net


def _parse_segments(root: SExpr, copper_layers: Sequence[str]) -> List[Segment]:
    result: List[Segment] = []
    for item in root[1:]:
        if not isinstance(item, list) or _head(item) != "segment":
            continue
        start_expr = _first_child(item, "start")
        end_expr = _first_child(item, "end")
        width_expr = _first_child(item, "width")
        layer_expr = _first_child(item, "layer")
        net_expr = _first_child(item, "net")
        if not (start_expr and end_expr and layer_expr and net_expr):
            continue
        start_units = (
            mm_to_pcb_units(_atom(start_expr, 1)),
            mm_to_pcb_units(_atom(start_expr, 2)),
        )
        end_units = (
            mm_to_pcb_units(_atom(end_expr, 1)),
            mm_to_pcb_units(_atom(end_expr, 2)),
        )
        width_units = mm_to_pcb_units(_atom(width_expr or [], 1))
        result.append(
            Segment(
                start=pcb_units_point_to_mm(start_units),
                end=pcb_units_point_to_mm(end_units),
                width_mm=pcb_units_to_mm(width_units),
                layer=_normalize_layer_name(_atom(layer_expr, 1, "") or "", copper_layers),
                net_id=_to_int(_atom(net_expr, 1)),
                start_units=start_units,
                end_units=end_units,
                width_units=width_units,
            )
        )
    return result


def _parse_vias(root: SExpr, copper_layers: Sequence[str], layer_order: Dict[str, int]) -> List[Via]:
    result: List[Via] = []
    for item in root[1:]:
        if not isinstance(item, list) or _head(item) != "via":
            continue
        at_expr = _first_child(item, "at")
        net_expr = _first_child(item, "net")
        layers_expr = _first_child(item, "layers")
        if not (at_expr and net_expr and layers_expr):
            continue
        layer_names = _expand_via_layers(
            (token for token in layers_expr[1:] if isinstance(token, str)),
            copper_layers,
            layer_order,
        )
        at_units = (
            mm_to_pcb_units(_atom(at_expr, 1)),
            mm_to_pcb_units(_atom(at_expr, 2)),
        )
        size_units = mm_to_pcb_units(_atom(_first_child(item, "size") or [], 1))
        drill_units = mm_to_pcb_units(_atom(_first_child(item, "drill") or [], 1))
        result.append(
            Via(
                at=pcb_units_point_to_mm(at_units),
                size_mm=pcb_units_to_mm(size_units),
                drill_mm=pcb_units_to_mm(drill_units),
                layers=layer_names,
                net_id=_to_int(_atom(net_expr, 1)),
                at_units=at_units,
                size_units=size_units,
                drill_units=drill_units,
            )
        )
    return result


def _parse_zones(root: SExpr, nets: Dict[int, str], copper_layers: Sequence[str]) -> List[Zone]:
    zones: List[Zone] = []
    for item in root[1:]:
        if not isinstance(item, list) or _head(item) != "zone":
            continue
        layer_expr = _first_child(item, "layer")
        layers_expr = _first_child(item, "layers")
        layer_tokens: List[str] = []
        if layer_expr and len(layer_expr) >= 2 and isinstance(layer_expr[1], str):
            layer_tokens.append(layer_expr[1])
        if layers_expr:
            layer_tokens.extend(token for token in layers_expr[1:] if isinstance(token, str))
        zone_layers = _expand_copper_layers(layer_tokens, copper_layers)
        polygons_units: List[IntPolygon] = []
        for filled_expr in _children(item, "filled_polygon"):
            points_units = _parse_pts_units(filled_expr)
            if len(points_units) >= 3:
                polygons_units.append(points_units)
        if not polygons_units:
            polygon_expr = _first_child(item, "polygon")
            points_units = _parse_pts_units(polygon_expr or [])
            if len(points_units) >= 3:
                polygons_units.append(points_units)
        if not zone_layers or not polygons_units:
            continue
        net_id = _to_int(_atom(_first_child(item, "net") or [], 1), 0)
        zones.append(
            Zone(
                layers=zone_layers,
                net_id=net_id,
                net_name=nets.get(net_id, ""),
                polygons=[pcb_units_polygon_to_mm(polygon) for polygon in polygons_units],
                polygons_units=polygons_units,
            )
        )
    return zones


def _append_bbox_point(xs: List[float], ys: List[float], point_expr: Optional[SExpr]) -> None:
    if point_expr:
        xs.append(float(_decimal_from_value(_atom(point_expr, 1, "0") or "0")))
        ys.append(float(_decimal_from_value(_atom(point_expr, 2, "0") or "0")))


def _parse_edge_cut_bbox(root: SExpr) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for item in root[1:]:
        if not isinstance(item, list):
            continue
        layer_expr = _first_child(item, "layer")
        if _atom(layer_expr or [], 1) != "Edge.Cuts":
            continue
        head = _head(item)
        if head in {"gr_line", "gr_rect", "gr_arc"}:
            _append_bbox_point(xs, ys, _first_child(item, "start"))
            _append_bbox_point(xs, ys, _first_child(item, "mid"))
            _append_bbox_point(xs, ys, _first_child(item, "end"))
            center_expr = _first_child(item, "center")
            _append_bbox_point(xs, ys, center_expr)
            end_expr = _first_child(item, "end")
            if center_expr and end_expr:
                center_x = float(_decimal_from_value(_atom(center_expr, 1, "0") or "0"))
                center_y = float(_decimal_from_value(_atom(center_expr, 2, "0") or "0"))
                radius = math.dist(
                    (center_x, center_y),
                    (
                        float(_decimal_from_value(_atom(end_expr, 1, "0") or "0")),
                        float(_decimal_from_value(_atom(end_expr, 2, "0") or "0")),
                    ),
                )
                xs.extend([center_x - radius, center_x + radius])
                ys.extend([center_y - radius, center_y + radius])
        elif head == "gr_circle":
            center_expr = _first_child(item, "center")
            end_expr = _first_child(item, "end")
            _append_bbox_point(xs, ys, center_expr)
            _append_bbox_point(xs, ys, end_expr)
            if center_expr and end_expr:
                center_x = float(_decimal_from_value(_atom(center_expr, 1, "0") or "0"))
                center_y = float(_decimal_from_value(_atom(center_expr, 2, "0") or "0"))
                radius = math.dist(
                    (center_x, center_y),
                    (
                        float(_decimal_from_value(_atom(end_expr, 1, "0") or "0")),
                        float(_decimal_from_value(_atom(end_expr, 2, "0") or "0")),
                    ),
                )
                xs.extend([center_x - radius, center_x + radius])
                ys.extend([center_y - radius, center_y + radius])
        elif head == "gr_poly":
            for x_mm, y_mm in pcb_units_polygon_to_mm(_parse_pts_units(item)):
                xs.append(x_mm)
                ys.append(y_mm)
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _fallback_bbox_from_geometry_units(
    segments: Sequence[Segment],
    vias: Sequence[Via],
    pads: Sequence[Pad],
    zones: Sequence[Zone],
) -> Tuple[int, int, int, int]:
    xs: List[int] = []
    ys: List[int] = []
    for segment in segments:
        radius = segment.width_units // 2
        xs.extend([segment.start_units[0] - radius, segment.start_units[0] + radius, segment.end_units[0] - radius, segment.end_units[0] + radius])
        ys.extend([segment.start_units[1] - radius, segment.start_units[1] + radius, segment.end_units[1] - radius, segment.end_units[1] + radius])
    for via in vias:
        radius = via.size_units // 2
        xs.extend([via.at_units[0] - radius, via.at_units[0] + radius])
        ys.extend([via.at_units[1] - radius, via.at_units[1] + radius])
    for pad in pads:
        radius = max(pad.size_units) // 2
        xs.extend([pad.x_units - radius, pad.x_units + radius])
        ys.extend([pad.y_units - radius, pad.y_units + radius])
    for zone in zones:
        for polygon in zone.polygons_units:
            for x_units, y_units in polygon:
                xs.append(x_units)
                ys.append(y_units)
    if not xs or not ys:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def parse_kicad_board(path: str | Path) -> BoardData:
    board_path = Path(path)
    text = board_path.read_text(encoding="utf-8")
    root = parse_sexpr(text)
    if _head(root) != "kicad_pcb":
        raise ValueError(f"{board_path} 不是合法的 kicad_pcb 文件")

    layers, copper_layers, layer_order, copper_layer_map = _parse_layers(root)
    nets = _parse_nets(root)
    rules = _parse_rules(root)
    segments = _parse_segments(root, copper_layers)
    vias = _parse_vias(root, copper_layers, layer_order)
    pads_by_net = _parse_pads(root, nets, copper_layers)
    zones = _parse_zones(root, nets, copper_layers)

    bbox_mm = _parse_edge_cut_bbox(root)
    if bbox_mm == (0.0, 0.0, 0.0, 0.0):
        bbox_units = _fallback_bbox_from_geometry_units(segments, vias, _all_pads(pads_by_net), zones)
        bbox_mm = (
            pcb_units_to_mm(bbox_units[0]),
            pcb_units_to_mm(bbox_units[1]),
            pcb_units_to_mm(bbox_units[2]),
            pcb_units_to_mm(bbox_units[3]),
        )
    else:
        bbox_units = tuple(mm_to_pcb_units(value) for value in bbox_mm)

    width_units = max(0, bbox_units[2] - bbox_units[0])
    height_units = max(0, bbox_units[3] - bbox_units[1])
    width_mm = pcb_units_to_mm(width_units)
    height_mm = pcb_units_to_mm(height_units)

    return BoardData(
        path=board_path,
        nets=nets,
        layers=layers,
        copper_layers=copper_layers,
        layer_order=layer_order,
        copper_layer_map=copper_layer_map,
        width_mm=width_mm,
        height_mm=height_mm,
        bbox_mm=bbox_mm,
        rules=rules,
        segments=segments,
        vias=vias,
        pads_by_net=pads_by_net,
        zones=zones,
        units_per_mm=PCB_UNITS_PER_MM,
        coordinate_unit_name=PCB_UNIT_NAME,
        width_units=width_units,
        height_units=height_units,
        bbox_units=bbox_units,
    )


def parse_kicad_pcb(path: str | Path) -> BoardData:
    return parse_kicad_board(path)
