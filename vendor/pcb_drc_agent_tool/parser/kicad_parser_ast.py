from pathlib import Path
from typing import Dict, List, Tuple
import math
import re
import time

from model.board import Board, Net, Pad, Via, Segment


# =========================================================
# 基础工具
# =========================================================

def parse_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def parse_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def unquote(s):
    if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s


def rotate_point(x: float, y: float, angle_deg: float) -> Tuple[float, float]:
    if abs(angle_deg) < 1e-12:
        return (x, y)
    a = math.radians(angle_deg)
    c = math.cos(a)
    s = math.sin(a)
    return (x * c - y * s, x * s + y * c)


# =========================================================
# S-Expression tokenizer / parser
# =========================================================

def tokenize(text: str) -> List[str]:
    """
    把 KiCad S-expression 文本切成 token。
    支持：
    - 括号
    - 普通原子
    - 带引号字符串
    """
    tokens = []
    token = []
    in_string = False
    escape = False

    for ch in text:
        if in_string:
            token.append(ch)

            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                tokens.append("".join(token))
                token = []

            continue

        if ch == '"':
            if token:
                tokens.append("".join(token))
                token = []
            token.append(ch)
            in_string = True
            continue

        if ch in ("(", ")"):
            if token:
                tokens.append("".join(token))
                token = []
            tokens.append(ch)
            continue

        if ch.isspace():
            if token:
                tokens.append("".join(token))
                token = []
            continue

        token.append(ch)

    if token:
        tokens.append("".join(token))

    return tokens


def parse_sexpr(tokens: List[str]):
    if not tokens:
        raise ValueError("Unexpected EOF while parsing")

    token = tokens.pop(0)

    if token == "(":
        out = []
        while tokens and tokens[0] != ")":
            out.append(parse_sexpr(tokens))

        if not tokens:
            raise ValueError("Missing closing ')'")

        tokens.pop(0)  # consume ')'
        return out

    if token == ")":
        raise ValueError("Unexpected ')'")

    return token


def build_ast(text: str):
    tokens = tokenize(text)
    ast = []

    while tokens:
        ast.append(parse_sexpr(tokens))

    return ast


# =========================================================
# AST helper
# =========================================================

def is_list(x) -> bool:
    return isinstance(x, list)


def get_root_kicad_pcb(ast):
    for node in ast:
        if is_list(node) and len(node) > 0 and node[0] == "kicad_pcb":
            return node
    raise ValueError("No (kicad_pcb ...) root found")


def get_top_level_children(root, key: str):
    out = []
    for item in root:
        if is_list(item) and len(item) > 0 and item[0] == key:
            out.append(item)
    return out


def find_first(node, key: str):
    if not is_list(node):
        return None

    for item in node:
        if is_list(item) and len(item) > 0 and item[0] == key:
            return item

    return None


def get_atom(child, idx: int, default=None):
    if child is None:
        return default
    if len(child) > idx:
        return child[idx]
    return default


# =========================================================
# 解析 net
# 只取顶层 (net id "name")
# =========================================================

def parse_nets(root) -> List[Net]:
    nets = []

    for item in get_top_level_children(root, "net"):
        if len(item) < 3:
            continue

        net_id = parse_int(item[1], None)
        if net_id is None:
            continue

        name = unquote(item[2])
        nets.append(Net(id=net_id, name=name))

    # 去重，防止异常文件重复定义
    dedup = {}
    for n in nets:
        dedup[n.id] = n.name

    return [Net(id=k, name=v) for k, v in sorted(dedup.items())]


def build_net_map(nets: List[Net]) -> Dict[int, str]:
    return {n.id: n.name for n in nets}


# =========================================================
# 解析 segment
# 只取顶层 segment
# =========================================================

def parse_segments(root, net_map: Dict[int, str]) -> List[Segment]:
    segments = []

    for i, node in enumerate(get_top_level_children(root, "segment")):
        start_node = find_first(node, "start")
        end_node = find_first(node, "end")
        width_node = find_first(node, "width")
        layer_node = find_first(node, "layer")
        net_node = find_first(node, "net")

        if not start_node or not end_node or not net_node:
            continue

        start = (
            parse_float(get_atom(start_node, 1)),
            parse_float(get_atom(start_node, 2)),
        )
        end = (
            parse_float(get_atom(end_node, 1)),
            parse_float(get_atom(end_node, 2)),
        )
        width = parse_float(get_atom(width_node, 1), 0.0)
        layer = unquote(get_atom(layer_node, 1, "UNKNOWN"))

        net_id = parse_int(get_atom(net_node, 1), 0)
        net_name = net_map.get(net_id, "")

        segments.append(
            Segment(
                id=f"SEG_{i}",
                net=net_name,
                layer=layer,
                width=width,
                start=start,
                end=end,
            )
        )

    return segments


