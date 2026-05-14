#!/usr/bin/env python3
"""
Dataset-oriented converter between KiCad `.kicad_pcb` boards and the Allegro-like `txt` layout files

This script is intentionally tuned for the 253data corpus instead of trying to be
an arbitrary KiCad/Allegro converter. For the txt side it aims to stay close to
the existing Allegro-style layout structure. Difficult/static sections can be
carried over from a donor txt file when available.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union


Node = Union[str, List["Node"]]
Point = Tuple[int, int]

DBU_MM = 0.000254  # 0.01 mil in millimeters
KICAD_ARC_MAX_SWEEP_DEG = 3.0
KICAD_ARC_MAX_CHORD_DBU = 600
KICAD_ROUTE_ARC_SEGMENTS = 16
OUTLINE_ONLY_ORIGIN_X = 363386
OUTLINE_ONLY_ORIGIN_Y = 534646
OUTLINE_ONLY_SIZE_X = 100000
OUTLINE_ONLY_SIZE_Y = 100000
STANDARD_KICAD_USER_LAYERS = [
    (32, "B.Adhes", "user"),
    (33, "F.Adhes", "user"),
    (34, "B.Paste", "user"),
    (35, "F.Paste", "user"),
    (36, "B.SilkS", "user"),
    (37, "F.SilkS", "user"),
    (38, "B.Mask", "user"),
    (39, "F.Mask", "user"),
    (40, "Dwgs.User", "user"),
    (41, "Cmts.User", "user"),
    (42, "Eco1.User", "user"),
    (43, "Eco2.User", "user"),
    (44, "Edge.Cuts", "user"),
    (45, "Margin", "user"),
    (46, "B.CrtYd", "user"),
    (47, "F.CrtYd", "user"),
    (48, "B.Fab", "user"),
    (49, "F.Fab", "user"),
]
PSEUDO_DONOR_TEMPLATE = Path("json/253data/0203qiyunf_30/441Pin_08BGA_6L_S_01311030.txt")
BGA_DATASET_KICAD_RE = re.compile(
    r"^(?:output_)?bga\.bga_"
    r"(?P<pin>\d+)pin_"
    r"(?P<row>\d+)x(?P<col>\d+)_"
    r"(?P<pitch>[0-9p]+)pitch_"
    r"(?P<dia>[0-9p]+)dia_"
    r"(?P<tw>[0-9p]+)tw_"
    r"(?P<tc>[0-9p]+)tc_"
    r"(?P<via>[0-9p]+)via_"
    r"(?P<drill>[0-9p]+)drill_"
    r"(?P<ring>\d+)ring_"
    r"(?P<layer>\d+)layer_"
    r"(?P<obs>[0-9p]+)obs_"
    r"(?P<drop>[0-9p]+)drop_"
    r"(?P<netorder>in2out|out2in)_"
    r"(?P<tp>l_global|l_quadrant|t_global|t_quadrant)"
    r"(?:_seed(?P<seed>noseed|\d+))?"
    r"(?P<drvpost>_drvpost)?$"
)
PSEUDO_DONOR_MINIMAL_LAYERS = [
    ("System/Guidline_Top", "false", "1"),
    ("System/Guidline_Bottom", "false", "1"),
    ("System/Guidline_All", "false", "1"),
    ("Layout/Silkscreen_Top", "false", "17"),
    ("Layout/Silkscreen_Bottom", "false", "11"),
    ("Layout/Resist_Top", "false", "13"),
    ("Layout/Resist_Bottom", "false", "13"),
    ("Layout/Layout_Outline", "false", "1"),
    ("Layout/Panel_Outline", "true", "30"),
    ("Cell/Silkscreen_Top", "false", "24"),
    ("Cell/Silkscreen_Bottom", "false", "12"),
    ("Cell/Resist_Top", "false", "13"),
    ("Cell/Resist_Bottom", "false", "13"),
    ("Cell/Stencil_Top", "false", "17"),
    ("Cell/Stencil_Bottom", "false", "17"),
    ("Part RefDes/Silkscreen_Top", "false", "24"),
    ("Part RefDes/Silkscreen_Bottom", "false", "12"),
]
INNER_LAYER_COLORS = ["16", "14", "18", "20", "24", "26", "28", "31", "33", "35", "37", "39", "41", "43", "45", "47"]
PSEUDO_DONOR_DYNAMIC_LAYER_PREFIXES = (
    "Conductor/",
    "Route Area/",
    "Slot/",
    "Boundary/",
    "Pin/",
    "Via/",
    "Split/",
    "Rule Area/",
    "Inhibit Route/",
    "Inhibit Via/",
    "Drc/",
    "Planning/",
)
PSEUDO_DONOR_SAFE_CONSTRAINT_SECTIONS = (
    "dicts",
    "design",
    "physical",
    "spacing",
    "samenet",
    "layout",
    "netclasses",
    "netgroups",
    "diffs",
    "enets",
)
PSEUDO_DONOR_EMPTY_CONSTRAINT_SECTIONS = (
    "pins",
    "pinpairs",
    "regions",
    "matchgroups",
    "clsclses",
    "rgnclses",
    "rgnclsclses",
)
PSEUDO_DONOR_CONSTRAINT_ORDER = (
    "dicts",
    "design",
    "physical",
    "spacing",
    "samenet",
    "layout",
    "netclasses",
    "netgroups",
    "diffs",
    "enets",
    "nets",
    "pins",
    "pinpairs",
    "regions",
    "matchgroups",
    "clsclses",
    "rgnclses",
    "rgnclsclses",
)
PSEUDO_DONOR_CANONICAL_STACKUPS = {
    6: ("Top", "Gnd02", "Sig03", "Sig04", "Power05", "Bottom"),
    4: ("Top", "Gnd02", "Power03", "Bottom"),
}
PSEUDO_DONOR_SAFE_RULE_CORE_TEMPLATE = Path("json/253data/txt/380Pin_08BGA_6L_SD_01261435.txt")


@dataclass
class RawSection:
    name: str
    text: str


@dataclass
class LayerSpec:
    txt_name: str
    kicad_name: str
    kind: str
    negative: bool = False
    thickness_mil: float = 1.2


@dataclass
class PathStep:
    kind: str  # line / arc
    x: int
    y: int
    width: int = 0
    cx: Optional[int] = None
    cy: Optional[int] = None
    rotate: Optional[str] = None


@dataclass
class OutlinePath:
    layer: str
    steps: List[PathStep]


@dataclass
class WirePath:
    net: str
    layer: str
    steps: List[PathStep]


@dataclass
class SurfaceZone:
    net: str
    layer: str
    boundary: List[PathStep]
    holes: List[List[PathStep]] = field(default_factory=list)
    source_kind: str = "conductive"


@dataclass
class PadGeometry:
    shape: str
    size_x: int
    size_y: int
    drill: int = 0
    pad_type: str = "smd"
    side: str = "Top"
    connected_layers: Tuple[str, ...] = ()


@dataclass
class PinInstance:
    number: str
    x: int
    y: int
    rotation_deg: float
    net: str
    geometry: PadGeometry
    padstack_name: str = ""
    is_testpoint: bool = False


@dataclass
class ComponentInstance:
    ref: str
    part: str
    footprint: str
    x: int
    y: int
    rotation_deg: float
    mirrored: bool
    pins: List[PinInstance]


@dataclass
class ViaInstance:
    net: str
    x: int
    y: int
    rotation_deg: float
    mirrored: bool
    size: int
    drill: int
    layers: Tuple[str, ...]
    padstack_name: str = ""


@dataclass
class BoardModel:
    stem: str
    lowerleft_x: int = 0
    lowerleft_y: int = 0
    width: int = 0
    height: int = 0
    layers: List[LayerSpec] = field(default_factory=list)
    components: List[ComponentInstance] = field(default_factory=list)
    vias: List[ViaInstance] = field(default_factory=list)
    wires: List[WirePath] = field(default_factory=list)
    zones: List[SurfaceZone] = field(default_factory=list)
    outlines: List[OutlinePath] = field(default_factory=list)
    net_order: List[str] = field(default_factory=list)
    connectivity_nets: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    constraint_props: Dict[str, Dict[str, str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    default_trace_width: int = 0
    default_trace_clearance: int = 0
    default_via_size: int = 0
    default_via_drill: int = 0

    def all_net_names(self) -> List[str]:
        ordered: List[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            if not name or name.lower() in {"none", "\"\"", '""'} or name in seen:
                return
            seen.add(name)
            ordered.append(name)

        for name in self.net_order:
            add(name)
        for name in self.connectivity_nets:
            add(name)
        for via in self.vias:
            add(via.net)
        for wire in self.wires:
            add(wire.net)
        for zone in self.zones:
            add(zone.net)
        for comp in self.components:
            for pin in comp.pins:
                add(pin.net)
        return ordered


@dataclass
class DonorViaChoice:
    padstack_name: str
    layers: Tuple[str, ...]
    size: int
    drill: int


def dbu_to_mm(value: int) -> float:
    return value * DBU_MM


def mm_to_dbu(value: float) -> int:
    return int(round(value / DBU_MM))


def fmt_mm(value: int) -> str:
    text = f"{dbu_to_mm(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_float(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def outline_only_translate(point: Point) -> Point:
    return (point[0] + OUTLINE_ONLY_ORIGIN_X, point[1] + OUTLINE_ONLY_ORIGIN_Y)


def outline_only_untranslate(point: Point) -> Point:
    return (point[0] - OUTLINE_ONLY_ORIGIN_X, point[1] - OUTLINE_ONLY_ORIGIN_Y)


def dbu_to_mil(value: int) -> float:
    return value / 100.0


def mil_to_dbu(value: float) -> int:
    return int(round(value * 100.0))


def parse_length_text_to_mil(text: str, default: float = 0.0) -> float:
    raw = (text or "").strip()
    if not raw:
        return default
    lowered = raw.lower()
    try:
        if lowered.endswith("mil"):
            return float(lowered[:-3].strip())
        if lowered.endswith(" mm"):
            return float(lowered[:-3].strip()) / 0.0254
        if lowered.endswith("mm"):
            return float(lowered[:-2].strip()) / 0.0254
        if lowered.endswith(" m"):
            return float(lowered[:-2].strip()) / 0.0254
        if lowered.endswith("m"):
            return float(lowered[:-1].strip()) / 0.0254
        return float(lowered)
    except ValueError:
        return default


def dataset_name_token_to_float(token: str) -> float:
    return float(token.replace("p", "."))


def parse_bga_dataset_kicad_stem(stem: str) -> Optional[Dict[str, Any]]:
    match = BGA_DATASET_KICAD_RE.fullmatch(stem)
    if not match:
        return None
    return {
        "pin": int(match.group("pin")),
        "row": int(match.group("row")),
        "col": int(match.group("col")),
        "pitch_mm": dataset_name_token_to_float(match.group("pitch")),
        "ball_pad_dia_mil": dataset_name_token_to_float(match.group("dia")),
        "trace_width_mil": dataset_name_token_to_float(match.group("tw")),
        "trace_clearance_mil": dataset_name_token_to_float(match.group("tc")),
        "via_size_mil": dataset_name_token_to_float(match.group("via")),
        "via_drill_mil": dataset_name_token_to_float(match.group("drill")),
        "tp_ring_steps": int(match.group("ring")),
        "layer_num": int(match.group("layer")),
        "obstacle_net_ratio": dataset_name_token_to_float(match.group("obs")),
        "obstacle_drop_prob": dataset_name_token_to_float(match.group("drop")),
        "net_order": match.group("netorder"),
        "tp_strategy": match.group("tp"),
        "seed": match.group("seed") or "",
        "drvpost": bool(match.group("drvpost")),
    }


def clean_token(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def quote_atom(value: str) -> str:
    if value in {"nil", "t"}:
        return value
    if re.fullmatch(r"[A-Za-z0-9_./:+*\\-]+", value or ""):
        return value
    return '"' + value.replace("\\", r"\\").replace('"', r"\"") + '"'


def sexpr_tokenize(text: str) -> Iterator[str]:
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            yield ch
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: List[str] = []
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            yield '"' + "".join(buf) + '"'
            continue
        j = i
        while j < n and (not text[j].isspace()) and text[j] not in "()":
            j += 1
        yield text[i:j]
        i = j


class TokenCursor:
    def __init__(self, tokens: Iterable[str]) -> None:
        self._tokens = iter(tokens)
        self._buffer: Optional[str] = None

    def peek(self) -> Optional[str]:
        if self._buffer is None:
            self._buffer = next(self._tokens, None)
        return self._buffer

    def pop(self) -> Optional[str]:
        token = self.peek()
        self._buffer = None
        return token


def parse_sexpr(text: str) -> Node:
    cursor = TokenCursor(sexpr_tokenize(text))

    def parse_value() -> Node:
        token = cursor.pop()
        if token is None:
            raise ValueError("unexpected end of S-expression")
        if token == "(":
            items: List[Node] = []
            while True:
                nxt = cursor.peek()
                if nxt is None:
                    raise ValueError("unterminated S-expression")
                if nxt == ")":
                    cursor.pop()
                    break
                items.append(parse_value())
            return items
        if token == ")":
            raise ValueError("unexpected ')'")
        return clean_token(token)

    node = parse_value()
    if cursor.peek() is not None:
        raise ValueError("trailing tokens after S-expression")
    return node


def emit_sexpr(node: Node, indent: int = 0) -> str:
    if isinstance(node, str):
        return quote_atom(node)
    if not node:
        return "()"

    flat = all(isinstance(item, str) for item in node)
    if flat:
        return "(" + " ".join(emit_sexpr(item, indent + 1) for item in node) + ")"

    head = emit_sexpr(node[0], indent + 1)
    parts = ["(" + head]
    child_indent = " " * (indent + 4)
    for child in node[1:]:
        if isinstance(child, list):
            parts.append("\n" + child_indent + emit_sexpr(child, indent + 4))
        else:
            parts.append(" " + emit_sexpr(child, indent + 1))
    parts.append(")")
    return "".join(parts)


def node_head(node: Node) -> Optional[str]:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return None


def child_nodes(node: Node, head: Optional[str] = None) -> Iterator[List[Node]]:
    if not isinstance(node, list):
        return
    for child in node[1:]:
        if isinstance(child, list):
            if head is None or node_head(child) == head:
                yield child


def child_text(node: Node, head: str, default: str = "") -> str:
    for child in child_nodes(node, head):
        if len(child) >= 2 and isinstance(child[1], str):
            return child[1]
    return default


def child_number(node: Node, head: str, default: float = 0.0) -> float:
    text = child_text(node, head, "")
    if text == "":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def child_bool_text(node: Node, head: str, default: bool = False) -> bool:
    value = child_text(node, head, "true" if default else "false").lower()
    return value in {"true", "t", "yes", "1"}


def extract_root_children(text: str, expected_root: str) -> List[RawSection]:
    idx = text.find("(")
    if idx < 0:
        raise ValueError("root S-expression not found")
    while idx < len(text) and text[idx] != "(":
        idx += 1
    if idx >= len(text):
        raise ValueError("root S-expression not found")
    if text[idx + 1 :].lstrip().split(None, 1)[0].rstrip(")") != expected_root:
        root = text[idx + 1 :].lstrip().split(None, 1)[0].rstrip(")")
        raise ValueError(f"expected root '{expected_root}', found '{root}'")

    depth = 0
    in_string = False
    escape = False
    child_start: Optional[int] = None
    children: List[RawSection] = []
    for pos, ch in enumerate(text[idx:], start=idx):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "(":
            depth += 1
            if depth == 2:
                child_start = pos
        elif ch == ")":
            if depth == 2 and child_start is not None:
                child_text_raw = text[child_start : pos + 1]
                name = child_text_raw[1:].lstrip().split(None, 1)[0].rstrip(")")
                children.append(RawSection(name=name, text=child_text_raw))
                child_start = None
            depth -= 1
            if depth == 0:
                break
    return children


def rebuild_root(root_name: str, children: Sequence[str]) -> str:
    body = "\n".join(child.strip() for child in children if child.strip())
    return f"({root_name}\n{body}\n)\n"


def parse_path_steps(path_node: List[Node]) -> List[PathStep]:
    steps: List[PathStep] = []
    for child in child_nodes(path_node):
        head = node_head(child)
        if head == "lineseg":
            pt = next(child_nodes(child, "pt"), None)
            if pt is None or len(pt) < 3:
                continue
            steps.append(
                PathStep(
                    kind="line",
                    x=int(round(float(pt[1]))),
                    y=int(round(float(pt[2]))),
                    width=int(round(child_number(child, "w", 0))),
                )
            )
        elif head == "arcseg":
            pt = next(child_nodes(child, "pt"), None)
            xy = next(child_nodes(child, "xy"), None)
            if pt is None or xy is None or len(pt) < 3 or len(xy) < 3:
                continue
            steps.append(
                PathStep(
                    kind="arc",
                    x=int(round(float(pt[1]))),
                    y=int(round(float(pt[2]))),
                    width=int(round(child_number(child, "w", 0))),
                    cx=int(round(float(xy[1]))),
                    cy=int(round(float(xy[2]))),
                    rotate=child_text(child, "rotate", "CCW"),
                )
            )
    return steps


def arc_segment_count(
    start: Point,
    arc: PathStep,
    *,
    points_per_circle: Optional[int] = None,
    max_sweep_deg: Optional[float] = None,
    max_chord_dbu: Optional[int] = None,
) -> int:
    if arc.kind != "arc" or arc.cx is None or arc.cy is None:
        return 1
    sx, sy = start
    ex, ey = arc.x, arc.y
    cx, cy = arc.cx, arc.cy
    radius = math.hypot(sx - cx, sy - cy)
    if radius <= 0:
        return 1
    start_ang = math.atan2(sy - cy, sx - cx)
    end_ang = math.atan2(ey - cy, ex - cx)
    delta = end_ang - start_ang
    rotate = (arc.rotate or "CCW").upper()
    if rotate == "CCW":
        while delta <= 0:
            delta += math.tau
    else:
        while delta >= 0:
            delta -= math.tau
    sweep = abs(delta)
    segments = 1
    if points_per_circle:
        segments = max(segments, int(math.ceil(sweep / (math.tau / points_per_circle))))
    if max_sweep_deg and max_sweep_deg > 0:
        segments = max(segments, int(math.ceil(sweep / math.radians(max_sweep_deg))))
    if max_chord_dbu and max_chord_dbu > 0:
        if radius <= max_chord_dbu / 2:
            theta_max = math.pi
        else:
            theta_max = 2 * math.asin(min(1.0, max_chord_dbu / (2 * radius)))
        if theta_max > 0:
            segments = max(segments, int(math.ceil(sweep / theta_max)))
    return max(1, segments)


def arc_points(
    start: Point,
    arc: PathStep,
    points_per_circle: Optional[int] = 32,
    *,
    max_sweep_deg: Optional[float] = None,
    max_chord_dbu: Optional[int] = None,
) -> List[Point]:
    if arc.kind != "arc" or arc.cx is None or arc.cy is None:
        return [(arc.x, arc.y)]
    sx, sy = start
    ex, ey = arc.x, arc.y
    cx, cy = arc.cx, arc.cy
    radius = math.hypot(sx - cx, sy - cy)
    if radius <= 0:
        return [(ex, ey)]
    start_ang = math.atan2(sy - cy, sx - cx)
    end_ang = math.atan2(ey - cy, ex - cx)
    delta = end_ang - start_ang
    rotate = (arc.rotate or "CCW").upper()
    if rotate == "CCW":
        while delta <= 0:
            delta += math.tau
    else:
        while delta >= 0:
            delta -= math.tau
    segments = arc_segment_count(
        start,
        arc,
        points_per_circle=points_per_circle,
        max_sweep_deg=max_sweep_deg,
        max_chord_dbu=max_chord_dbu,
    )
    pts: List[Point] = []
    for i in range(1, segments + 1):
        ang = start_ang + delta * (i / segments)
        pts.append((int(round(cx + radius * math.cos(ang))), int(round(cy + radius * math.sin(ang)))))
    return pts


def flatten_steps(
    steps: Sequence[PathStep],
    *,
    points_per_circle: Optional[int] = 32,
    max_sweep_deg: Optional[float] = None,
    max_chord_dbu: Optional[int] = None,
) -> List[Point]:
    if not steps:
        return []
    result: List[Point] = []
    current: Optional[Point] = None
    for step in steps:
        if step.kind == "line":
            pt = (step.x, step.y)
            if current is None or result[-1:] != [pt]:
                result.append(pt)
            current = pt
            continue
        if current is None:
            current = (step.x, step.y)
            result.append(current)
            continue
        pts = arc_points(
            current,
            step,
            points_per_circle=points_per_circle,
            max_sweep_deg=max_sweep_deg,
            max_chord_dbu=max_chord_dbu,
        )
        result.extend(pts)
        current = pts[-1] if pts else current
    return result


def flatten_steps_for_kicad(steps: Sequence[PathStep]) -> List[Point]:
    return flatten_steps(
        steps,
        points_per_circle=None,
        max_sweep_deg=KICAD_ARC_MAX_SWEEP_DEG,
        max_chord_dbu=KICAD_ARC_MAX_CHORD_DBU,
    )


def arc_points_fixed_segments(start: Point, arc: PathStep, segments: int) -> List[Point]:
    if arc.kind != "arc" or arc.cx is None or arc.cy is None or segments <= 0:
        return [(arc.x, arc.y)]
    sx, sy = start
    ex, ey = arc.x, arc.y
    cx, cy = arc.cx, arc.cy
    radius = math.hypot(sx - cx, sy - cy)
    if radius <= 0:
        return [(ex, ey)]
    start_ang = math.atan2(sy - cy, sx - cx)
    end_ang = math.atan2(ey - cy, ex - cx)
    delta = end_ang - start_ang
    rotate = (arc.rotate or "CCW").upper()
    if rotate == "CCW":
        while delta <= 0:
            delta += math.tau
    else:
        while delta >= 0:
            delta -= math.tau
    points: List[Point] = []
    for idx in range(1, segments + 1):
        frac = idx / segments
        ang = start_ang + delta * frac
        points.append((int(round(cx + math.cos(ang) * radius)), int(round(cy + math.sin(ang) * radius))))
    return points


def flatten_route_steps_for_kicad(steps: Sequence[PathStep]) -> List[Point]:
    if not steps:
        return []
    result: List[Point] = []
    current: Optional[Point] = None
    for step in steps:
        if step.kind == "line":
            pt = (step.x, step.y)
            if current is None or result[-1:] != [pt]:
                result.append(pt)
            current = pt
            continue
        if current is None:
            current = (step.x, step.y)
            result.append(current)
            continue
        # Native outline_only boards in this corpus consistently materialize each route arc
        # as a 16-segment chain regardless of sweep.
        pts = arc_points_fixed_segments(current, step, KICAD_ROUTE_ARC_SEGMENTS)
        result.extend(pts)
        current = pts[-1] if pts else current
    return result


def wire_segment_width(wire: WirePath) -> int:
    widths = [step_width(step, 0) for step in wire.steps if step_width(step, 0) > 0]
    return widths[0] if widths else 0


def split_wire_points(wire: WirePath) -> List[Point]:
    return flatten_steps(wire.steps, points_per_circle=32)


def copper_layer_names(board: BoardModel) -> Tuple[str, ...]:
    return tuple(layer.txt_name for layer in board.layers)


def pin_route_layers(board: BoardModel, comp: ComponentInstance, pin: PinInstance) -> Tuple[str, ...]:
    if pin.geometry.connected_layers:
        return tuple(layer for layer in pin.geometry.connected_layers if layer)
    if pin.geometry.pad_type != "smd":
        return copper_layer_names(board)
    return ("Bottom",) if comp.mirrored else ("Top",)


def wire_protected_points(board: BoardModel) -> set[Tuple[str, str, Point]]:
    protected: set[Tuple[str, str, Point]] = set()
    for comp in board.components:
        for pin in comp.pins:
            if not pin.net:
                continue
            pt = (pin.x, pin.y)
            for layer in pin_route_layers(board, comp, pin):
                protected.add((pin.net, conductor_layer_txt(layer), pt))
                protected.add((pin.net, layer, pt))
    for via in board.vias:
        if not via.net:
            continue
        pt = (via.x, via.y)
        for layer in via.layers:
            protected.add((via.net, conductor_layer_txt(layer), pt))
            protected.add((via.net, layer, pt))
    return protected


def merge_keyed_line_chains(
    keyed_edges: Dict[Tuple[str, str, int], List[Tuple[Point, Point]]],
    *,
    protected_points: set[Tuple[str, str, Point]],
) -> List[WirePath]:
    merged: List[WirePath] = []
    for key in sorted(keyed_edges.keys(), key=lambda item: (item[0], item[1], item[2])):
        net, layer, width = key
        edges = keyed_edges[key]
        adjacency: Dict[Point, List[int]] = {}
        for idx, (a, b) in enumerate(edges):
            adjacency.setdefault(a, []).append(idx)
            adjacency.setdefault(b, []).append(idx)
        visited: set[int] = set()

        def degree(point: Point) -> int:
            return len(adjacency.get(point, []))

        def is_break(point: Point) -> bool:
            return degree(point) != 2 or (net, layer, point) in protected_points

        def build_chain(edge_idx: int, start: Point, closed_loop: bool = False) -> List[Point]:
            path: List[Point] = [start]
            current = start
            current_edge = edge_idx
            while True:
                visited.add(current_edge)
                a, b = edges[current_edge]
                nxt = b if current == a else a
                path.append(nxt)
                current = nxt
                if not closed_loop and is_break(current):
                    break
                candidates = [idx for idx in adjacency[current] if idx not in visited]
                if not candidates:
                    break
                if closed_loop and current == start:
                    break
                if len(candidates) != 1:
                    break
                current_edge = candidates[0]
                if closed_loop and current in path[:-1]:
                    break
            return path

        for point in sorted(adjacency.keys()):
            if not is_break(point):
                continue
            for edge_idx in sorted(adjacency[point]):
                if edge_idx in visited:
                    continue
                points = build_chain(edge_idx, point, closed_loop=False)
                steps = [PathStep("line", x, y, width) for x, y in points]
                merged.append(WirePath(net=net, layer=layer, steps=steps))

        for edge_idx, (a, b) in enumerate(edges):
            if edge_idx in visited:
                continue
            start = min(a, b)
            points = build_chain(edge_idx, start, closed_loop=True)
            if points[-1] != points[0]:
                points.append(points[0])
            steps = [PathStep("line", x, y, width) for x, y in points]
            merged.append(WirePath(net=net, layer=layer, steps=steps))
    return merged


def merge_board_wires_for_txt(board: BoardModel) -> List[WirePath]:
    keyed_edges: Dict[Tuple[str, str, int], List[Tuple[Point, Point]]] = {}
    for wire in board.wires:
        points = split_wire_points(wire)
        if len(points) < 2:
            continue
        width = wire_segment_width(wire)
        key = (wire.net, wire.layer, width)
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            keyed_edges.setdefault(key, []).append((a, b))
    return merge_keyed_line_chains(keyed_edges, protected_points=wire_protected_points(board))


def merge_outline_paths(outlines: Sequence[OutlinePath]) -> List[OutlinePath]:
    keyed_edges: Dict[Tuple[str, str, int], List[Tuple[Point, Point]]] = {}
    for outline in outlines:
        points = flatten_steps(outline.steps, points_per_circle=32)
        if len(points) < 2:
            continue
        key = ("", outline.layer, 0)
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            keyed_edges.setdefault(key, []).append((a, b))
    merged = merge_keyed_line_chains(keyed_edges, protected_points=set())
    return [OutlinePath(layer=wire.layer, steps=wire.steps) for wire in merged]


def step_width(step: PathStep, default: int = 0) -> int:
    return step.width or default


def layer_txt_to_kicad(name: str) -> str:
    base = name
    if "/" in base:
        base = base.split("/")[-1]
    if base in {"Top", "Bottom"}:
        return base
    upper = base.upper()
    if upper.startswith("GND"):
        return "GND" + base[3:]
    if upper.startswith("SIG"):
        return "SIG" + base[3:]
    if upper.startswith("ART"):
        return "ART" + base[3:]
    if upper.startswith("POWER"):
        return "POWER" + base[5:]
    if upper.startswith("CONDUCTOR"):
        return base
    return upper


def layer_kicad_to_txt(name: str) -> str:
    if name in {"Top", "Bottom"}:
        return name
    upper = name.upper()
    if upper.startswith("GND"):
        return "Gnd" + name[3:]
    if upper.startswith("SIG"):
        return "Sig" + name[3:]
    if upper.startswith("ART"):
        return "Art" + name[3:]
    if upper.startswith("POWER"):
        return "Power" + name[5:]
    return name


def conductor_layer_txt(name: str) -> str:
    base = name
    return f"Conductor/{base}"


def guess_layer_kind(txt_name: str, ltype: str = "") -> str:
    if txt_name in {"Top", "Bottom"}:
        return "signal"
    if ltype.lower() == "plane":
        return "power"
    upper = txt_name.upper()
    if upper.startswith(("GND", "POWER")):
        return "power"
    return "signal"


def infer_bounds_from_points(points: Iterable[Tuple[int, int]]) -> Tuple[int, int, int, int]:
    pts = list(points)
    if not pts:
        return (0, 0, 0, 0)
    xs = [pt[0] for pt in pts]
    ys = [pt[1] for pt in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def path_points_for_compare(steps: Sequence[PathStep]) -> List[Point]:
    return flatten_steps(steps, points_per_circle=32)


def normalize_closed_points(points: Sequence[Point]) -> Tuple[Point, ...]:
    pts = list(points)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return ()
    variants: List[Tuple[Point, ...]] = []
    n = len(pts)
    for seq in (pts, list(reversed(pts))):
        for idx in range(n):
            rotated = tuple(seq[idx:] + seq[:idx])
            variants.append(rotated)
    return min(variants)


def normalized_boundary_key(steps: Sequence[PathStep]) -> Tuple[Point, ...]:
    return normalize_closed_points(path_points_for_compare(steps))


def path_bbox_area(steps: Sequence[PathStep]) -> int:
    min_x, min_y, max_x, max_y = infer_bounds_from_points(path_points_for_compare(steps))
    return max(0, max_x - min_x) * max(0, max_y - min_y)


def is_simple_rectilinear_surface(steps: Sequence[PathStep]) -> bool:
    points = path_points_for_compare(steps)
    if len(points) < 4:
        return False
    if points[0] != points[-1]:
        points.append(points[0])
    edges = list(zip(points, points[1:]))
    if len(edges) != 4:
        return False
    for (ax, ay), (bx, by) in edges:
        if ax != bx and ay != by:
            return False
    return True


def is_simple_surface_boundary(steps: Sequence[PathStep]) -> bool:
    if not steps or len(steps) > 4:
        return False
    if is_simple_rectilinear_surface(steps):
        return True
    if len(steps) == 2 and steps[0].kind == "line" and steps[1].kind == "arc":
        line = steps[0]
        arc = steps[1]
        return (
            arc.cx is not None
            and arc.cy is not None
            and line.x == arc.x
            and line.y == arc.y
        )
    return False


def iqr_upper_outlier_limit(values: Sequence[int]) -> Optional[float]:
    if len(values) < 4:
        return None
    ordered = sorted(values)

    def percentile(p: float) -> float:
        pos = (len(ordered) - 1) * p
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return float(ordered[lo])
        frac = pos - lo
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac

    q1 = percentile(0.25)
    q3 = percentile(0.75)
    iqr = q3 - q1
    return q3 + 1.5 * iqr


def top_level_surface_zone_layer(txt_layer: str) -> Optional[str]:
    mapping = {
        "Layout/Resist_Top": "Conductor/SOLDERMASK_TOP",
        "Layout/Resist_Bottom": "Conductor/SOLDERMASK_BOTTOM",
        "Cell/Stencil_Top": "Conductor/PASTEMASK_TOP",
        "Cell/Stencil_Bottom": "Conductor/PASTEMASK_BOTTOM",
    }
    return mapping.get(txt_layer)


def zone_layer_to_surface_txt_layer(zone_layer: str) -> Optional[str]:
    mapping = {
        "Conductor/SOLDERMASK_TOP": "Layout/Resist_Top",
        "Conductor/SOLDERMASK_BOTTOM": "Layout/Resist_Bottom",
        "Conductor/PASTEMASK_TOP": "Cell/Stencil_Top",
        "Conductor/PASTEMASK_BOTTOM": "Cell/Stencil_Bottom",
    }
    return mapping.get(zone_layer)


def rect_outline_from_bounds(bounds: Tuple[int, int, int, int]) -> List[OutlinePath]:
    min_x, min_y, max_x, max_y = bounds
    steps = [
        PathStep("line", min_x, min_y),
        PathStep("line", max_x, min_y),
        PathStep("line", max_x, max_y),
        PathStep("line", min_x, max_y),
        PathStep("line", min_x, min_y),
    ]
    return [OutlinePath(layer="Boundary/All", steps=steps)]


def looks_like_power_ground_net(name: str) -> bool:
    upper = name.upper()
    tokens = (
        "GND",
        "GROUND",
        "PGND",
        "AGND",
        "DGND",
        "POWER",
        "PWR",
        "VCC",
        "VDD",
        "VSS",
        "VBAT",
        "VIN",
        "VREF",
        "VCORE",
        "VTT",
        "+1V",
        "+2V",
        "+3V",
        "+5V",
        "+12V",
        "1V0",
        "1V1",
        "1V2",
        "1V5",
        "1V8",
        "2V5",
        "3V3",
        "5V",
    )
    if any(token in upper for token in tokens):
        return True
    if upper.startswith("+") and "CORE" in upper:
        return True
    return False


def parse_pad_name_geometry(name: str) -> Optional[Tuple[str, int, int]]:
    if not name or name == "None":
        return None
    m = re.fullmatch(r"C_([0-9.]+)", name)
    if m:
        dia = mil_to_dbu(float(m.group(1)))
        return ("circle", dia, dia)
    m = re.fullmatch(r"R_([0-9.]+)_([0-9.]+)", name)
    if m:
        return ("rectangle", mil_to_dbu(float(m.group(1))), mil_to_dbu(float(m.group(2))))
    m = re.fullmatch(r"OB_([0-9.]+)_([0-9.]+)", name)
    if m:
        return ("oblong", mil_to_dbu(float(m.group(1))), mil_to_dbu(float(m.group(2))))
    m = re.fullmatch(r"O_([0-9.]+)_([0-9.]+)", name)
    if m:
        return ("oblong", mil_to_dbu(float(m.group(1))), mil_to_dbu(float(m.group(2))))
    m = re.fullmatch(r"S_([0-9.]+)", name)
    if m:
        side = mil_to_dbu(float(m.group(1)))
        return ("rectangle", side, side)
    return None


@dataclass
class TxtPadstack:
    name: str
    psktype: str
    pskusage: str
    from_layer: str
    to_layer: str
    drill: int
    padsets: List[Tuple[str, str, str]]  # layer, ptype, name


def parse_txt_padstack_node(node: List[Node]) -> TxtPadstack:
    name = node[1] if len(node) >= 2 and isinstance(node[1], str) else "UNKNOWN"
    psktype = child_text(node, "psktype", "smd")
    pskusage = child_text(node, "pskusage", psktype)
    fromtos = next(child_nodes(node, "fromtos"), None)
    from_layer = "Top"
    to_layer = "Top"
    if fromtos is not None and len(fromtos) >= 3:
        from_layer = str(fromtos[1])
        to_layer = str(fromtos[2])
    drill = 0
    drill_node = next(child_nodes(node, "drill"), None)
    if drill_node is not None:
        hole_node = next(child_nodes(drill_node, "hole"), None)
        if hole_node is not None:
            hole_size = next(child_nodes(hole_node, "holesize"), None)
            if hole_size is not None and len(hole_size) >= 3:
                drill = int(round(max(float(hole_size[1]), float(hole_size[2]))))
    padsets: List[Tuple[str, str, str]] = []
    padsets_node = next(child_nodes(node, "padsets"), None)
    if padsets_node is not None:
        for padset in child_nodes(padsets_node, "padset"):
            padsets.append(
                (
                    child_text(padset, "layer", ""),
                    child_text(padset, "ptype", ""),
                    child_text(padset, "name", ""),
                )
            )
    return TxtPadstack(name, psktype, pskusage, from_layer, to_layer, drill, padsets)


def pick_pad_geometry(
    padstack: Optional[TxtPadstack],
    requested_layers: Sequence[str],
) -> PadGeometry:
    if padstack is None:
        return PadGeometry(shape="circle", size_x=mil_to_dbu(12), size_y=mil_to_dbu(12))

    candidates: List[str] = []
    for layer in requested_layers:
        for ps_layer, ptype, pad_name in padstack.padsets:
            if ptype == "connect" and ps_layer == layer and pad_name not in {"", "None"}:
                candidates.append(pad_name)
    if not candidates:
        for ps_layer, ptype, pad_name in padstack.padsets:
            if ptype == "connect" and ps_layer in {padstack.from_layer, padstack.to_layer, "Inner_Default"} and pad_name not in {"", "None"}:
                candidates.append(pad_name)
    if not candidates:
        for _ps_layer, ptype, pad_name in padstack.padsets:
            if ptype == "connect" and pad_name not in {"", "None"}:
                candidates.append(pad_name)

    geom = parse_pad_name_geometry(candidates[0]) if candidates else None
    if geom is None:
        if padstack.psktype in {"through", "thru", "via"} and padstack.drill > 0:
            size = int(round(padstack.drill * 1.8))
            return PadGeometry(
                shape="circle",
                size_x=size,
                size_y=size,
                drill=padstack.drill,
                pad_type="thru_hole",
                side="Through",
                connected_layers=tuple(requested_layers or ("Top", "Bottom")),
            )
        return PadGeometry(shape="circle", size_x=mil_to_dbu(12), size_y=mil_to_dbu(12))

    shape, sx, sy = geom
    side = "Top"
    if requested_layers:
        if "Bottom" in requested_layers and "Top" not in requested_layers:
            side = "Bottom"
        elif "Top" in requested_layers and "Bottom" in requested_layers:
            side = "Through"
    elif padstack.from_layer == "Bottom" and padstack.to_layer == "Bottom":
        side = "Bottom"
    elif padstack.from_layer != padstack.to_layer:
        side = "Through"

    pad_type = "smd"
    if padstack.psktype in {"through", "thru", "pin"} or side == "Through":
        pad_type = "thru_hole"

    return PadGeometry(
        shape=shape,
        size_x=sx,
        size_y=sy,
        drill=padstack.drill,
        pad_type=pad_type,
        side=side,
        connected_layers=tuple(requested_layers or (padstack.from_layer, padstack.to_layer)),
    )


def pad_shape_to_kicad(shape: str) -> str:
    return {
        "circle": "circle",
        "rectangle": "rect",
        "oblong": "oval",
        "oval": "oval",
    }.get(shape, "rect")


def kicad_shape_to_pad(shape: str) -> str:
    return {
        "circle": "circle",
        "rect": "rectangle",
        "roundrect": "rectangle",
        "oval": "oblong",
        "trapezoid": "rectangle",
    }.get(shape, "rectangle")


def parse_txt_board(path: Path) -> BoardModel:
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = extract_root_children(text, "layout")
    section_map: Dict[str, RawSection] = {sec.name: sec for sec in sections}
    board = BoardModel(stem=path.stem)

    params_node = parse_sexpr(section_map["parameters"].text) if "parameters" in section_map else None
    if isinstance(params_node, list):
        board.lowerleft_x = int(round(child_number(params_node, "lowerleft", 0.0)))
        lowerleft = next(child_nodes(params_node, "lowerleft"), None)
        if lowerleft is not None and len(lowerleft) >= 3:
            board.lowerleft_x = int(round(float(lowerleft[1])))
            board.lowerleft_y = int(round(float(lowerleft[2])))
        size = next(child_nodes(params_node, "size"), None)
        if size is not None and len(size) >= 3:
            board.width = int(round(float(size[1])))
            board.height = int(round(float(size[2])))

    if "layermanager" in section_map:
        lm = parse_sexpr(section_map["layermanager"].text)
        stackup = next(child_nodes(lm, "stackup"), None)
        if stackup is not None:
            for layer_node in child_nodes(stackup, "layer"):
                if len(layer_node) < 2 or not isinstance(layer_node[1], str):
                    continue
                txt_name = layer_node[1]
                if not txt_name:
                    continue
                ltype = child_text(layer_node, "ltype", "")
                board.layers.append(
                    LayerSpec(
                        txt_name=txt_name,
                        kicad_name=layer_txt_to_kicad(txt_name),
                        kind=guess_layer_kind(txt_name, ltype),
                        negative=child_bool_text(layer_node, "isnegtive", False),
                        thickness_mil=parse_length_text_to_mil(child_text(layer_node, "thickness", "1.2mil"), 1.2),
                    )
                )

    library_node = parse_sexpr(section_map["library"].text) if "library" in section_map else None
    padstacks: Dict[str, TxtPadstack] = {}
    if isinstance(library_node, list):
        padstacks_node = next(child_nodes(library_node, "padstacks"), None)
        if padstacks_node is not None:
            for pstk in child_nodes(padstacks_node, "padstack"):
                parsed = parse_txt_padstack_node(pstk)
                padstacks[parsed.name] = parsed

    nets_node = parse_sexpr(section_map["nets"].text) if "nets" in section_map else None
    if isinstance(nets_node, list):
        for net_node in child_nodes(nets_node, "net"):
            if len(net_node) < 2 or not isinstance(net_node[1], str):
                continue
            name = net_node[1]
            conns: List[Tuple[str, str]] = []
            for child in child_nodes(net_node):
                head = node_head(child)
                if head == "comp":
                    ref = child[1] if len(child) >= 2 and isinstance(child[1], str) else ""
                    pin = child_text(child, "pin", "")
                    conns.append((ref, pin))
            board.connectivity_nets[name] = conns
            if name and name not in board.net_order:
                board.net_order.append(name)
            board.constraint_props.setdefault(name, {})

    constraint_node = parse_sexpr(section_map["constraint"].text) if "constraint" in section_map else None
    if isinstance(constraint_node, list):
        constraint_nets = next(child_nodes(constraint_node, "nets"), None)
        if constraint_nets is not None:
            for net_node in child_nodes(constraint_nets, "net"):
                if len(net_node) < 2 or not isinstance(net_node[1], str):
                    continue
                name = net_node[1]
                props = board.constraint_props.setdefault(name, {})
                props_node = next(child_nodes(net_node, "props"), None)
                if props_node is None:
                    continue
                for prop in child_nodes(props_node, "propname"):
                    if len(prop) < 4 or not isinstance(prop[1], str):
                        continue
                    prop_name = prop[1]
                    prop_value = ""
                    for idx in range(2, len(prop) - 1):
                        if prop[idx] == "propvalue" and isinstance(prop[idx + 1], str):
                            prop_value = prop[idx + 1]
                            break
                    if prop_name:
                        props[prop_name] = prop_value

    if "components" in section_map:
        components_node = parse_sexpr(section_map["components"].text)
        for comp_node in child_nodes(components_node, "component"):
            ref = comp_node[1] if len(comp_node) >= 2 and isinstance(comp_node[1], str) else "U?"
            part = child_text(comp_node, "part", ref)
            xy = next(child_nodes(comp_node, "xy"), None)
            if xy is None or len(xy) < 3:
                continue
            cx = int(round(float(xy[1])))
            cy = int(round(float(xy[2])))
            rotation_deg = child_number(comp_node, "rotation", 0.0) / 100.0
            mirrored = child_bool_text(comp_node, "mirrored", False)
            fp_node = next(child_nodes(comp_node, "footprint"), None)
            footprint_name = child_text(comp_node, "footprint", part)
            pins: List[PinInstance] = []
            if fp_node is not None:
                if len(fp_node) >= 2 and isinstance(fp_node[1], str):
                    footprint_name = fp_node[1]
                pins_node = next(child_nodes(fp_node, "pins"), None)
                if pins_node is not None:
                    for pin_node in child_nodes(pins_node, "pin"):
                        number = child_text(pin_node, "number", "")
                        pxy = next(child_nodes(pin_node, "xy"), None)
                        if pxy is None or len(pxy) < 3:
                            continue
                        px = int(round(float(pxy[1])))
                        py = int(round(float(pxy[2])))
                        prot = child_number(pin_node, "rotation", rotation_deg * 100.0) / 100.0
                        ps_node = next(child_nodes(pin_node, "padstack"), None)
                        ps_name = ps_node[1] if ps_node is not None and len(ps_node) >= 2 and isinstance(ps_node[1], str) else ""
                        requested_layers: List[str] = []
                        if ps_node is not None:
                            conn = next(child_nodes(ps_node, "connection"), None)
                            if conn is not None:
                                for layer_node in child_nodes(conn, "layer"):
                                    if len(layer_node) >= 2 and isinstance(layer_node[1], str):
                                        requested_layers.append(str(layer_node[1]))
                        if not requested_layers:
                            requested_layers = ["Bottom" if mirrored else "Top"]
                        geom = pick_pad_geometry(padstacks.get(ps_name), requested_layers)
                        pins.append(
                            PinInstance(
                                number=number,
                                x=px,
                                y=py,
                                rotation_deg=prot,
                                net="",
                                geometry=geom,
                                padstack_name=ps_name,
                                is_testpoint=bool(child_text(pin_node, "testpoint", "")),
                            )
                        )
            board.components.append(
                ComponentInstance(
                    ref=ref,
                    part=part,
                    footprint=footprint_name,
                    x=cx,
                    y=cy,
                    rotation_deg=rotation_deg,
                    mirrored=mirrored,
                    pins=pins,
                )
            )

    pin_net_map: Dict[Tuple[str, str], str] = {}
    for net_name, conns in board.connectivity_nets.items():
        for ref, pin in conns:
            pin_net_map[(ref, pin)] = net_name
    for comp in board.components:
        for pin in comp.pins:
            pin.net = pin_net_map.get((comp.ref, pin.number), "")

    if "vias" in section_map:
        vias_node = parse_sexpr(section_map["vias"].text)
        for via_node in child_nodes(vias_node, "via"):
            net = child_text(via_node, "net", "")
            xy = next(child_nodes(via_node, "xy"), None)
            if xy is None or len(xy) < 3:
                continue
            px = int(round(float(xy[1])))
            py = int(round(float(xy[2])))
            rotation_deg = child_number(via_node, "rotation", 0.0) / 100.0
            mirrored = child_bool_text(via_node, "mirrored", False)
            ps_node = next(child_nodes(via_node, "padstack"), None)
            ps_name = ps_node[1] if ps_node is not None and len(ps_node) >= 2 and isinstance(ps_node[1], str) else ""
            layers: List[str] = []
            if ps_node is not None:
                conn = next(child_nodes(ps_node, "connection"), None)
                if conn is not None:
                    for layer_node in child_nodes(conn, "layer"):
                        if len(layer_node) >= 2 and isinstance(layer_node[1], str):
                            layers.append(str(layer_node[1]))
            pstk = padstacks.get(ps_name)
            geom = pick_pad_geometry(pstk, layers or ["Top", "Bottom"])
            size = max(geom.size_x, geom.size_y, pstk.drill if pstk else 0)
            drill = pstk.drill if pstk else geom.drill
            board.vias.append(
                ViaInstance(
                    net=net,
                    x=px,
                    y=py,
                    rotation_deg=rotation_deg,
                    mirrored=mirrored,
                    size=size,
                    drill=drill,
                    layers=tuple(layers or ["Top", "Bottom"]),
                    padstack_name=ps_name,
                )
            )

    if "wires" in section_map:
        wires_node = parse_sexpr(section_map["wires"].text)
        for wire_node in child_nodes(wires_node, "wire"):
            net = child_text(wire_node, "net", "")
            path_node = next(child_nodes(wire_node, "path"), None)
            if path_node is None:
                continue
            layer = child_text(path_node, "layer", "")
            board.wires.append(WirePath(net=net, layer=layer, steps=parse_path_steps(path_node)))

    if "conductives" in section_map:
        cond_node = parse_sexpr(section_map["conductives"].text)
        for surf_node in child_nodes(cond_node, "surface"):
            net = child_text(surf_node, "net", "")
            boundary = next(child_nodes(surf_node, "boundary"), None)
            if boundary is None:
                continue
            path_node = next(child_nodes(boundary, "path"), None)
            if path_node is None:
                continue
            layer = child_text(surf_node, "layer", "")
            holes: List[List[PathStep]] = []
            voids_node = next(child_nodes(surf_node, "voids"), None)
            if voids_node is not None:
                for void_node in child_nodes(voids_node, "void"):
                    vpath = next(child_nodes(void_node, "path"), None)
                    if vpath is not None:
                        holes.append(parse_path_steps(vpath))
            board.zones.append(
                SurfaceZone(
                    net=net,
                    layer=layer,
                    boundary=parse_path_steps(path_node),
                    holes=holes,
                    source_kind="conductive",
                )
            )

    if "surfaces" in section_map:
        surfaces_node = parse_sexpr(section_map["surfaces"].text)
        stencil_candidates: Dict[str, List[SurfaceZone]] = {}
        for surf_node in child_nodes(surfaces_node, "surface"):
            txt_layer = child_text(surf_node, "layer", "")
            zone_layer = top_level_surface_zone_layer(txt_layer)
            if zone_layer is None:
                continue
            boundary = next(child_nodes(surf_node, "boundary"), None)
            if boundary is None:
                continue
            path_node = next(child_nodes(boundary, "path"), None)
            if path_node is None:
                continue
            boundary_steps = parse_path_steps(path_node)
            if not boundary_steps:
                continue
            zone = SurfaceZone(
                net="",
                layer=zone_layer,
                boundary=boundary_steps,
                holes=[],
                source_kind="surface",
            )
            if txt_layer.startswith("Layout/Resist_"):
                board.zones.append(zone)
            elif txt_layer.startswith("Cell/Stencil_"):
                stencil_candidates.setdefault(zone_layer, []).append(zone)

        for zone_layer, zones in stencil_candidates.items():
            areas = [path_bbox_area(zone.boundary) for zone in zones]
            upper_limit = iqr_upper_outlier_limit(areas)
            seen: set[Tuple[Point, ...]] = set()
            for zone in zones:
                if not is_simple_surface_boundary(zone.boundary):
                    continue
                area = path_bbox_area(zone.boundary)
                if upper_limit is not None and area > upper_limit:
                    continue
                key = normalized_boundary_key(zone.boundary)
                if not key or key in seen:
                    continue
                seen.add(key)
                board.zones.append(zone)

    if "polygons" in section_map:
        polys_node = parse_sexpr(section_map["polygons"].text)
        for poly_node in child_nodes(polys_node, "polygon"):
            path_node = next(child_nodes(poly_node, "path"), None)
            if path_node is None:
                continue
            layer = child_text(path_node, "layer", "")
            if layer == "Boundary/All":
                board.outlines.append(OutlinePath(layer=layer, steps=parse_path_steps(path_node)))

    if not board.outlines:
        bounds = infer_bounds_from_points(
            [(comp.x, comp.y) for comp in board.components]
            + [(via.x, via.y) for via in board.vias]
        )
        board.outlines = rect_outline_from_bounds(bounds)

    if board.width <= 0 or board.height <= 0:
        outline_pts: List[Tuple[int, int]] = []
        for outline in board.outlines:
            outline_pts.extend(flatten_steps(outline.steps))
        min_x, min_y, max_x, max_y = infer_bounds_from_points(outline_pts)
        board.lowerleft_x = min_x
        board.lowerleft_y = min_y
        board.width = max_x - min_x
        board.height = max_y - min_y

    if not board.layers:
        board.layers = [
            LayerSpec("Top", "Top", "signal"),
            LayerSpec("Bottom", "Bottom", "signal"),
        ]
    return board


def infer_kicad_net_ids(board: BoardModel) -> Dict[str, int]:
    return {name: idx for idx, name in enumerate(board.all_net_names(), start=1)}


def kicad_layer_table(board: BoardModel) -> List[Tuple[int, str, str]]:
    copper = board.layers or [LayerSpec("Top", "Top", "signal"), LayerSpec("Bottom", "Bottom", "signal")]
    top = copper[0]
    bottom = copper[-1] if len(copper) > 1 else LayerSpec("Bottom", "Bottom", "signal")
    layers: List[Tuple[int, str, str]] = [(0, top.kicad_name, top.kind)]
    for idx, layer in enumerate(copper[1:-1], start=1):
        layers.append((idx, layer.kicad_name, layer.kind))
    layers.append((31, bottom.kicad_name, bottom.kind))
    layers.extend(STANDARD_KICAD_USER_LAYERS)
    return layers


def kicad_pad_layers(geom: PadGeometry) -> str:
    if geom.pad_type == "smd":
        if geom.side == "Bottom":
            return "Bottom B.Paste B.Mask"
        return "Top F.Paste F.Mask"
    return "*.Cu *.Mask"


def component_pin_local_xy(comp: ComponentInstance, pin: PinInstance) -> Tuple[int, int]:
    dx = pin.x - comp.x
    dy = pin.y - comp.y
    rot = math.radians(comp.rotation_deg)
    local_x = dx * math.cos(rot) - dy * math.sin(rot)
    local_y_prime = dx * math.sin(rot) + dy * math.cos(rot)
    local_y = -local_y_prime if comp.mirrored else local_y_prime
    return int(round(local_x)), int(round(local_y))


def pad_rotation_local(comp: ComponentInstance, pin: PinInstance) -> float:
    rot = pin.rotation_deg - comp.rotation_deg
    if comp.mirrored:
        rot = -rot
    return rot


def write_kicad(board: BoardModel) -> str:
    net_ids = infer_kicad_net_ids(board)
    lines: List[str] = []
    def shift_point(x: int, y: int) -> Tuple[int, int]:
        return outline_only_translate((x, y))

    lines.append("( kicad_pcb  ( version 20171130 )")
    lines.append('( host pcbnew "convert_kicad_txt.py" )')
    track_count = sum(max(0, len(flatten_route_steps_for_kicad(w.steps)) - 1) for w in board.wires)
    zone_count = len(board.zones)
    lines.append(" ( general  ( thickness 1.6 )")
    lines.append(" ( drawings 0 )")
    lines.append(f" ( tracks {track_count} )")
    lines.append(f" ( zones {zone_count} )")
    lines.append(f" ( modules {len(board.components)} )")
    lines.append(f" ( nets {len(net_ids) + 1} )")
    lines.append(")")
    lines.append(" ( page A4 )")
    lines.append(" ( layers")
    for layer_id, name, kind in kicad_layer_table(board):
        lines.append(f" ( {layer_id} {name} {kind} )")
    lines.append(")")

    via_sizes = [via.size for via in board.vias if via.size > 0]
    via_drills = [via.drill for via in board.vias if via.drill > 0]
    first_pad = next((pin.geometry for comp in board.components for pin in comp.pins if pin.geometry.size_x > 0), None)
    pad_sx = first_pad.size_x if first_pad else mil_to_dbu(60)
    pad_sy = first_pad.size_y if first_pad else mil_to_dbu(60)
    lines.append(" ( setup")
    lines.append(" ( last_trace_width 0.1524 )")
    lines.append(" ( trace_clearance 0.1524 )")
    lines.append(" ( zone_clearance 0.508 )")
    lines.append(" ( trace_min 0.127 )")
    lines.append(f" ( via_size {fmt_mm(max(via_sizes) if via_sizes else mil_to_dbu(30))} )")
    lines.append(f" ( via_drill {fmt_mm(max(via_drills) if via_drills else mil_to_dbu(16))} )")
    lines.append(" ( edge_width 0.05 )")
    lines.append(" ( segment_width 0.2 )")
    lines.append(" ( pcb_text_width 0.3 )")
    lines.append(" ( pcb_text_size 1.5 1.5 )")
    lines.append(" ( mod_text_size 1 1 )")
    lines.append(" ( mod_text_width 0.15 )")
    lines.append(f" ( pad_size {fmt_mm(pad_sx)} {fmt_mm(pad_sy)} )")
    lines.append(f" ( pad_drill {fmt_mm(max(via_drills) if via_drills else mil_to_dbu(16))} )")
    lines.append(" ( pad_to_mask_clearance 0.051 )")
    lines.append(" ( solder_mask_min_width 0.25 )")
    lines.append(" ( aux_axis_origin 0 0 )")
    lines.append(" ( visible_elements FFFFFF7F )")
    lines.append(")")

    lines.append(' ( net 0 "" )')
    for net_name, net_id in net_ids.items():
        lines.append(f" ( net {net_id} {quote_atom(net_name)} )")

    lines.append(" ( net_class Default \"default\"")
    lines.append(" ( clearance 0.1524 )")
    lines.append(" ( trace_width 0.1524 )")
    lines.append(f" ( via_dia {fmt_mm(max(via_sizes) if via_sizes else mil_to_dbu(30))} )")
    lines.append(f" ( via_drill {fmt_mm(max(via_drills) if via_drills else mil_to_dbu(16))} )")
    lines.append(" ( uvia_dia 0.3 )")
    lines.append(" ( uvia_drill 0.1 )")
    for net_name in board.all_net_names():
        lines.append(f" ( add_net {quote_atom(net_name)} )")
    lines.append(")")

    fixed_outline = [
        (OUTLINE_ONLY_ORIGIN_X, OUTLINE_ONLY_ORIGIN_Y),
        (OUTLINE_ONLY_ORIGIN_X + OUTLINE_ONLY_SIZE_X, OUTLINE_ONLY_ORIGIN_Y),
        (OUTLINE_ONLY_ORIGIN_X + OUTLINE_ONLY_SIZE_X, OUTLINE_ONLY_ORIGIN_Y + OUTLINE_ONLY_SIZE_Y),
        (OUTLINE_ONLY_ORIGIN_X, OUTLINE_ONLY_ORIGIN_Y + OUTLINE_ONLY_SIZE_Y),
        (OUTLINE_ONLY_ORIGIN_X, OUTLINE_ONLY_ORIGIN_Y),
    ]
    for a, b in zip(fixed_outline, fixed_outline[1:]):
        lines.append(f" ( gr_line  ( start {fmt_mm(a[0])} {fmt_mm(a[1])} )")
        lines.append(f" ( end {fmt_mm(b[0])} {fmt_mm(b[1])} )")
        lines.append(" ( layer Edge.Cuts )")
        lines.append(" ( width 0.05 )")
        lines.append(")")

    for comp in board.components:
        layer = "Bottom" if comp.mirrored else "Top"
        silk = "B.SilkS" if comp.mirrored else "F.SilkS"
        fab = "B.Fab" if comp.mirrored else "F.Fab"
        angle = f" {fmt_float(comp.rotation_deg)}" if abs(comp.rotation_deg) > 1e-6 else ""
        comp_x, comp_y = shift_point(comp.x, comp.y)
        lines.append(f" ( module {quote_atom(comp.footprint)}  ( layer {layer} )")
        lines.append(" ( tedit 0 )")
        lines.append(" ( tstamp 0 )")
        lines.append(f" ( at {fmt_mm(comp_x)} {fmt_mm(comp_y)}{angle} )")
        lines.append(f" ( fp_text reference {quote_atom(comp.ref)}  ( at 0 -1 )")
        lines.append(f" ( layer {silk} )")
        lines.append(" ( effects  ( font  ( size 0.5 0.5 )")
        lines.append(" ( thickness 0.1 )")
        lines.append(")")
        lines.append(")")
        lines.append(")")
        lines.append(f" ( fp_text value {quote_atom(comp.part)}  ( at 0 1 )")
        lines.append(f" ( layer {fab} )")
        lines.append(" ( effects  ( font  ( size 0.5 0.5 )")
        lines.append(" ( thickness 0.1 )")
        lines.append(")")
        lines.append(")")
        lines.append(")")
        for pin in comp.pins:
            local_x, local_y = component_pin_local_xy(comp, pin)
            pad_rot = pad_rotation_local(comp, pin)
            rot_suffix = f" {fmt_float(pad_rot)}" if abs(pad_rot) > 1e-6 else ""
            net_id = net_ids.get(pin.net, 0)
            shape = pad_shape_to_kicad(pin.geometry.shape)
            lines.append(f" ( pad {quote_atom(pin.number)} {pin.geometry.pad_type} {shape}  ( at {fmt_mm(local_x)} {fmt_mm(local_y)}{rot_suffix} )")
            lines.append(f" ( size {fmt_mm(pin.geometry.size_x)} {fmt_mm(pin.geometry.size_y)} )")
            if pin.geometry.pad_type != "smd" and pin.geometry.drill > 0:
                lines.append(f" ( drill {fmt_mm(pin.geometry.drill)} )")
            lines.append(f" ( layers {kicad_pad_layers(pin.geometry)} )")
            if net_id > 0:
                lines.append(f" ( net {net_id} {quote_atom(pin.net)} )")
            lines.append(")")
        lines.append(")")

    for via in board.vias:
        net_id = net_ids.get(via.net, 0)
        via_layers = [layer_txt_to_kicad(layer) for layer in via.layers if layer and not layer.startswith("Resist")]
        if not via_layers:
            via_layers = ["Top", "Bottom"]
        elif len(via_layers) == 1:
            via_layers = [via_layers[0], via_layers[0]]
        else:
            via_layers = [via_layers[0], via_layers[-1]]
        layer_tokens = " ".join(via_layers)
        via_x, via_y = shift_point(via.x, via.y)
        lines.append(f" ( via  ( at {fmt_mm(via_x)} {fmt_mm(via_y)} )")
        lines.append(f" ( size {fmt_mm(via.size)} )")
        if via.drill > 0:
            lines.append(f" ( drill {fmt_mm(via.drill)} )")
        lines.append(f" ( layers {layer_tokens} )")
        lines.append(f" ( net {net_id} )")
        lines.append(")")

    for wire in board.wires:
        pts = flatten_route_steps_for_kicad(wire.steps)
        if len(pts) < 2:
            continue
        net_id = net_ids.get(wire.net, 0)
        layer = layer_txt_to_kicad(wire.layer)
        width = wire_segment_width(wire) or mil_to_dbu(5)
        for a, b in zip(pts, pts[1:]):
            a = shift_point(a[0], a[1])
            b = shift_point(b[0], b[1])
            lines.append(f" ( segment  ( start {fmt_mm(a[0])} {fmt_mm(a[1])} )")
            lines.append(f" ( end {fmt_mm(b[0])} {fmt_mm(b[1])} )")
            lines.append(f" ( width {fmt_mm(width)} )")
            lines.append(f" ( layer {layer} )")
            lines.append(f" ( net {net_id} )")
            lines.append(")")

    for zone in board.zones:
        pts = flatten_steps_for_kicad(zone.boundary)
        if len(pts) < 3:
            continue
        net_id = net_ids.get(zone.net, 0)
        lines.append(f" ( zone  ( net {net_id} )")
        lines.append(f" ( net_name {quote_atom(zone.net)} )")
        lines.append(f" ( layer {layer_txt_to_kicad(zone.layer)} )")
        lines.append(" ( hatch edge 0.508 )")
        lines.append(" ( connect_pads  ( clearance 0 )")
        lines.append(")")
        lines.append(" ( min_thickness 0.254 )")
        lines.append(" ( fill yes  ( arc_segments 16 )")
        lines.append(" ( thermal_gap 0.508 )")
        lines.append(" ( thermal_bridge_width 0.508 )")
        lines.append(")")
        lines.append(" ( polygon  ( pts")
        for x, y in pts:
            x, y = shift_point(x, y)
            lines.append(f" ( xy {fmt_mm(x)} {fmt_mm(y)} )")
        lines.append(")")
        lines.append(")")
        lines.append(")")

    lines.append(")")
    return "\n".join(lines) + "\n"


def parse_kicad_board(path: Path) -> BoardModel:
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = extract_root_children(text, "kicad_pcb")
    board = BoardModel(stem=path.stem.replace(".default", "").replace(".outline_only", ""))
    net_names: Dict[int, str] = {}
    layer_specs: List[LayerSpec] = []
    outline_lines: List[Tuple[Tuple[int, int], Tuple[int, int]]] = []

    for sec in sections:
        if sec.name == "layers":
            node = parse_sexpr(sec.text)
            for child in child_nodes(node):
                if len(child) < 3 or not isinstance(child[0], str) or not isinstance(child[1], str) or not isinstance(child[2], str):
                    continue
                try:
                    layer_id = int(child[0])
                except ValueError:
                    continue
                layer_name = child[1]
                layer_kind = child[2]
                if layer_id not in {0, 31} and not (1 <= layer_id <= 30):
                    continue
                layer_specs.append(
                    LayerSpec(
                        txt_name=layer_kicad_to_txt(layer_name),
                        kicad_name=layer_name,
                        kind="power" if layer_kind == "power" else "signal",
                        negative=(layer_kind == "power"),
                    )
                )
        elif sec.name == "setup":
            node = parse_sexpr(sec.text)
            board.default_trace_width = mm_to_dbu(child_number(node, "last_trace_width", 0.0))
            board.default_trace_clearance = mm_to_dbu(child_number(node, "trace_clearance", 0.0))
            board.default_via_size = mm_to_dbu(child_number(node, "via_size", 0.0))
            board.default_via_drill = mm_to_dbu(child_number(node, "via_drill", 0.0))
        elif sec.name == "net":
            node = parse_sexpr(sec.text)
            if len(node) >= 3 and isinstance(node[1], str) and isinstance(node[2], str):
                try:
                    net_id = int(node[1])
                    net_name = node[2]
                    net_names[net_id] = net_name
                    if net_id > 0 and net_name and net_name not in board.net_order:
                        board.net_order.append(net_name)
                except ValueError:
                    pass

    if not layer_specs:
        layer_specs = [LayerSpec("Top", "Top", "signal"), LayerSpec("Bottom", "Bottom", "signal")]
    layer_specs.sort(key=lambda item: 0 if item.kicad_name == "Top" else (999 if item.kicad_name == "Bottom" else int(re.sub(r"\D", "", item.kicad_name) or "500")))
    board.layers = layer_specs

    for sec in sections:
        if sec.name == "module":
            node = parse_sexpr(sec.text)
            footprint = node[1] if len(node) >= 2 and isinstance(node[1], str) else "UNKNOWN"
            layer = child_text(node, "layer", "Top")
            at = next(child_nodes(node, "at"), None)
            if at is None or len(at) < 3:
                continue
            cx = mm_to_dbu(float(at[1]))
            cy = mm_to_dbu(float(at[2]))
            rotation_deg = float(at[3]) if len(at) >= 4 and isinstance(at[3], str) else 0.0
            ref = "U?"
            value = footprint
            for fp_text in child_nodes(node, "fp_text"):
                if len(fp_text) >= 3 and isinstance(fp_text[1], str) and isinstance(fp_text[2], str):
                    if fp_text[1] == "reference":
                        ref = fp_text[2]
                    elif fp_text[1] == "value":
                        value = fp_text[2]
            mirrored = layer == "Bottom"
            pins: List[PinInstance] = []
            for pad in child_nodes(node, "pad"):
                if len(pad) < 4:
                    continue
                number = str(pad[1])
                pad_type = str(pad[2])
                shape = kicad_shape_to_pad(str(pad[3]))
                at_node = next(child_nodes(pad, "at"), None)
                size_node = next(child_nodes(pad, "size"), None)
                if at_node is None or size_node is None or len(at_node) < 3 or len(size_node) < 3:
                    continue
                px = mm_to_dbu(float(at_node[1]))
                py = mm_to_dbu(float(at_node[2]))
                prot = float(at_node[3]) if len(at_node) >= 4 and isinstance(at_node[3], str) else 0.0
                sx = mm_to_dbu(float(size_node[1]))
                sy = mm_to_dbu(float(size_node[2]))
                drill_node = next(child_nodes(pad, "drill"), None)
                drill = 0
                if drill_node is not None and len(drill_node) >= 2 and isinstance(drill_node[1], str):
                    drill = mm_to_dbu(float(drill_node[1]))
                layers_node = next(child_nodes(pad, "layers"), None)
                layer_tokens = [str(item) for item in layers_node[1:]] if isinstance(layers_node, list) else []
                connected_layers = tuple(
                    layer_kicad_to_txt(token)
                    for token in layer_tokens
                    if token not in {"F.Paste", "B.Paste", "F.Mask", "B.Mask", "*.Mask", "*.Cu"}
                )
                if not connected_layers:
                    if pad_type != "smd":
                        connected_layers = copper_layer_names(board)
                    else:
                        connected_layers = ("Bottom",) if mirrored else ("Top",)
                local_py = -py if mirrored else py
                theta = math.radians(-rotation_deg)
                abs_x = int(round(cx + px * math.cos(theta) - local_py * math.sin(theta)))
                abs_y = int(round(cy + px * math.sin(theta) + local_py * math.cos(theta)))
                net_node = next(child_nodes(pad, "net"), None)
                net_name = ""
                if net_node is not None and len(net_node) >= 2:
                    if len(net_node) >= 3 and isinstance(net_node[2], str):
                        net_name = net_node[2]
                    else:
                        try:
                            net_name = net_names.get(int(net_node[1]), "")
                        except ValueError:
                            net_name = ""
                pins.append(
                    PinInstance(
                        number=number,
                        x=abs_x,
                        y=abs_y,
                        rotation_deg=rotation_deg + prot,
                        net=net_name,
                        geometry=PadGeometry(
                            shape=shape,
                            size_x=sx,
                            size_y=sy,
                            drill=drill,
                            pad_type=pad_type,
                            side="Bottom" if mirrored else "Top",
                            connected_layers=connected_layers,
                        ),
                    )
                )
            board.components.append(
                ComponentInstance(
                    ref=ref,
                    part=value,
                    footprint=footprint,
                    x=cx,
                    y=cy,
                    rotation_deg=rotation_deg,
                    mirrored=mirrored,
                    pins=pins,
                )
            )
        elif sec.name == "segment":
            node = parse_sexpr(sec.text)
            start = next(child_nodes(node, "start"), None)
            end = next(child_nodes(node, "end"), None)
            if start is None or end is None or len(start) < 3 or len(end) < 3:
                continue
            width = mm_to_dbu(child_number(node, "width", 0.127))
            layer = child_text(node, "layer", "Top")
            net_id = int(child_text(node, "net", "0") or "0")
            net_name = net_names.get(net_id, "")
            board.wires.append(
                WirePath(
                    net=net_name,
                    layer=conductor_layer_txt(layer_kicad_to_txt(layer)),
                    steps=[
                        PathStep("line", mm_to_dbu(float(start[1])), mm_to_dbu(float(start[2])), width),
                        PathStep("line", mm_to_dbu(float(end[1])), mm_to_dbu(float(end[2])), width),
                    ],
                )
            )
        elif sec.name == "via":
            node = parse_sexpr(sec.text)
            at = next(child_nodes(node, "at"), None)
            if at is None or len(at) < 3:
                continue
            layers = next(child_nodes(node, "layers"), None)
            layer_tokens = tuple(layer_kicad_to_txt(str(tok)) for tok in layers[1:]) if isinstance(layers, list) else ("Top", "Bottom")
            net_id = int(child_text(node, "net", "0") or "0")
            board.vias.append(
                ViaInstance(
                    net=net_names.get(net_id, ""),
                    x=mm_to_dbu(float(at[1])),
                    y=mm_to_dbu(float(at[2])),
                    rotation_deg=0.0,
                    mirrored=False,
                    size=mm_to_dbu(child_number(node, "size", 0.8)),
                    drill=mm_to_dbu(child_number(node, "drill", 0.4)),
                    layers=layer_tokens,
                )
            )
        elif sec.name == "zone":
            node = parse_sexpr(sec.text)
            net_name = child_text(node, "net_name", "")
            layer = child_text(node, "layer", "Top")
            polygon = next(child_nodes(node, "polygon"), None)
            if polygon is None:
                continue
            pts_node = next(child_nodes(polygon, "pts"), None)
            if pts_node is None:
                continue
            steps: List[PathStep] = []
            for pt_node in child_nodes(pts_node, "xy"):
                if len(pt_node) >= 3 and isinstance(pt_node[1], str) and isinstance(pt_node[2], str):
                    steps.append(PathStep("line", mm_to_dbu(float(pt_node[1])), mm_to_dbu(float(pt_node[2])), 0))
            board.zones.append(
                SurfaceZone(
                    net=net_name,
                    layer=conductor_layer_txt(layer_kicad_to_txt(layer)),
                    boundary=steps,
                )
            )
        elif sec.name == "gr_line":
            node = parse_sexpr(sec.text)
            if child_text(node, "layer", "") != "Edge.Cuts":
                continue
            start = next(child_nodes(node, "start"), None)
            end = next(child_nodes(node, "end"), None)
            if start is None or end is None or len(start) < 3 or len(end) < 3:
                continue
            outline_lines.append(
                (
                    (mm_to_dbu(float(start[1])), mm_to_dbu(float(start[2]))),
                    (mm_to_dbu(float(end[1])), mm_to_dbu(float(end[2]))),
                )
            )

    for net_name in board.all_net_names():
        board.connectivity_nets.setdefault(net_name, [])
    for comp in board.components:
        for pin in comp.pins:
            if pin.net:
                board.connectivity_nets.setdefault(pin.net, []).append((comp.ref, pin.number))

    if outline_lines:
        for a, b in outline_lines:
            board.outlines.append(OutlinePath(layer="Boundary/All", steps=[PathStep("line", a[0], a[1]), PathStep("line", b[0], b[1])]))
    if not board.outlines:
        pts = [(comp.x, comp.y) for comp in board.components] + [(via.x, via.y) for via in board.vias]
        board.outlines = rect_outline_from_bounds(infer_bounds_from_points(pts))
    else:
        board.outlines = merge_outline_paths(board.outlines)

    pts: List[Tuple[int, int]] = []
    for outline in board.outlines:
        pts.extend(flatten_steps(outline.steps))
    min_x, min_y, max_x, max_y = infer_bounds_from_points(pts)
    if (
        min_x == OUTLINE_ONLY_ORIGIN_X
        and min_y == OUTLINE_ONLY_ORIGIN_Y
        and max_x == OUTLINE_ONLY_ORIGIN_X + OUTLINE_ONLY_SIZE_X
        and max_y == OUTLINE_ONLY_ORIGIN_Y + OUTLINE_ONLY_SIZE_Y
    ):
        for comp in board.components:
            comp.x, comp.y = outline_only_untranslate((comp.x, comp.y))
            for pin in comp.pins:
                pin.x, pin.y = outline_only_untranslate((pin.x, pin.y))
        for via in board.vias:
            via.x, via.y = outline_only_untranslate((via.x, via.y))
        for wire in board.wires:
            for step in wire.steps:
                step.x, step.y = outline_only_untranslate((step.x, step.y))
                if step.cx is not None and step.cy is not None:
                    step.cx, step.cy = outline_only_untranslate((step.cx, step.cy))
        for zone in board.zones:
            for step in zone.boundary:
                step.x, step.y = outline_only_untranslate((step.x, step.y))
                if step.cx is not None and step.cy is not None:
                    step.cx, step.cy = outline_only_untranslate((step.cx, step.cy))
            for hole in zone.holes:
                for step in hole:
                    step.x, step.y = outline_only_untranslate((step.x, step.y))
                    if step.cx is not None and step.cy is not None:
                        step.cx, step.cy = outline_only_untranslate((step.cx, step.cy))
        for outline in board.outlines:
            for step in outline.steps:
                step.x, step.y = outline_only_untranslate((step.x, step.y))

    pts = []
    for outline in board.outlines:
        pts.extend(flatten_steps(outline.steps))
    min_x, min_y, max_x, max_y = infer_bounds_from_points(pts)
    board.lowerleft_x = min_x
    board.lowerleft_y = min_y
    board.width = max(0, max_x - min_x)
    board.height = max(0, max_y - min_y)
    return board


def make_pad_name(geom: PadGeometry) -> str:
    if geom.shape == "circle":
        return f"C_{dbu_to_mil(max(geom.size_x, geom.size_y)):.2f}"
    if geom.shape in {"oblong", "oval"}:
        return f"OB_{dbu_to_mil(geom.size_x):.2f}_{dbu_to_mil(geom.size_y):.2f}"
    return f"R_{dbu_to_mil(geom.size_x):.2f}_{dbu_to_mil(geom.size_y):.2f}"


def make_padstack_name(geom: PadGeometry) -> str:
    pad_name = make_pad_name(geom).replace(".", "_")
    if geom.pad_type == "smd":
        side = geom.side.upper()
        return f"TXT_{side}_{pad_name}"
    drill_mil = dbu_to_mil(geom.drill) if geom.drill else 0.0
    return f"TXT_THRU_{pad_name}_D{drill_mil:.2f}".replace(".", "_")


def geometry_bbox(points: Sequence[Tuple[int, int, int, int]]) -> Tuple[int, int, int, int]:
    if not points:
        span = mil_to_dbu(80)
        return (-span, -span, span, span)
    min_x = min(x - sx // 2 for x, y, sx, sy in points)
    max_x = max(x + sx // 2 for x, y, sx, sy in points)
    min_y = min(y - sy // 2 for x, y, sx, sy in points)
    max_y = max(y + sy // 2 for x, y, sx, sy in points)
    margin = max(mil_to_dbu(20), max(max_x - min_x, max_y - min_y) // 20)
    return (min_x - margin, min_y - margin, max_x + margin, max_y + margin)


def make_rect_surface_node(bounds: Tuple[int, int, int, int], layer: str) -> List[Node]:
    min_x, min_y, max_x, max_y = bounds
    path = [
        "path",
        ["lineseg", ["pt", str(min_x), str(min_y)], ["w", "0"]],
        ["lineseg", ["pt", str(min_x), str(max_y)], ["w", "0"]],
        ["lineseg", ["pt", str(max_x), str(max_y)], ["w", "0"]],
        ["lineseg", ["pt", str(max_x), str(min_y)], ["w", "0"]],
        ["lineseg", ["pt", str(min_x), str(min_y)], ["w", "0"]],
    ]
    return ["surface", ["net", "none"], ["boundary", path], ["voids"], ["props"], ["layer", layer]]


def make_origin_circle_surface_node(center: Point, radius: int, layer: str) -> List[Node]:
    cx, cy = center
    path = [
        "path",
        ["lineseg", ["pt", str(cx + radius), str(cy)], ["w", "0"]],
        ["arcseg", ["pt", str(cx + radius), str(cy)], ["w", "0"], ["xy", str(cx), str(cy)], ["rotate", "CCW"]],
    ]
    return ["surface", ["net", "none"], ["boundary", path], ["voids"], ["props"], ["layer", layer]]


def make_line_path_node(start: Point, end: Point, width: int, layer: str) -> List[Node]:
    return [
        "path",
        ["issamewidth", "true"],
        ["lineseg", ["pt", str(start[0]), str(start[1])], ["w", str(width)]],
        ["lineseg", ["pt", str(end[0]), str(end[1])], ["w", str(width)]],
        ["props"],
        ["layer", layer],
    ]


def make_text_node(text: str, size: int, xy: Point, layer: str, justify: str = "LC", mirrored: bool = False) -> List[Node]:
    return [
        "text",
        text,
        ["size", str(size)],
        ["xy", str(xy[0]), str(xy[1])],
        ["rotation", "0"],
        ["mirrored", "true" if mirrored else "false"],
        ["justify", justify],
        ["props"],
        ["layer", layer],
    ]


def footprint_figure_node(
    pin_points: Sequence[Tuple[int, int, int, int]],
    *,
    mirrored: bool = False,
    minimal: bool = False,
    include_texts: bool = True,
    include_origin_paths: bool = True,
) -> List[Node]:
    suffix = "Bottom" if mirrored else "Top"
    bounds = geometry_bbox(pin_points)
    min_x, min_y, max_x, max_y = bounds
    cx = (min_x + max_x) // 2
    cy = (min_y + max_y) // 2
    arm = max(mil_to_dbu(20), min(max_x - min_x, max_y - min_y) // 4)
    origin_arm = mil_to_dbu(20)
    text_y = min_y - max(mil_to_dbu(16), arm // 2)
    surfaces: List[Node] = []
    paths: List[Node] = []
    if include_origin_paths:
        paths = [
            make_line_path_node((cx - origin_arm, cy), (cx + origin_arm, cy), mil_to_dbu(3), "Cell/Origin"),
            make_line_path_node((cx, cy - origin_arm), (cx, cy + origin_arm), mil_to_dbu(3), "Cell/Origin"),
        ]
    texts: List[Node] = []
    if minimal:
        surfaces = []
    else:
        surfaces = [
            make_rect_surface_node(bounds, f"Cell/Placement_{suffix}"),
            make_origin_circle_surface_node((cx, cy), arm, f"User/Manufacturing_Place_Bound_{suffix}"),
        ]
    if include_texts:
        texts = [
            make_text_node("HAMP", 2, (cx, text_y), f"User/User_Part_Number_Assembly_{suffix}", mirrored=mirrored),
            make_text_node("#DEV", 2, (cx, text_y), f"User/Device_Type_Assembly_{suffix}", mirrored=mirrored),
            make_text_node("#REFDES", 3, (min_x, min_y), f"Part RefDes/Silkscreen_{suffix}", justify="LL", mirrored=mirrored),
            make_text_node("#TOL", 2, (cx, text_y), f"User/Tolerance_Assembly_{suffix}", mirrored=mirrored),
            make_text_node("#VAL", 2, (cx, text_y), f"User/Component_Value_Assembly_{suffix}", mirrored=mirrored),
            make_text_node("#REFDES", 3, (cx, min_y), f"Part RefDes/Assembly_{suffix}", mirrored=mirrored),
        ]
    return [
        "figure",
        ["surfaces", *surfaces],
        ["paths", *paths],
        ["texts", *texts],
        ["polygons", "nil"],
        ["conductives", "nil"],
    ]


def safe_identifier_text(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_+./-]+", "_", value or "")
    return sanitized.strip("_") or "ITEM"


def build_generated_library(
    board: BoardModel,
    *,
    minimal_footprint_figures: bool = False,
    include_footprint_texts: bool = True,
    include_origin_paths: bool = True,
) -> Tuple[str, Dict[Tuple[str, str, int, int, int, str], str]]:
    pad_geoms: Dict[str, PadGeometry] = {"None": PadGeometry("circle", 0, 0)}
    padstack_map: Dict[Tuple[str, str, int, int, int, str], str] = {}

    for comp in board.components:
        for pin in comp.pins:
            pad_name = make_pad_name(pin.geometry)
            pad_geoms[pad_name] = pin.geometry
            key = (
                pin.geometry.shape,
                pin.geometry.pad_type,
                pin.geometry.size_x,
                pin.geometry.size_y,
                pin.geometry.drill,
                pin.geometry.side,
            )
            padstack_map[key] = make_padstack_name(pin.geometry)
    for via in board.vias:
        geom = PadGeometry(
            shape="circle",
            size_x=via.size,
            size_y=via.size,
            drill=via.drill,
            pad_type="thru_hole",
            side="Through",
            connected_layers=via.layers,
        )
        pad_name = make_pad_name(geom)
        pad_geoms[pad_name] = geom
        key = (geom.shape, geom.pad_type, geom.size_x, geom.size_y, geom.drill, geom.side)
        padstack_map[key] = make_padstack_name(geom)

    pad_entries: List[str] = ['(pads', '    (pad "None"', '        (shape "null")', '        (figure', '            (polygons)', '        )', '    )']
    for pad_name, geom in sorted(pad_geoms.items()):
        if pad_name == "None":
            continue
        pad_entries.append(f'    (pad "{pad_name}"')
        if geom.shape == "circle":
            pad_entries.append('        (shape "circle")')
            pad_entries.append("        (figure")
            pad_entries.append("            (polygons")
            pad_entries.append(f"                (circle {geom.size_x})")
            pad_entries.append("            )")
            pad_entries.append("        )")
        elif geom.shape in {"oblong", "oval"}:
            pad_entries.append('        (shape "oblong")')
            pad_entries.append("        (figure")
            pad_entries.append("            (polygons")
            pad_entries.append(f"                (oblong {geom.size_x} {geom.size_y})")
            pad_entries.append("            )")
            pad_entries.append("        )")
        else:
            pad_entries.append('        (shape "rectangle")')
            pad_entries.append("        (figure")
            pad_entries.append("            (polygons")
            pad_entries.append(f"                (rectangle {geom.size_x} {geom.size_y})")
            pad_entries.append("            )")
            pad_entries.append("        )")
        pad_entries.append("    )")
    pad_entries.append(")")

    copper_layers = [layer.txt_name for layer in board.layers]
    padstack_entries: List[str] = ["(padstacks"]
    for key, name in sorted(padstack_map.items(), key=lambda item: item[1]):
        shape, pad_type, sx, sy, drill, side = key
        pad_name = make_pad_name(PadGeometry(shape, sx, sy, drill, pad_type, side))
        from_layer = "Top"
        to_layer = "Top"
        usage = "smd"
        psk_type = "smd"
        connected_layers = ["Top"] if side == "Top" else ["Bottom"] if side == "Bottom" else copper_layers
        if pad_type != "smd" or side == "Through":
            from_layer = "Top"
            to_layer = "Bottom"
            usage = "through"
            psk_type = "through"
        elif side == "Bottom":
            from_layer = "Bottom"
            to_layer = "Bottom"

        padstack_entries.append(f'    (padstack "{name}"')
        padstack_entries.append(f'        (psktype "{psk_type}")')
        padstack_entries.append(f'        (pskusage "{usage}")')
        padstack_entries.append(f'        (fromtos "{from_layer}" "{to_layer}")')
        padstack_entries.append("        (drill")
        if drill > 0:
            padstack_entries.append('            (hole "circle"')
            padstack_entries.append('                (holeplating "true")')
            padstack_entries.append(f"                (holesize {drill} {drill})")
            padstack_entries.append("                (holeoffset 0 0)")
            padstack_entries.append("                (holetolerance_x 0 0)")
            padstack_entries.append("                (holetolerance_y 0 0)")
            padstack_entries.append("            )")
        else:
            padstack_entries.append('            (hole "none"')
            padstack_entries.append('                (holeplating "true")')
            padstack_entries.append("                (holesize 0 0)")
            padstack_entries.append("                (holeoffset 0 0)")
            padstack_entries.append("                (holetolerance_x 0 0)")
            padstack_entries.append("                (holetolerance_y 0 0)")
            padstack_entries.append("            )")
        padstack_entries.append('            (symbol (figtype "NULL") (figsize 0 0) (figchars ""))')
        padstack_entries.append("        )")
        padstack_entries.append("        (padsets")
        resist_top = pad_name if side == "Top" and pad_type == "smd" else "None"
        resist_bottom = pad_name if side == "Bottom" and pad_type == "smd" else "None"
        stencil_top = resist_top
        stencil_bottom = resist_bottom
        padstack_entries.append(f'            (padset (layer "Resist_Top") (ptype "connect") (name "{resist_top}") (offset 0 0))')
        padstack_entries.append(f'            (padset (layer "Resist_Bottom") (ptype "connect") (name "{resist_bottom}") (offset 0 0))')
        padstack_entries.append(f'            (padset (layer "Stencil_Top") (ptype "connect") (name "{stencil_top}") (offset 0 0))')
        padstack_entries.append(f'            (padset (layer "Stencil_Bottom") (ptype "connect") (name "{stencil_bottom}") (offset 0 0))')
        for layer_name in copper_layers:
            connect_name = pad_name if layer_name in connected_layers else "None"
            padstack_entries.append(f'            (padset (layer "{layer_name}") (ptype "connect") (name "{connect_name}") (offset 0 0))')
            padstack_entries.append(f'            (padset (layer "{layer_name}") (ptype "thermal") (name "None") (offset 0 0))')
            padstack_entries.append(f'            (padset (layer "{layer_name}") (ptype "clearance") (name "None") (offset 0 0))')
        padstack_entries.append("        )")
        padstack_entries.append("    )")
    padstack_entries.append(")")

    footprint_defs: Dict[str, Tuple[str, List[PinInstance], ComponentInstance]] = {}
    for comp in board.components:
        footprint_defs.setdefault(comp.footprint, (comp.part, comp.pins, comp))

    footprint_entries: List[str] = ["(footprints"]
    part_entries: List[str] = ["(parts"]
    for footprint_name, (part_name, pins, comp) in sorted(footprint_defs.items()):
        part_atom = safe_identifier_text(part_name)
        footprint_entries.append(f'    (footprint "{footprint_name}"')
        footprint_entries.append('        (ftype "Package")')
        footprint_entries.append("        (pins")
        pin_order: List[str] = []
        local_pin_points: List[Tuple[int, int, int, int]] = []
        for pin in pins:
            pad_key = (
                pin.geometry.shape,
                pin.geometry.pad_type,
                pin.geometry.size_x,
                pin.geometry.size_y,
                pin.geometry.drill,
                pin.geometry.side,
            )
            padstack_name = padstack_map[pad_key]
            pin_order.append(pin.number)
            local_x, local_y = component_pin_local_xy(comp, pin)
            local_pin_points.append((local_x, local_y, pin.geometry.size_x, pin.geometry.size_y))
            footprint_entries.append("            (pin")
            footprint_entries.append(f'                (number "{pin.number}")')
            footprint_entries.append(f"                (xy {local_x} {local_y})")
            footprint_entries.append(f"                (rotation {int(round(pad_rotation_local(comp, pin) * 100))})")
            footprint_entries.append(f'                (padstack "{padstack_name}")')
            footprint_entries.append("            )")
        footprint_entries.append("        )")
        footprint_entries.append(
            emit_sexpr(
                footprint_figure_node(
                    local_pin_points,
                    mirrored=comp.mirrored,
                    minimal=minimal_footprint_figures,
                    include_texts=include_footprint_texts,
                    include_origin_paths=include_origin_paths,
                ),
                8,
            )
        )
        footprint_entries.append("        (props")
        footprint_entries.append('            (propname "VERSION_ID" propvalue "1")')
        footprint_entries.append(f'            (propname "LIBRARY_PATH" propvalue "pseudo_donor/{footprint_name}.psm")')
        footprint_entries.append("        )")
        footprint_entries.append("    )")

        part_entries.append(f'    (part "{part_name}"')
        part_entries.append(f'        (footprint "{footprint_name}")')
        part_entries.append('        (class "IC")')
        part_entries.append(f"        (pincount {len(pin_order)})")
        part_entries.append("        (pinorders")
        joined = " ".join(f'"{pin}"' for pin in pin_order)
        part_entries.append(f'            ("{footprint_name}" ({joined}))')
        part_entries.append("        )")
        part_entries.append("        (pinuses")
        part_entries.append(f'            ("{footprint_name}" ({" ".join(["UNSPEC"] * len(pin_order))}))')
        part_entries.append("        )")
        part_entries.append("        (pinswaps")
        part_entries.append(f'            ("{footprint_name}" ({joined}))')
        part_entries.append("        )")
        part_entries.append("        (functions")
        part_entries.append(f'            ("{footprint_name}" "{part_atom}" ({joined}))')
        part_entries.append("        )")
        part_entries.append("        (props)")
        part_entries.append("    )")
    footprint_entries.append(")")
    part_entries.append(")")

    library = rebuild_root(
        "library",
        [
            "\n".join(pad_entries),
            "\n".join(padstack_entries),
            "\n".join(footprint_entries),
            "\n".join(part_entries),
        ],
    )
    return library, padstack_map


def build_donor_via_padstack_choices(donor_board: Optional[BoardModel]) -> Dict[Tuple[int, int], List[DonorViaChoice]]:
    choices: Dict[Tuple[int, int], List[DonorViaChoice]] = {}
    if donor_board is None:
        return choices
    for via in donor_board.vias:
        if not via.padstack_name:
            continue
        key = (via.size, via.drill)
        choices.setdefault(key, []).append(
            DonorViaChoice(
                padstack_name=via.padstack_name,
                layers=tuple(via.layers),
                size=via.size,
                drill=via.drill,
            )
        )
    return choices


def pick_donor_via_choice(
    via: ViaInstance,
    connected_layers: Tuple[str, ...],
    donor_choices: Dict[Tuple[int, int], List[DonorViaChoice]],
) -> Optional[DonorViaChoice]:
    candidates = donor_choices.get((via.size, via.drill), [])
    if not candidates:
        candidates = [choice for choices in donor_choices.values() for choice in choices]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda choice: (
                abs(choice.size - via.size) + abs(choice.drill - via.drill),
                abs(len(choice.layers) - len(connected_layers)),
                0 if choice.layers == connected_layers else 1,
            ),
        )
    for choice in candidates:
        if choice.layers == connected_layers:
            return choice
    for choice in candidates:
        if len(choice.layers) == len(connected_layers):
            return choice
    return candidates[0]


def donor_constraint_props_for_board(
    board: BoardModel,
    donor_board: Optional[BoardModel],
) -> Dict[str, Dict[str, str]]:
    merged: Dict[str, Dict[str, str]] = {}
    donor_props = donor_board.constraint_props if donor_board is not None else {}
    for net_name in board.all_net_names():
        props = dict(donor_props.get(net_name, {}))
        props.update(board.constraint_props.get(net_name, {}))
        merged[net_name] = props
    return merged


def emit_txt_parameters(board: BoardModel) -> str:
    return "\n".join(
        [
            "(parameters",
            '    (unit "mils")',
            "    (accuracy 2)",
            f"    (lowerleft {board.lowerleft_x} {board.lowerleft_y})",
            f"    (size {board.width} {board.height})",
            ")",
        ]
    )


def emit_default_grids() -> str:
    return "\n".join(
        [
            "(grids",
            '    (mode "On")',
            '    (active "Other")',
            "    (offset 0 0)",
            '    (tracegrid "TG1X5")',
            '    (viagrid "VG25X2")',
            '    (placegrid "PG25")',
            '    (othergrid "OG100")',
            "    (griditems",
            '        (item (name "TG1X5") (gtype "Trace") (row 100 100 100 100 100) (col 100 100 100 100 100) (origin 0 0))',
            '        (item (name "VG25X2") (gtype "Via") (row 2500 2500) (col 200 200) (origin 0 0))',
            '        (item (name "PG25") (gtype "Place") (row 2500) (col 2500) (origin 0 0))',
            '        (item (name "OG100") (gtype "Other") (row 10000) (col 10000) (origin 0 0))',
            "    )",
            ")",
        ]
    )


def emit_layermanager(board: BoardModel) -> str:
    lines = ["(layermanager", "(stackup"]
    lines.extend(
        [
            '    (layer "" (ltype "Surface") (material "AIR") (thickness "0mil") (isnegtive "false") (dielectricconst "1.000000") (stacking ""))'
        ]
    )
    for idx, layer in enumerate(board.layers):
        ltype = "Plane" if layer.kind == "power" else "Conductor"
        lines.append(
            f'    (layer "{layer.txt_name}" (ltype "{ltype}") (material "COPPER") '
            f'(thickness "{layer.thickness_mil:.6f}mil") (isnegtive "{str(layer.negative).lower()}") '
            f'(dielectricconst "4.500000") (stacking ""))'
        )
        if idx != len(board.layers) - 1:
            lines.append(
                '    (layer "" (ltype "Dielectric") (material "FR-4") (thickness "8.000000mil") '
                '(isnegtive "false") (dielectricconst "4.500000") (stacking "Core"))'
            )
    lines.append(")")
    lines.append(")")
    return "\n".join(lines)


def emit_connectivity_nets(board: BoardModel) -> str:
    lines = ["(nets"]
    for net_name in board.all_net_names():
        lines.append(f'    (net "{net_name}"')
        for ref, pin in board.connectivity_nets.get(net_name, []):
            lines.append(f'        (comp "{ref}" (pin "{pin}"))')
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def merge_connectivity_nets_with_donor(board: BoardModel, donor_nets_text: Optional[str]) -> str:
    if not donor_nets_text:
        return emit_connectivity_nets(board)
    try:
        donor_node = parse_sexpr(donor_nets_text)
    except Exception:
        return emit_connectivity_nets(board)
    if not isinstance(donor_node, list) or node_head(donor_node) != "nets":
        return emit_connectivity_nets(board)

    donor_by_name: Dict[str, List[Node]] = {}
    for net_node in child_nodes(donor_node, "net"):
        if len(net_node) >= 2 and isinstance(net_node[1], str):
            donor_by_name[net_node[1]] = net_node

    merged_entries: List[str] = []
    for net_name in board.all_net_names():
        donor_net = donor_by_name.get(net_name)
        if donor_net is None:
            lines = [f'(net "{net_name}"']
            for ref, pin in board.connectivity_nets.get(net_name, []):
                lines.append(f'    (comp "{ref}" (pin "{pin}"))')
            lines.append(")")
            merged_entries.append("\n".join(lines))
            continue

        new_children: List[Node] = ["net", net_name]
        for child in child_nodes(donor_net):
            if node_head(child) == "comp":
                continue
            new_children.append(child)
        for ref, pin in board.connectivity_nets.get(net_name, []):
            new_children.append(["comp", ref, ["pin", pin]])
        merged_entries.append(emit_sexpr(new_children, 4))

    return rebuild_root("nets", merged_entries)


def emit_constraint_nets(
    board: BoardModel,
    prop_source: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    lines = ["(nets"]
    for net_name in board.all_net_names():
        lines.append(f'    (net "{net_name}"')
        props = dict((prop_source or board.constraint_props).get(net_name, {}))
        if looks_like_power_ground_net(net_name):
            props.setdefault("PHYSICAL_CONSTRAINT_SET", "POWER")
            if "GND" in net_name.upper():
                props.setdefault("PHYSICAL_CONSTRAINT_SET_GND", "GND")
        if props:
            lines.append("        (props")
            for prop_name, prop_value in props.items():
                if prop_name == "PHYSICAL_CONSTRAINT_SET_GND":
                    lines.append(f'            (propname "PHYSICAL_CONSTRAINT_SET" propvalue "{prop_value}")')
                else:
                    lines.append(f'            (propname "{prop_name}" propvalue "{prop_value}")')
            lines.append("        )")
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def emit_vias_txt(
    board: BoardModel,
    padstack_map: Dict[Tuple[str, str, int, int, int, str], str],
    donor_via_choices: Optional[Dict[Tuple[int, int], List[DonorViaChoice]]] = None,
) -> str:
    lines = ["(vias"]
    all_copper_layers = tuple(layer.txt_name for layer in board.layers if layer.kind in {"signal", "power"})
    quantized_specs: set[Tuple[int, int, str]] = set()
    for via in board.vias:
        connected_layers = via.layers
        if connected_layers in {("Top", "Bottom"), ("Bottom", "Top")} and len(all_copper_layers) >= 2:
            connected_layers = all_copper_layers
        geom = PadGeometry(
            shape="circle",
            size_x=via.size,
            size_y=via.size,
            drill=via.drill,
            pad_type="thru_hole",
            side="Through",
            connected_layers=connected_layers,
        )
        key = (geom.shape, geom.pad_type, geom.size_x, geom.size_y, geom.drill, geom.side)
        donor_choice = pick_donor_via_choice(via, connected_layers, donor_via_choices or {})
        if donor_choice is not None:
            padstack_name = donor_choice.padstack_name
            connected_layers = donor_choice.layers
            if donor_choice.size != via.size or donor_choice.drill != via.drill:
                quantized_specs.add((via.size, via.drill, donor_choice.padstack_name))
        else:
            padstack_name = padstack_map[key]
        lines.append("    (via")
        lines.append(f'        (net "{via.net}")')
        lines.append(f"        (xy {via.x} {via.y})")
        lines.append(f"        (rotation {int(round(via.rotation_deg * 100))})")
        lines.append(f'        (mirrored "{str(via.mirrored).lower()}")')
        lines.append('        (testpoint "")')
        lines.append(f'        (padstack "{padstack_name}"')
        lines.append("            (connection")
        for layer in connected_layers:
            lines.append(f'                (layer "{layer}" (paduse "connect"))')
        lines.append("            )")
        lines.append("        )")
        lines.append("        (props)")
        lines.append("    )")
    lines.append(")")
    if quantized_specs:
        for size, drill, padstack_name in sorted(quantized_specs):
            note = (
                f"Quantized via {size}/{drill} to donor padstack {padstack_name}"
            )
            if note not in board.notes:
                board.notes.append(note)
    return "\n".join(lines)


def emit_wires_txt(board: BoardModel) -> str:
    lines = ["(wires"]
    for wire in merge_board_wires_for_txt(board):
        lines.append("    (wire")
        lines.append(f'        (net "{wire.net}")')
        lines.append("        (path")
        lines.append('            (issamewidth "true")')
        for step in wire.steps:
            if step.kind == "line":
                lines.append("            (lineseg")
                lines.append(f"                (pt {step.x} {step.y})")
                lines.append(f"                (w {step.width})")
                lines.append("            )")
            else:
                lines.append("            (arcseg")
                lines.append(f"                (pt {step.x} {step.y})")
                lines.append(f"                (w {step.width})")
                lines.append(f"                (xy {step.cx} {step.cy})")
                lines.append(f"                (rotate {step.rotate or 'CCW'})")
                lines.append("            )")
        lines.append("            (props)")
        lines.append(f'            (layer "{wire.layer}")')
        lines.append("        )")
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def emit_polygons_txt(board: BoardModel) -> str:
    lines = ["(polygons"]
    for outline in merge_outline_paths(board.outlines):
        pts = flatten_steps(outline.steps, points_per_circle=32)
        if len(pts) < 2:
            continue
        lines.append("    (polygon")
        lines.append("        (path")
        for x, y in pts:
            lines.append(f"            (lineseg (pt {x} {y}) (w 0))")
        lines.append("            (props")
        lines.append('                (propname "FIXED_PRIVATE" propvalue t)')
        lines.append("            )")
        lines.append('            (layer "Boundary/All")')
        lines.append("        )")
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def emit_surfaces_txt(board: BoardModel) -> str:
    lines = ["(surfaces"]
    for zone in board.zones:
        txt_layer = zone_layer_to_surface_txt_layer(zone.layer)
        if not txt_layer or zone.net:
            continue
        lines.append("    (surface")
        lines.append('        (net "none")')
        lines.append("        (boundary")
        lines.append("            (path")
        for step in zone.boundary:
            if step.kind == "line":
                lines.append(f"                (lineseg (pt {step.x} {step.y}) (w {step.width}))")
            else:
                lines.append(
                    f"                (arcseg (pt {step.x} {step.y}) (w {step.width}) "
                    f"(xy {step.cx} {step.cy}) (rotate {step.rotate or 'CCW'}))"
                )
        lines.append("            )")
        lines.append("        )")
        if zone.holes:
            lines.append("        (voids")
            for hole in zone.holes:
                lines.append("            (void")
                lines.append("                (path")
                for step in hole:
                    if step.kind == "line":
                        lines.append(f"                    (lineseg (pt {step.x} {step.y}) (w {step.width}))")
                    else:
                        lines.append(
                            f"                    (arcseg (pt {step.x} {step.y}) (w {step.width}) "
                            f"(xy {step.cx} {step.cy}) (rotate {step.rotate or 'CCW'}))"
                        )
                lines.append("                )")
                lines.append("            )")
            lines.append("        )")
        else:
            lines.append("        (voids)")
        lines.append("        (props)")
        lines.append(f'        (layer "{txt_layer}")')
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def emit_conductives_txt(board: BoardModel) -> str:
    lines = ["(conductives"]
    for zone in board.zones:
        if not zone.net and zone_layer_to_surface_txt_layer(zone.layer):
            continue
        lines.append("    (surface")
        lines.append(f'        (net "{zone.net}")')
        lines.append("        (boundary")
        lines.append("            (path")
        for step in zone.boundary:
            if step.kind == "line":
                lines.append(f"                (lineseg (pt {step.x} {step.y}) (w {step.width}))")
            else:
                lines.append(
                    f"                (arcseg (pt {step.x} {step.y}) (w {step.width}) "
                    f"(xy {step.cx} {step.cy}) (rotate {step.rotate or 'CCW'}))"
                )
        lines.append("            )")
        lines.append("        )")
        if zone.holes:
            lines.append("        (voids")
            for hole in zone.holes:
                lines.append("            (void")
                lines.append("                (path")
                for step in hole:
                    if step.kind == "line":
                        lines.append(f"                    (lineseg (pt {step.x} {step.y}) (w {step.width}))")
                    else:
                        lines.append(
                            f"                    (arcseg (pt {step.x} {step.y}) (w {step.width}) "
                            f"(xy {step.cx} {step.cy}) (rotate {step.rotate or 'CCW'}))"
                        )
                lines.append("                )")
                lines.append("            )")
            lines.append("        )")
        else:
            lines.append("        (voids)")
        lines.append("        (props)")
        lines.append(f'        (layer "{zone.layer}")')
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def merge_conductives_with_donor(board: BoardModel, donor_conductives_text: Optional[str]) -> str:
    if not donor_conductives_text:
        return emit_conductives_txt(board)
    try:
        donor_node = parse_sexpr(donor_conductives_text)
    except Exception:
        return emit_conductives_txt(board)
    if not isinstance(donor_node, list) or node_head(donor_node) != "conductives":
        return emit_conductives_txt(board)

    allowed_nets = set(board.all_net_names())
    kept_children: List[str] = []
    for child in child_nodes(donor_node):
        if node_head(child) != "surface":
            kept_children.append(emit_sexpr(child))
            continue
        net_name = child_text(child, "net", "")
        if not net_name or net_name.lower() == "none" or net_name in allowed_nets:
            kept_children.append(emit_sexpr(child))
    return rebuild_root("conductives", kept_children)


def emit_components_txt(
    board: BoardModel,
    padstack_map: Dict[Tuple[str, str, int, int, int, str], str],
    *,
    local_pin_coordinates: bool = False,
    minimal_footprint_figures: bool = False,
    include_footprint_texts: bool = True,
    include_origin_paths: bool = True,
) -> str:
    lines = ["(components"]
    for comp in board.components:
        lines.append(f'    (component "{comp.ref}"')
        lines.append(f'        (part "{comp.part}")')
        lines.append(f"        (xy {comp.x} {comp.y})")
        lines.append('        (isplaced "true")')
        lines.append(f"        (rotation {int(round(comp.rotation_deg * 100))})")
        lines.append(f'        (mirrored "{str(comp.mirrored).lower()}")')
        lines.append(f'        (footprint "{comp.footprint}"')
        lines.append('            (ftype "Package")')
        lines.append("            (pins")
        pin_points: List[Tuple[int, int, int, int]] = []
        for pin in comp.pins:
            key = (
                pin.geometry.shape,
                pin.geometry.pad_type,
                pin.geometry.size_x,
                pin.geometry.size_y,
                pin.geometry.drill,
                pin.geometry.side,
            )
            padstack_name = padstack_map[key]
            px = pin.x
            py = pin.y
            prot = pin.rotation_deg
            if local_pin_coordinates:
                px, py = component_pin_local_xy(comp, pin)
                prot = pad_rotation_local(comp, pin)
            pin_points.append((px, py, pin.geometry.size_x, pin.geometry.size_y))
            lines.append("                (pin")
            lines.append(f'                    (number "{pin.number}")')
            lines.append(f"                    (xy {px} {py})")
            lines.append(f"                    (rotation {int(round(prot * 100))})")
            lines.append(f'                    (testpoint "{"" if not pin.is_testpoint else pin.number}")')
            lines.append(f'                    (padstack "{padstack_name}"')
            lines.append("                        (connection")
            for layer in pin.geometry.connected_layers or (( "Bottom",) if comp.mirrored else ("Top",)):
                lines.append(f'                            (layer "{layer}" (paduse "connect"))')
            lines.append("                        )")
            lines.append("                    )")
            lines.append("                    (props)")
            lines.append("                )")
        lines.append("            )")
        lines.append(
            emit_sexpr(
                footprint_figure_node(
                    pin_points,
                    mirrored=comp.mirrored,
                    minimal=minimal_footprint_figures,
                    include_texts=include_footprint_texts,
                    include_origin_paths=include_origin_paths,
                ),
                12,
            )
        )
        lines.append("        )")
        lines.append("        (props)")
        lines.append("    )")
    lines.append(")")
    return "\n".join(lines)


def default_constraint_section(
    board: BoardModel,
    prop_source: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    return rebuild_root(
        "constraint",
        [
            "(dicts)",
            "(design)",
            "(physical)",
            "(spacing)",
            "(samenet)",
            "(layout)",
            "(netclasses)",
            "(netgroups)",
            "(diffs)",
            "(enets)",
            emit_constraint_nets(board, prop_source),
            "(pins)",
            "(pinpairs)",
            "(regions)",
            "(matchgroups)",
            "(clsclses)",
            "(rgnclses)",
            "(rgnclsclses)",
        ],
    )


def constraint_referenced_net_names(node: Node) -> set[str]:
    refs: set[str] = set()
    if not isinstance(node, list):
        return refs
    head = node_head(node)
    if head == "net" and len(node) >= 2 and isinstance(node[1], str):
        refs.add(node[1])
    elif head == "otype" and len(node) >= 4 and isinstance(node[1], str) and node[1].lower() == "net":
        for idx in range(2, len(node) - 1):
            if node[idx] == "name" and isinstance(node[idx + 1], str):
                refs.add(node[idx + 1])
    for child in child_nodes(node):
        refs.update(constraint_referenced_net_names(child))
    return refs


def prune_constraint_refs(node: Node, allowed_nets: set[str], is_root: bool = False) -> Optional[Node]:
    if not isinstance(node, list):
        return node
    if not is_root:
        refs = constraint_referenced_net_names(node)
        if refs and not refs.issubset(allowed_nets):
            return None
    pruned: List[Node] = [node[0]]
    for child in node[1:]:
        if isinstance(child, list):
            child_pruned = prune_constraint_refs(child, allowed_nets)
            if child_pruned is None:
                continue
            pruned.append(child_pruned)
        else:
            pruned.append(child)
    return pruned


def merge_constraint_with_donor(
    board: BoardModel,
    donor_constraint_text: Optional[str],
    prop_source: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    if not donor_constraint_text:
        return default_constraint_section(board)
    allowed_nets = set(board.all_net_names())
    try:
        donor_node = parse_sexpr(donor_constraint_text)
    except Exception:
        return default_constraint_section(board)
    if not isinstance(donor_node, list) or node_head(donor_node) != "constraint":
        return default_constraint_section(board)
    merged: List[str] = []
    replaced = False
    for child in child_nodes(donor_node):
        if node_head(child) == "nets":
            merged.append(emit_constraint_nets(board, prop_source))
            replaced = True
        else:
            pruned_child = prune_constraint_refs(child, allowed_nets)
            if pruned_child is not None:
                merged.append(emit_sexpr(pruned_child))
    if not replaced:
        merged.append(emit_constraint_nets(board, prop_source))
    return rebuild_root("constraint", merged)


def merge_rule_core_constraint_with_donor(
    board: BoardModel,
    donor_constraint_text: Optional[str],
    prop_source: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    retained: Dict[str, str] = {}
    if donor_constraint_text:
        try:
            donor_node = parse_sexpr(donor_constraint_text)
        except Exception:
            donor_node = None
        if isinstance(donor_node, list) and node_head(donor_node) == "constraint":
            for child in child_nodes(donor_node):
                head = node_head(child)
                if head in {"design", "physical", "spacing", "samenet", "layout"}:
                    retained[head] = emit_sexpr(child)
    merged: List[str] = []
    for head in PSEUDO_DONOR_CONSTRAINT_ORDER:
        if head == "nets":
            merged.append(emit_constraint_nets(board, prop_source))
        elif head in {"design", "physical", "spacing", "samenet", "layout"}:
            merged.append(retained.get(head, f"({head})"))
        else:
            merged.append(f"({head})")
    return rebuild_root("constraint", merged)


def merge_pseudo_donor_constraint_with_donor(
    board: BoardModel,
    donor_constraint_text: Optional[str],
    prop_source: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    baseline = pseudo_donor_safe_constraint_children()
    merged: List[str] = []
    for head in PSEUDO_DONOR_CONSTRAINT_ORDER:
        if head == "nets":
            merged.append(emit_constraint_nets(board, prop_source))
        elif head in {"design", "physical", "spacing", "samenet", "layout"}:
            merged.append(baseline.get(head, f"({head})"))
        else:
            merged.append(f"({head})")
    return rebuild_root("constraint", merged)


def donor_layout_sections(text: Optional[str]) -> Dict[str, RawSection]:
    if not text:
        return {}
    try:
        return {sec.name: sec for sec in extract_root_children(text, "layout")}
    except Exception:
        return {}


@lru_cache(maxsize=16)
def pseudo_donor_template_sections(template_path_hint: str = "") -> Dict[str, RawSection]:
    template_path = Path(template_path_hint) if template_path_hint else PSEUDO_DONOR_TEMPLATE
    if not template_path.is_absolute():
        template_path = Path(__file__).resolve().parent / template_path
    if not template_path.is_file():
        return {}
    try:
        return donor_layout_sections(template_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


@lru_cache(maxsize=1)
def pseudo_donor_safe_static_template_text() -> Optional[str]:
    sections = pseudo_donor_template_sections("")
    layermanager = sections.get("layermanager")
    if layermanager is None:
        return None
    return layermanager.text


@lru_cache(maxsize=1)
def pseudo_donor_safe_constraint_children() -> Dict[str, str]:
    template_path = PSEUDO_DONOR_SAFE_RULE_CORE_TEMPLATE
    if not template_path.is_absolute():
        template_path = Path(__file__).resolve().parent / template_path
    if not template_path.is_file():
        return {}
    sections = donor_layout_sections(template_path.read_text(encoding="utf-8", errors="replace"))
    constraint = sections.get("constraint")
    if constraint is None:
        return {}
    try:
        node = parse_sexpr(constraint.text)
    except Exception:
        return {}
    if not isinstance(node, list) or node_head(node) != "constraint":
        return {}
    return {
        head: emit_sexpr(child)
        for child in child_nodes(node)
        if (head := node_head(child)) is not None
    }


@lru_cache(maxsize=16)
def pseudo_donor_template_copper_layers(template_path_hint: str = "") -> Tuple[str, ...]:
    sections = pseudo_donor_template_sections(template_path_hint)
    layermanager = sections.get("layermanager")
    if layermanager is None:
        return ()
    try:
        lm_node = parse_sexpr(layermanager.text)
        stackup_node = next(child_nodes(lm_node, "stackup"), None)
        if stackup_node is None:
            return ()
        names: List[str] = []
        for layer_node in child_nodes(stackup_node, "layer"):
            if len(layer_node) < 2 or not isinstance(layer_node[1], str):
                continue
            name = layer_node[1]
            if name:
                names.append(name)
        return tuple(names)
    except Exception:
        return ()


def resolved_dbu_value(explicit: int, setup_default: int, filename_mil: float) -> int:
    if explicit > 0:
        return explicit
    if setup_default > 0:
        return setup_default
    if filename_mil > 0:
        return mil_to_dbu(filename_mil)
    return 0


def rename_txt_layer_value(value: str, aliases: Dict[str, str]) -> str:
    if not aliases:
        return value
    exact = aliases.get(value)
    if exact is not None:
        return exact
    for old_name, new_name in aliases.items():
        suffix = "/" + old_name
        if value.endswith(suffix):
            return value[: -len(suffix)] + "/" + new_name
    return value


def apply_txt_layer_aliases_to_board(board: BoardModel, aliases: Dict[str, str]) -> None:
    if not aliases:
        return
    for layer in board.layers:
        layer.txt_name = aliases.get(layer.txt_name, layer.txt_name)
    for comp in board.components:
        for pin in comp.pins:
            pin.geometry.connected_layers = tuple(aliases.get(name, name) for name in pin.geometry.connected_layers)
    for via in board.vias:
        via.layers = tuple(aliases.get(name, name) for name in via.layers)
    for wire in board.wires:
        wire.layer = rename_txt_layer_value(wire.layer, aliases)
    for zone in board.zones:
        zone.layer = rename_txt_layer_value(zone.layer, aliases)


def donor_board_compatible_with_target(board: BoardModel, donor_board: Optional[BoardModel]) -> bool:
    if donor_board is None:
        return False
    if len(board.layers) != len(donor_board.layers):
        return False
    board_refs = {comp.ref for comp in board.components if comp.ref}
    donor_refs = {comp.ref for comp in donor_board.components if comp.ref}
    if board_refs and donor_refs:
        return board_refs == donor_refs
    return False


def layer_alias_map(source_names: Sequence[str], target_names: Sequence[str]) -> Dict[str, str]:
    if len(source_names) != len(target_names):
        return {}
    return {
        current: target
        for current, target in zip(source_names, target_names)
        if current != target
    }


def align_board_layers_to_donor_stackup(board: BoardModel, donor_board: Optional[BoardModel]) -> Dict[str, str]:
    if donor_board is None:
        return {}
    board_layers = tuple(layer.txt_name for layer in board.layers)
    donor_layers = tuple(layer.txt_name for layer in donor_board.layers)
    if not board_layers or len(board_layers) != len(donor_layers):
        return {}
    aliases = layer_alias_map(board_layers, donor_layers)
    if not aliases:
        return {}
    apply_txt_layer_aliases_to_board(board, aliases)
    note = "Aligned donor-mode layer names to donor stackup"
    if note not in board.notes:
        board.notes.append(note)
    return aliases


def rewrite_txt_layer_aliases_in_node(node: Node, aliases: Dict[str, str]) -> Node:
    if isinstance(node, list):
        return [rewrite_txt_layer_aliases_in_node(child, aliases) for child in node]
    if isinstance(node, str):
        return rename_txt_layer_value(node, aliases)
    return node


def rewrite_txt_layer_aliases_in_text(text: Optional[str], aliases: Dict[str, str]) -> Optional[str]:
    if not text or not aliases:
        return text
    try:
        node = parse_sexpr(text)
    except Exception:
        return text
    rewritten = rewrite_txt_layer_aliases_in_node(node, aliases)
    return emit_sexpr(rewritten) + "\n"


def canonical_pseudo_donor_copper_layers(board: BoardModel) -> Tuple[str, ...]:
    canonical = PSEUDO_DONOR_CANONICAL_STACKUPS.get(len(board.layers), ())
    if canonical:
        return canonical
    return tuple(layer.txt_name for layer in board.layers)


def normalize_pseudo_donor_board_layers(board: BoardModel, pseudo_donor_template_path: Optional[Path]) -> Dict[str, str]:
    template_layers = canonical_pseudo_donor_copper_layers(board)
    used_canonical = template_layers in PSEUDO_DONOR_CANONICAL_STACKUPS.values()
    if len(template_layers) != len(board.layers):
        template_layers = pseudo_donor_template_copper_layers(str(pseudo_donor_template_path or ""))
        used_canonical = False
    board_layers = tuple(layer.txt_name for layer in board.layers)
    if not template_layers or len(template_layers) != len(board_layers):
        return {}
    aliases = {
        current: target
        for current, target in zip(board_layers, template_layers)
        if current != target
    }
    if not aliases:
        return {}
    apply_txt_layer_aliases_to_board(board, aliases)
    if used_canonical:
        note = "Aligned pseudo-donor layer names to canonical stackup"
    else:
        note = "Aligned pseudo-donor layer names to template stackup"
    if note not in board.notes:
        board.notes.append(note)
    return aliases


def dynamic_display_layer_name(name: str) -> bool:
    return name.startswith(PSEUDO_DONOR_DYNAMIC_LAYER_PREFIXES)


def make_display_layer_node(name: str, visible: str, color: str, pattern: str = "default") -> List[Node]:
    return ["layer", name, ["visible", visible], ["color", color], ["pattern", pattern]]


def inner_layer_color(index: int) -> str:
    return INNER_LAYER_COLORS[index % len(INNER_LAYER_COLORS)]


def inner_signal_layer_color(index: int) -> str:
    return str(3 + (index % 8))


def inner_layer_visible(layer: LayerSpec) -> str:
    return "false" if layer.kind == "power" else "true"


def inner_layer_family_color(layer: LayerSpec, index: int, signal_color: str, power_color: str) -> str:
    return power_color if layer.kind == "power" else signal_color


def pseudo_donor_static_layer_nodes(template_text: Optional[str]) -> List[List[Node]]:
    if template_text:
        try:
            lm_node = parse_sexpr(template_text)
            layers_node = next(child_nodes(lm_node, "layers"), None)
            if layers_node is not None:
                static_nodes: List[List[Node]] = []
                for layer_node in child_nodes(layers_node, "layer"):
                    name = layer_node[1] if len(layer_node) >= 2 and isinstance(layer_node[1], str) else ""
                    if not name or dynamic_display_layer_name(name):
                        continue
                    static_nodes.append(layer_node)
                if static_nodes:
                    return static_nodes
        except Exception:
            pass
    return [make_display_layer_node(name, visible, color) for name, visible, color in PSEUDO_DONOR_MINIMAL_LAYERS]


def make_stackup_layer_node(
    name: str,
    ltype: str,
    material: str,
    thickness_text: str,
    dielectricconst: str,
    stacking: str,
) -> List[Node]:
    return [
        "layer",
        name,
        ["ltype", ltype],
        ["material", material],
        ["thickness", thickness_text],
        ["isnegtive", "false"],
        ["dielectricconst", dielectricconst],
        ["stacking", stacking],
    ]


def pseudo_donor_stackup_node(board: BoardModel) -> List[Node]:
    stackup: List[Node] = [
        "stackup",
        make_stackup_layer_node("", "Surface", "AIR", "0mil", "1.000000", ""),
    ]
    for idx, layer in enumerate(board.layers):
        ltype = "Plane" if layer.kind == "power" else "Conductor"
        thickness_mil = layer.thickness_mil if layer.thickness_mil > 0 else (1.4 if idx in {0, len(board.layers) - 1} else 0.6)
        stackup.append(
            make_stackup_layer_node(
                layer.txt_name,
                ltype,
                "COPPER",
                f"{thickness_mil:.6f}mil",
                "4.500000",
                "",
            )
        )
        if idx != len(board.layers) - 1:
            dielectric_kind = "Prepreg" if idx % 2 == 0 else "Core"
            stackup.append(
                make_stackup_layer_node("", "Dielectric", "FR-4", "8.000000mil", "4.500000", dielectric_kind)
            )
    stackup.append(make_stackup_layer_node("", "Surface", "AIR", "0mil", "1.000000", ""))
    return stackup


def pseudo_donor_dynamic_layer_nodes(board: BoardModel) -> List[List[Node]]:
    nodes: List[List[Node]] = [
        make_display_layer_node("Conductor/Top", "true", "22"),
    ]
    for idx, layer in enumerate(board.layers[1:-1]):
        nodes.append(
            make_display_layer_node(
                f"Conductor/{layer.txt_name}",
                inner_layer_visible(layer),
                inner_layer_family_color(layer, idx, inner_signal_layer_color(idx), "24"),
            )
        )
    nodes.append(make_display_layer_node("Conductor/Bottom", "true", "21"))
    nodes.extend(
        [
            make_display_layer_node("Route Area/Top", "false", "1"),
            make_display_layer_node("Route Area/Bottom", "false", "1"),
            make_display_layer_node("Route Area/All", "false", "10"),
            make_display_layer_node("Slot/Top", "false", "57"),
            make_display_layer_node("Slot/Bottom", "false", "17"),
            make_display_layer_node("Slot/All", "false", "1"),
        ]
    )
    for layer in board.layers[1:-1]:
        nodes.append(make_display_layer_node(f"Route Area/{layer.txt_name}", "false", "1"))
        nodes.append(make_display_layer_node(f"Slot/{layer.txt_name}", "false", "24"))
    nodes.extend(
        [
            make_display_layer_node("Boundary/Top", "false", "12"),
            make_display_layer_node("Boundary/Bottom", "false", "12"),
            make_display_layer_node("Boundary/All", "false", "24"),
            make_display_layer_node("Pin/Top", "true", "22"),
            make_display_layer_node("Pin/Bottom", "true", "21"),
            make_display_layer_node("Pin/Resist_Top", "false", "23"),
            make_display_layer_node("Pin/Resist_Bottom", "false", "23"),
            make_display_layer_node("Pin/Stencil_Top", "false", "11"),
            make_display_layer_node("Pin/Stencil_Bottom", "false", "11"),
            make_display_layer_node("Pin/Inner_Default", "false", "1"),
        ]
    )
    for idx, layer in enumerate(board.layers[1:-1]):
        nodes.append(make_display_layer_node(f"Boundary/{layer.txt_name}", "false", "12"))
        nodes.append(
            make_display_layer_node(
                f"Pin/{layer.txt_name}",
                inner_layer_visible(layer),
                inner_layer_family_color(layer, idx, inner_signal_layer_color(idx), "24"),
            )
        )
    nodes.extend(
        [
            make_display_layer_node("Via/Top", "true", "4"),
            make_display_layer_node("Via/Bottom", "true", "4"),
            make_display_layer_node("Via/Resist_Top", "false", "23"),
            make_display_layer_node("Via/Resist_Bottom", "false", "23"),
            make_display_layer_node("Via/Stencil_Top", "false", "24"),
            make_display_layer_node("Via/Stencil_Bottom", "false", "24"),
            make_display_layer_node("Via/Inner_Default", "false", "1"),
            make_display_layer_node("Split/Top", "false", "24"),
            make_display_layer_node("Split/Bottom", "false", "24"),
            make_display_layer_node("Split/All", "false", "14"),
            make_display_layer_node("Rule Area/Top", "false", "44"),
            make_display_layer_node("Rule Area/Bottom", "false", "48"),
            make_display_layer_node("Rule Area/All", "false", "48"),
            make_display_layer_node("Inhibit Route/Top", "false", "35"),
            make_display_layer_node("Inhibit Route/Bottom", "false", "35"),
            make_display_layer_node("Inhibit Route/All", "false", "35"),
            make_display_layer_node("Inhibit Via/Top", "false", "35"),
            make_display_layer_node("Inhibit Via/Bottom", "false", "35"),
            make_display_layer_node("Inhibit Via/All", "false", "35"),
            make_display_layer_node("Drc/Top", "true", "8"),
            make_display_layer_node("Drc/Bottom", "true", "8"),
            make_display_layer_node("Drc/All", "true", "19"),
            make_display_layer_node("Drc/Placement_Top", "false", "19"),
            make_display_layer_node("Drc/Placement_Bottom", "false", "19"),
            make_display_layer_node("Drc/Resist_Top", "false", "1"),
            make_display_layer_node("Drc/Resist_Bottom", "false", "1"),
            make_display_layer_node("Drc/Stencil_Top", "false", "1"),
            make_display_layer_node("Drc/Stencil_Bottom", "false", "1"),
            make_display_layer_node("Drc/Assembly_Top", "false", "1"),
            make_display_layer_node("Drc/Assembly_Bottom", "false", "1"),
            make_display_layer_node("Drc/Silkscreen_Top", "false", "1"),
            make_display_layer_node("Drc/Silkscreen_Bottom", "false", "1"),
            make_display_layer_node("Planning/Top", "true", "57"),
            make_display_layer_node("Planning/Bottom", "true", "17"),
            make_display_layer_node("Planning/All", "true", "24"),
        ]
    )
    for idx, layer in enumerate(board.layers[1:-1]):
        nodes.append(make_display_layer_node(f"Via/{layer.txt_name}", inner_layer_visible(layer), "4"))
        nodes.append(make_display_layer_node(f"Split/{layer.txt_name}", "false", "24"))
        nodes.append(make_display_layer_node(f"Rule Area/{layer.txt_name}", "false", "24"))
        nodes.append(make_display_layer_node(f"Inhibit Route/{layer.txt_name}", "false", "24"))
        nodes.append(make_display_layer_node(f"Inhibit Via/{layer.txt_name}", "false", "24"))
        nodes.append(make_display_layer_node(f"Drc/{layer.txt_name}", inner_layer_visible(layer), "8"))
        nodes.append(
            make_display_layer_node(
                f"Planning/{layer.txt_name}",
                inner_layer_visible(layer),
                inner_layer_family_color(layer, idx, "24", "24"),
            )
        )
    return nodes


def layers_ref_node(names: Sequence[str]) -> List[Node]:
    return ["layers", *[["layer", name] for name in names]]


def layerset_node(name: str, layers: Sequence[str]) -> List[Node]:
    return ["layerset", name, *[["layer", layer] for layer in layers]]


def pseudo_donor_layersets_node(board: BoardModel) -> List[Node]:
    nodes: List[Node] = [
        "layersets",
        layerset_node(
            "all",
            [
                "Pin/Top",
                "Pin/Bottom",
                "Cell/Silkscreen_Top",
                "Layout/Silkscreen_Top",
                "Cell/Silkscreen_Bottom",
                "Layout/Silkscreen_Bottom",
                "Layout/Panel_Outline",
            ],
        ),
        layerset_node("adt", ["Pin/Top", "Part RefDes/Silkscreen_Top", "Cell/Silkscreen_Top", "Layout/Panel_Outline"]),
        layerset_node("adb", ["Pin/Bottom", "Part RefDes/Silkscreen_Bottom", "Cell/Silkscreen_Bottom", "Layout/Panel_Outline"]),
    ]
    for layer in board.layers:
        nodes.append(
            layerset_node(
                layer.txt_name.lower(),
                [f"Via/{layer.txt_name}", f"Pin/{layer.txt_name}", f"Conductor/{layer.txt_name}"],
            )
        )
    nodes.extend(
        [
            layerset_node(
                "silktop",
                ["Part RefDes/Silkscreen_Top", "Cell/Silkscreen_Top", "Layout/Silkscreen_Top", "Layout/Panel_Outline"],
            ),
            layerset_node(
                "silkbot",
                ["Part RefDes/Silkscreen_Bottom", "Cell/Silkscreen_Bottom", "Layout/Silkscreen_Bottom", "Layout/Panel_Outline"],
            ),
            layerset_node(
                "soldtop",
                ["Via/Resist_Top", "Pin/Resist_Top", "Cell/Resist_Top", "Layout/Resist_Top", "Layout/Panel_Outline"],
            ),
            layerset_node(
                "soldbot",
                ["Via/Resist_Bottom", "Pin/Resist_Bottom", "Cell/Resist_Bottom", "Layout/Resist_Bottom", "Layout/Panel_Outline"],
            ),
            layerset_node("pasttop", ["Pin/Stencil_Top", "Cell/Stencil_Top", "Layout/Panel_Outline"]),
            layerset_node("pastbot", ["Pin/Stencil_Bottom", "Cell/Stencil_Bottom", "Layout/Panel_Outline"]),
        ]
    )
    return nodes


def stackbar_node(name: str, layers: Sequence[str]) -> List[Node]:
    return ["stackbar", name, layers_ref_node(layers), ["visible", "true"]]


def pseudo_donor_stackbars_node(board: BoardModel) -> List[Node]:
    nodes: List[Node] = [
        "stackbars",
        stackbar_node(
            "Top",
            [
                "Layout/Panel_Outline",
                "Layout/Layout_Outline",
                "Layout/Silkscreen_Top",
                "Cell/Silkscreen_Top",
                "Part RefDes/Silkscreen_Top",
                "Pin/Top",
                "Via/Top",
                "Conductor/Top",
            ],
        ),
    ]
    for layer in board.layers[1:-1]:
        nodes.append(
            stackbar_node(
                layer.txt_name,
                [
                    "Layout/Layout_Outline",
                    "Route Area/All",
                    f"Route Area/{layer.txt_name}",
                    f"Pin/{layer.txt_name}",
                    f"Via/{layer.txt_name}",
                    f"Conductor/{layer.txt_name}",
                ],
            )
        )
    nodes.append(
        stackbar_node(
            "Bottom",
            [
                "Layout/Panel_Outline",
                "Layout/Layout_Outline",
                "Layout/Silkscreen_Bottom",
                "Cell/Silkscreen_Bottom",
                "Part RefDes/Silkscreen_Bottom",
                "Pin/Bottom",
                "Via/Bottom",
                "Conductor/Bottom",
            ],
        )
    )
    return nodes


def emit_pseudo_donor_layermanager(board: BoardModel, pseudo_donor_template_path: Optional[Path] = None) -> str:
    template_text = pseudo_donor_safe_static_template_text()
    return emit_template_informed_layermanager(board, template_text)


def emit_template_informed_layermanager(board: BoardModel, template_text: Optional[str]) -> str:
    layers_node: List[Node] = ["layers"]
    layers_node.extend(pseudo_donor_static_layer_nodes(template_text))
    layers_node.extend(pseudo_donor_dynamic_layer_nodes(board))
    layermanager = [
        "layermanager",
        pseudo_donor_stackup_node(board),
        layers_node,
        pseudo_donor_layersets_node(board),
        pseudo_donor_stackbars_node(board),
    ]
    return emit_sexpr(layermanager) + "\n"


def apply_bga_pseudo_donor_defaults(board: BoardModel, source_path: Path) -> bool:
    params = parse_bga_dataset_kicad_stem(source_path.stem)
    if params is None:
        return False

    board.default_trace_width = resolved_dbu_value(board.default_trace_width, board.default_trace_width, params["trace_width_mil"])
    board.default_trace_clearance = resolved_dbu_value(board.default_trace_clearance, board.default_trace_clearance, params["trace_clearance_mil"])
    board.default_via_size = resolved_dbu_value(board.default_via_size, board.default_via_size, params["via_size_mil"])
    board.default_via_drill = resolved_dbu_value(board.default_via_drill, board.default_via_drill, params["via_drill_mil"])

    recovered_vias = 0
    for via in board.vias:
        resolved_size = resolved_dbu_value(via.size, board.default_via_size, params["via_size_mil"])
        resolved_drill = resolved_dbu_value(via.drill, board.default_via_drill, params["via_drill_mil"])
        if via.size != resolved_size and resolved_size > 0:
            via.size = resolved_size
            recovered_vias += 1
        if via.drill != resolved_drill and resolved_drill > 0:
            via.drill = resolved_drill
            recovered_vias += 1

    filled_widths = 0
    for wire in board.wires:
        for step in wire.steps:
            if step.width <= 0 and board.default_trace_width > 0:
                step.width = board.default_trace_width
                filled_widths += 1

    if "pseudo-donor:bga" not in board.notes:
        board.notes.append("pseudo-donor:bga")
    if recovered_vias:
        board.notes.append(f"Recovered BGA via tech on {recovered_vias} fields from setup/filename")
    if filled_widths:
        board.notes.append(f"Recovered {filled_widths} missing wire widths from setup/filename")
    return True


def pseudo_donor_sections_for_board(
    board: BoardModel,
    source_path: Optional[Path],
    pseudo_donor_template_path: Optional[Path] = None,
) -> Dict[str, RawSection]:
    if source_path is None or not apply_bga_pseudo_donor_defaults(board, source_path):
        return {}
    normalize_pseudo_donor_board_layers(board, pseudo_donor_template_path)
    template_sections = pseudo_donor_template_sections(str(pseudo_donor_template_path or ""))
    pseudo_sections: Dict[str, RawSection] = {}
    for name in ("Pcb-Design_Version", "grids", "props", "materials", "textblocks", "colors", "constraint"):
        if name in template_sections:
            pseudo_sections[name] = template_sections[name]
    pseudo_sections["texts"] = RawSection("texts", "(texts)")
    pseudo_sections["paths"] = RawSection("paths", "(paths)")
    return pseudo_sections


def collect_layermanager_declared_layers(layermanager_text: str) -> set[str]:
    try:
        node = parse_sexpr(layermanager_text)
        layers_node = next(child_nodes(node, "layers"), None)
        if layers_node is None:
            return set()
        return {
            layer[1]
            for layer in child_nodes(layers_node, "layer")
            if len(layer) >= 2 and isinstance(layer[1], str)
        }
    except Exception:
        return set()


def collect_board_used_display_layers(board: BoardModel) -> set[str]:
    used: set[str] = set()
    for wire in board.wires:
        used.add(wire.layer)
    for via in board.vias:
        used.update({f"Via/{layer}" for layer in via.layers})
    for zone in board.zones:
        txt_layer = zone_layer_to_surface_txt_layer(zone.layer)
        if txt_layer:
            used.add(txt_layer)
        else:
            used.add(zone.layer)
    return used


def validate_pseudo_donor_declared_layers(board: BoardModel, layermanager_text: str) -> List[str]:
    declared_layers = collect_layermanager_declared_layers(layermanager_text)
    used_layers = collect_board_used_display_layers(board)
    return sorted(layer for layer in used_layers if layer not in declared_layers)


def shift_layout_geometry(node: Node, dx: int, dy: int) -> None:
    if not isinstance(node, list):
        return
    head = node_head(node)
    if head in {"xy", "pt", "lowerleft"} and len(node) >= 3:
        try:
            node[1] = str(int(round(float(node[1]))) + dx)
            node[2] = str(int(round(float(node[2]))) + dy)
        except Exception:
            pass
    for child in node[1:]:
        shift_layout_geometry(child, dx, dy)


def trim_pseudo_donor_constraint(node: Node) -> None:
    if not isinstance(node, list) or node_head(node) != "constraint":
        return
    for idx in range(1, len(node)):
        child = node[idx]
        if isinstance(child, list) and child and child[0] in {"pins", "pinpairs", "matchgroups"}:
            node[idx] = [child[0]]


def postprocess_pseudo_donor_layout(layout_text: str, board: BoardModel) -> str:
    try:
        root = parse_sexpr(layout_text)
    except Exception:
        return layout_text
    center_x = board.lowerleft_x + board.width // 2
    center_y = board.lowerleft_y + board.height // 2
    shift_layout_geometry(root, -center_x, -center_y)
    for child in root[1:]:
        trim_pseudo_donor_constraint(child)
    return emit_sexpr(root) + "\n"


def emit_txt(
    board: BoardModel,
    donor_txt_path: Optional[Path],
    source_path: Optional[Path] = None,
    pseudo_donor_template_path: Optional[Path] = None,
) -> str:
    donor_text = donor_txt_path.read_text(encoding="utf-8", errors="replace") if donor_txt_path and donor_txt_path.is_file() else None
    donor_sections = donor_layout_sections(donor_text)
    donor_board: Optional[BoardModel] = None
    pseudo_donor_mode = False
    incompatible_donor_mode = False
    if donor_txt_path and donor_txt_path.is_file():
        try:
            donor_board = parse_txt_board(donor_txt_path)
        except Exception:
            donor_board = None
        incompatible_donor_mode = donor_board is not None and not donor_board_compatible_with_target(board, donor_board)
    elif source_path is not None:
        pseudo_sections = pseudo_donor_sections_for_board(
            board,
            source_path,
            pseudo_donor_template_path=pseudo_donor_template_path,
        )
        if pseudo_sections:
            donor_sections = pseudo_sections
            pseudo_donor_mode = True
    donor_layer_aliases_to_target: Dict[str, str] = {}
    if incompatible_donor_mode and donor_board is not None:
        donor_layer_aliases_to_target = layer_alias_map(
            tuple(layer.txt_name for layer in donor_board.layers),
            tuple(layer.txt_name for layer in board.layers),
        )
    library_text, padstack_map = build_generated_library(
        board,
        minimal_footprint_figures=False,
        include_footprint_texts=not pseudo_donor_mode,
        include_origin_paths=not pseudo_donor_mode,
    )
    donor_via_choices = build_donor_via_padstack_choices(donor_board if not incompatible_donor_mode else None)
    constraint_props = donor_constraint_props_for_board(board, None if incompatible_donor_mode else donor_board)
    if incompatible_donor_mode:
        note = "Donor board mismatched target; using donor static sections only"
        if note not in board.notes:
            board.notes.append(note)
    sections: List[str] = []
    sections.append(donor_sections.get("Pcb-Design_Version", RawSection("Pcb-Design_Version", '(Pcb-Design_Version "PCB Builder V1.0")')).text)
    sections.append(donor_sections.get("parameters", RawSection("parameters", emit_txt_parameters(board))).text)
    sections.append(donor_sections.get("grids", RawSection("grids", emit_default_grids())).text)
    sections.append(donor_sections.get("props", RawSection("props", "(props)")).text)
    if pseudo_donor_mode:
        layermanager_text = emit_pseudo_donor_layermanager(
            board,
            pseudo_donor_template_path=pseudo_donor_template_path,
        )
        missing_layers = validate_pseudo_donor_declared_layers(board, layermanager_text)
        if missing_layers:
            raise ValueError(f"Pseudo-donor layermanager missing declared layers: {', '.join(missing_layers)}")
        sections.append(layermanager_text)
    else:
        if incompatible_donor_mode:
            template_layermanager = donor_sections.get("layermanager").text if "layermanager" in donor_sections else None
            layermanager_text = emit_template_informed_layermanager(board, template_layermanager)
        else:
            layermanager_text = donor_sections.get("layermanager", RawSection("layermanager", emit_layermanager(board))).text
        if incompatible_donor_mode:
            missing_layers = sorted(
                layer
                for layer in collect_board_used_display_layers(board)
                if layer not in collect_layermanager_declared_layers(layermanager_text)
            )
            if missing_layers:
                raise ValueError(f"Donor layermanager missing declared layers: {', '.join(missing_layers)}")
        sections.append(layermanager_text)
    sections.append(donor_sections.get("materials", RawSection("materials", "(materials)")).text)
    sections.append(donor_sections.get("textblocks", RawSection("textblocks", "(textblocks)")).text)
    sections.append(donor_sections.get("colors", RawSection("colors", "(colors)")).text)
    sections.append(
        donor_sections.get("surfaces", RawSection("surfaces", emit_surfaces_txt(board))).text
        if not incompatible_donor_mode
        else emit_surfaces_txt(board)
    )
    sections.append(
        donor_sections.get("texts", RawSection("texts", "(texts)")).text
        if not incompatible_donor_mode
        else "(texts)"
    )
    sections.append(emit_vias_txt(board, padstack_map, donor_via_choices))
    sections.append(
        donor_sections.get("paths", RawSection("paths", "(paths)")).text
        if not incompatible_donor_mode
        else "(paths)"
    )
    sections.append(emit_wires_txt(board))
    sections.append(
        donor_sections.get("polygons", RawSection("polygons", emit_polygons_txt(board))).text
        if not incompatible_donor_mode
        else emit_polygons_txt(board)
    )
    sections.append(
        merge_conductives_with_donor(
            board,
            donor_sections.get("conductives").text if "conductives" in donor_sections else None,
        )
        if not incompatible_donor_mode
        else emit_conductives_txt(board)
    )
    sections.append(
        donor_sections.get(
            "components",
            RawSection(
                "components",
                emit_components_txt(
                    board,
                    padstack_map,
                    local_pin_coordinates=False,
                    minimal_footprint_figures=False,
                    include_footprint_texts=not pseudo_donor_mode,
                    include_origin_paths=not pseudo_donor_mode,
                ),
            ),
        ).text
        if not incompatible_donor_mode
        else emit_components_txt(
            board,
            padstack_map,
            local_pin_coordinates=False,
            minimal_footprint_figures=False,
            include_footprint_texts=not pseudo_donor_mode,
            include_origin_paths=not pseudo_donor_mode,
        )
    )
    sections.append(
        merge_connectivity_nets_with_donor(
            board,
            donor_sections.get("nets").text if "nets" in donor_sections and not incompatible_donor_mode else None,
        )
    )
    sections.append(
        (
            merge_pseudo_donor_constraint_with_donor(
                board,
                donor_sections.get("constraint").text if "constraint" in donor_sections else None,
                constraint_props,
            )
            if pseudo_donor_mode
            else merge_rule_core_constraint_with_donor(
                board,
                rewrite_txt_layer_aliases_in_text(
                    donor_sections.get("constraint").text if "constraint" in donor_sections else None,
                    donor_layer_aliases_to_target,
                ),
                constraint_props,
            )
            if incompatible_donor_mode
            else merge_constraint_with_donor(
                board,
                donor_sections.get("constraint").text if "constraint" in donor_sections else None,
                constraint_props,
            )
        )
    )
    sections.append(
        donor_sections.get("library", RawSection("library", library_text)).text
        if not incompatible_donor_mode
        else library_text
    )
    layout_text = rebuild_root("layout", sections)
    if pseudo_donor_mode:
        return postprocess_pseudo_donor_layout(layout_text, board)
    return layout_text


def infer_default_donor_dir(mode: str, input_path: Path) -> Optional[Path]:
    if mode == "kicad_to_txt":
        path_str = input_path.as_posix()
        if "kicad/outline_only/exclude_missing" in path_str:
            return input_path.parents[3] / "txt" / "exclude_missing" if input_path.is_file() else input_path.parents[2] / "txt" / "exclude_missing"
        if "kicad/outline_only" in path_str:
            return input_path.parents[2] / "txt" if input_path.is_file() else input_path.parents[1] / "txt"
        return None
    if mode == "txt_to_kicad":
        path_str = input_path.as_posix()
        if "/txt/exclude_missing" in path_str:
            base = input_path.parents[2] if input_path.is_file() else input_path.parents[1]
            return base / "kicad" / "outline_only" / "exclude_missing"
        if "/txt/" in path_str or input_path.parent.name == "txt" or input_path.name == "txt":
            if input_path.is_dir() and input_path.name == "txt":
                base = input_path.parent
            else:
                base = input_path.parent.parent
            return base / "kicad" / "outline_only"
    return None


def donor_candidate_stems(stem: str) -> List[str]:
    normalized = stem.replace(".default", "").replace(".outline_only", "")
    candidates: List[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    add(normalized)
    current = normalized
    suffix_patterns = [
        r"_(?:incomplete|complete|completed|filled|restored|fixed|pred|predicted)(?:_\d+)?$",
    ]
    while True:
        updated = current
        for pattern in suffix_patterns:
            updated = re.sub(pattern, "", updated, flags=re.IGNORECASE)
        if updated == current or not updated:
            break
        add(updated)
        current = updated
    return candidates


def matching_donor_file(mode: str, donor_dir: Optional[Path], stem: str) -> Optional[Path]:
    if donor_dir is None:
        return None
    if mode == "kicad_to_txt":
        suffix = ".txt"
    else:
        suffix = ".kicad_pcb"
    for candidate_stem in donor_candidate_stems(stem):
        candidate = donor_dir / f"{candidate_stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def discover_inputs(input_path: Path, suffix: str) -> List[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(p for p in input_path.iterdir() if p.is_file() and p.name.endswith(suffix))


def output_path_for(mode: str, output_dir: Path, input_file: Path) -> Path:
    stem = input_file.stem
    if mode == "txt_to_kicad":
        return output_dir / f"{stem}.kicad_pcb"
    if stem.endswith(".default") or stem.endswith(".outline_only"):
        stem = stem.rsplit(".", 1)[0]
    return output_dir / f"{stem}.txt"


def convert_one(
    mode: str,
    input_file: Path,
    output_dir: Path,
    donor_dir: Optional[Path],
    pseudo_donor_template_path: Optional[Path] = None,
) -> Dict[str, Any]:
    donor_file = matching_donor_file(mode, donor_dir, input_file.stem.replace(".default", "").replace(".outline_only", ""))
    if mode == "txt_to_kicad":
        board = parse_txt_board(input_file)
        output_text = write_kicad(board)
    else:
        board = parse_kicad_board(input_file)
        output_text = emit_txt(
            board,
            donor_file,
            source_path=input_file,
            pseudo_donor_template_path=pseudo_donor_template_path,
        )
    out_path = output_path_for(mode, output_dir, input_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output_text, encoding="utf-8")
    return {
        "input": str(input_file),
        "output": str(out_path),
        "donor": str(donor_file) if donor_file else "",
        "pseudo_donor_template": str(pseudo_donor_template_path) if pseudo_donor_template_path else "",
        "net_count": len(board.all_net_names()),
        "component_count": len(board.components),
        "wire_count": len(board.wires),
        "zone_count": len(board.zones),
        "via_count": len(board.vias),
        "notes": board.notes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert between KiCad boards and Allegro-like txt layout files.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for cmd, suffix in (("txt_to_kicad", ".txt"), ("kicad_to_txt", ".kicad_pcb")):
        sub = subparsers.add_parser(cmd)
        sub.add_argument("input", help=f"Input {suffix} file or directory")
        sub.add_argument("--output-dir", required=True, help="Output directory")
        sub.add_argument("--donor-dir", default="", help="Optional donor directory used to preserve static sections")
        sub.add_argument(
            "--pseudo-donor-template",
            default="",
            help="Optional txt template used for pseudo-donor mode when no donor file is matched",
        )
        sub.add_argument("--report-json", default="", help="Optional path to write conversion summary JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    mode = args.command
    suffix = ".txt" if mode == "txt_to_kicad" else ".kicad_pcb"
    donor_dir = Path(args.donor_dir) if args.donor_dir else infer_default_donor_dir(mode, input_path)
    pseudo_donor_template_path = Path(args.pseudo_donor_template) if args.pseudo_donor_template else None

    inputs = discover_inputs(input_path, suffix)
    if not inputs:
        print(f"No input files with suffix {suffix} found in {input_path}", file=sys.stderr)
        return 1

    results: List[Dict[str, Any]] = []
    failures = 0
    for input_file in inputs:
        try:
            results.append(
                convert_one(
                    mode,
                    input_file,
                    output_dir,
                    donor_dir,
                    pseudo_donor_template_path=pseudo_donor_template_path,
                )
            )
        except Exception as exc:  # noqa: BLE001 - want per-file resilience in batch mode
            failures += 1
            results.append(
                {
                    "input": str(input_file),
                    "output": "",
                    "donor": "",
                    "pseudo_donor_template": str(pseudo_donor_template_path) if pseudo_donor_template_path else "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    if args.report_json:
        report_path = Path(args.report_json)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "mode": mode,
                    "input": str(input_path),
                    "output_dir": str(output_dir),
                    "donor_dir": str(donor_dir) if donor_dir else "",
                    "pseudo_donor_template": str(pseudo_donor_template_path) if pseudo_donor_template_path else "",
                    "total": len(inputs),
                    "failures": failures,
                    "results": results,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if failures:
        print(f"Completed with {failures} failure(s).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