# =========================================================
# 解析 via
# 支持：
#   (via ...)
#   (via blind ...)
#   (via micro ...)
# =========================================================

def parse_vias(root, net_map: Dict[int, str]) -> List[Via]:
    vias = []

    for i, node in enumerate(get_top_level_children(root, "via")):
        via_kind = "THROUGH"

        # KiCad 5 里可能是:
        # ['via', 'micro', ...]
        # ['via', 'blind', ...]
        if len(node) > 1 and isinstance(node[1], str) and node[1] in ("micro", "blind"):
            via_kind = node[1].upper()

        at_node = find_first(node, "at")
        drill_node = find_first(node, "drill")
        size_node = find_first(node, "size")
        net_node = find_first(node, "net")
        layers_node = find_first(node, "layers")

        if not at_node or not net_node:
            continue

        x = parse_float(get_atom(at_node, 1))
        y = parse_float(get_atom(at_node, 2))
        drill = parse_float(get_atom(drill_node, 1), 0.0)
        size= parse_float(get_atom(size_node, 1), 0.0)

        start_layer = ""
        end_layer = ""
        if layers_node and len(layers_node) >= 3:
            start_layer = unquote(layers_node[1])
            end_layer = unquote(layers_node[2])

        net_id = parse_int(get_atom(net_node, 1), 0)
        net_name = net_map.get(net_id, "")

        vias.append(
            Via(
                id=f"VIA_{i}",
                net=net_name,
                x=x,
                y=y,
                drill=drill,
                size=size,
                type=via_kind,
                start_layer=start_layer,
                end_layer=end_layer,
            )
        )

    return vias


# =========================================================
# BGA 相关辅助
# =========================================================

def _extract_component_ref(fp_node) -> str:
    """
    KiCad 5:
      (fp_text reference U1 ...)
    KiCad 6/7:
      (property "Reference" "U1" ...)
    """
    for item in fp_node:
        if is_list(item) and len(item) >= 3 and item[0] == "fp_text" and item[1] == "reference":
            return unquote(item[2])

    for item in fp_node:
        if is_list(item) and len(item) >= 3 and item[0] == "property":
            if unquote(item[1]) == "Reference":
                return unquote(item[2])

    return "UNKNOWN"


def _extract_package_name(fp_node) -> str:
    """
    module / footprint 第二个原子通常是封装名
    例如:
      (module BGA_BENCHMARK ...)
      (footprint "Package_BGA:UFBGA-64" ...)
    """
    if len(fp_node) >= 2 and isinstance(fp_node[1], str):
        return unquote(fp_node[1])
    return ""

def _infer_bga_from_pad_names(fp_node) -> bool:
    """
    如果 footprint 中有大量 pad 名符合 BGA 命名规则，
    即使封装名不含 BGA，也判定为 BGA 类封装。
    """
    count = 0
    matched = 0

    for item in fp_node:
        if not (is_list(item) and len(item) > 1 and item[0] == "pad"):
            continue

        count += 1
        pad_name = unquote(get_atom(item, 1, ""))
        if _looks_like_bga_pad_name(pad_name):
            matched += 1

    if count == 0:
        return False

    # 经验规则：至少 16 个 pad，且 70% 以上符合 BGA 命名
    return count >= 16 and matched / count >= 0.7

def _is_bga_package(name: str) -> bool:
    name_u = name.upper()
    bga_keywords = [
        "BGA",
        "FBGA",
        "FCBGA",
        "FFG",
    ]
    return any(k in name_u for k in bga_keywords)
#def _is_bga_package(package_name: str) -> bool:
#    return "BGA" in package_name.upper()


def _extract_fp_at(fp_node) -> Tuple[float, float, float]:
    """
    footprint/module 的板级位置和旋转：
      (at x y [angle])
    """
    at_node = find_first(fp_node, "at")
    if not at_node:
        return 0.0, 0.0, 0.0

    x = parse_float(get_atom(at_node, 1), 0.0)
    y = parse_float(get_atom(at_node, 2), 0.0)
    angle = parse_float(get_atom(at_node, 3), 0.0)
    return x, y, angle


def _parse_bga_pin_name(pad_name: str):
    """
    A1, B3, AA12 -> (row, col)
    """
    i = 0
    while i < len(pad_name) and pad_name[i].isalpha():
        i += 1

    if i == 0 or i == len(pad_name):
        return None, None

    row = pad_name[:i]

    try:
        col = int(pad_name[i:])
    except Exception:
        return None, None

    return row, col

def _looks_like_bga_pad_name(pad_name: str) -> bool:
    """
    典型BGA pad命名:
      A1, B3, AA12, AB30
    """
    return re.match(r"^[A-Za-z]+[0-9]+$", pad_name) is not None

# =========================================================
# 解析 pad
# 关键：把局部 pad 坐标转成板级坐标
# =========================================================

def parse_pads(root, net_map: Dict[int, str]) -> List[Pad]:
    pads = []

    # KiCad 5 常见是 module，兼容 footprint
    fps = get_top_level_children(root, "module") + get_top_level_children(root, "footprint")

    for fp_node in fps:
        comp_ref = _extract_component_ref(fp_node)
        #package_name = _extract_package_name(fp_node)
        #is_bga = _is_bga_package(package_name)
        package_name = _extract_package_name(fp_node)
        is_bga = _is_bga_package(package_name) or _infer_bga_from_pad_names(fp_node)

        fp_x, fp_y, fp_angle = _extract_fp_at(fp_node)

        for item in fp_node:
            if not (is_list(item) and len(item) > 0 and item[0] == "pad"):
                continue

            # KiCad 5 pad 头通常是:
            # (pad A1 smd circle ...)
            pad_name = unquote(get_atom(item, 1, ""))

            pad_shape = ""
            if len(item) >= 4 and isinstance(item[3], str):
                pad_shape = item[3]

            at_node = find_first(item, "at")
            size_node = find_first(item, "size")
            layers_node = find_first(item, "layers")
            net_node = find_first(item, "net")

            if not at_node:
                continue

            local_x = parse_float(get_atom(at_node, 1), 0.0)
            local_y = parse_float(get_atom(at_node, 2), 0.0)
        

            # pad 自身角度先不单独参与几何点计算，
            # 这里只需要 pad 中心点，所以只用 footprint 旋转即可
            rx, ry = rotate_point(local_x, local_y, fp_angle)
            x = fp_x + rx
            y = fp_y + ry
            size_x = 0.0
            size_y = 0.0
            if size_node and len(size_node) >= 3:
                size_x = parse_float(get_atom(size_node, 1), 0.0)
                size_y = parse_float(get_atom(size_node, 2), 0.0)

            layer = unquote(get_atom(layers_node, 1, "UNKNOWN"))

            net_name = ""
            if net_node and len(net_node) >= 2:
                net_id = parse_int(get_atom(net_node, 1), 0)
                net_name = net_map.get(net_id, "")

            bga_row = None
            bga_col = None
            if is_bga:
                bga_row, bga_col = _parse_bga_pin_name(pad_name)

            pads.append(
                Pad(
                    id=f"{comp_ref}.{pad_name}",
                    net=net_name,
                    x=x,
                    y=y,
                    layer=layer,
                    component=comp_ref,
                    is_bga=is_bga,
                    bga_row=bga_row,
                    bga_col=bga_col,
                    size_x=size_x,
                    size_y=size_y,
                    shape=pad_shape,
                )
            )

    return pads


# =========================================================
# 主入口
# =========================================================

def parse_kicad(path: str) -> Board:
    t0 = time.perf_counter()
    print("[PARSER] read file: start")
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    print(f"[PARSER] read file: done in {time.perf_counter() - t0:.3f}s, chars={len(text)}")

    t1 = time.perf_counter()
    print("[PARSER] build_ast: start")
    ast = build_ast(text)
    print(f"[PARSER] build_ast: done in {time.perf_counter() - t1:.3f}s")

    t2 = time.perf_counter()
    print("[PARSER] get_root: start")
    root = get_root_kicad_pcb(ast)
    print(f"[PARSER] get_root: done in {time.perf_counter() - t2:.3f}s")

    board = Board()

    t3 = time.perf_counter()
    print("[PARSER] parse_nets: start")
    board.nets = parse_nets(root)
    net_map = build_net_map(board.nets)
    print(f"[PARSER] parse_nets: done in {time.perf_counter() - t3:.3f}s, nets={len(board.nets)}")

    t4 = time.perf_counter()
    print("[PARSER] parse_pads: start")
    board.pads = parse_pads(root, net_map)
    print(f"[PARSER] parse_pads: done in {time.perf_counter() - t4:.3f}s, pads={len(board.pads)}")

    t5 = time.perf_counter()
    print("[PARSER] parse_vias: start")
    board.vias = parse_vias(root, net_map)
    print(f"[PARSER] parse_vias: done in {time.perf_counter() - t5:.3f}s, vias={len(board.vias)}")

    t6 = time.perf_counter()
    print("[PARSER] parse_segments: start")
    board.segments = parse_segments(root, net_map)
    print(f"[PARSER] parse_segments: done in {time.perf_counter() - t6:.3f}s, segments={len(board.segments)}")

    print(f"[PARSER] total: {time.perf_counter() - t0:.3f}s")
    return board