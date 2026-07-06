from pathlib import Path
from typing import Dict, List, Tuple
import math
import re

from model.board import Board, Net, Pad, Via, Segment


# =========================================================
# basic utils
# =========================================================

#浮点和整型的解析函数，遇到异常时返回默认值
def parse_float(x, default=0.0, log_fn=None):
    try:
        return float(x)
    except Exception as e:
        if log_fn:
            log_fn(f"Error parsing float from '{x}': {e}")
        return default


def parse_int(x, default=0, log_fn=None):
    try:
        return int(x)
    except Exception as e:
        if log_fn:
            log_fn(f"Error parsing int from '{x}': {e}")
        return default

#字符串去引号函数，如果字符串以双引号开头和结尾，则去掉引号，否则返回原字符串
def unquote(s):
    if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1]
    return s

#旋转点坐标的函数，接受点的x和y坐标以及旋转角度（以度为单位），返回旋转后的新坐标
def rotate_point(x: float, y: float, angle_deg: float) -> Tuple[float, float]:
    if abs(angle_deg) < 1e-12:
        return (x, y)
    a = math.radians(angle_deg)
    c = math.cos(a)
    s = math.sin(a)
    return (x * c - y * s, x * s + y * c)


# =========================================================
# block extractor
# =========================================================


def extract_blocks(text: str, key: str) -> List[str]:
    """
    Extract S-expression blocks by keyword, e.g.:
      (segment ...)
      (via ...)
      (module ...)
      (footprint ...)
      (pad ...)
    """
    pattern = re.compile(r'\(\s*' + re.escape(key) + r'\b') #匹配以左括号开头，后面跟着可选的空格和关键字，关键字后面必须是单词边界（即不能是另一个单词的一部分）
    blocks = []

    pos = 0
    n = len(text)

    while True:
        m = pattern.search(text, pos) #在文本中从位置pos开始搜索匹配pattern的第一个位置，返回一个Match对象，如果没有找到匹配项，则返回None
        if not m:
            break

        start = m.start()
        depth = 0
        i = start

        while i < n:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1

        blocks.append(text[start:i])
        pos = i

    return blocks


def extract_blocks_multi(text: str, keys: List[str]) -> Dict[str, List[str]]:
    """
    一次扫描文本，提取多个 key 对应的完整 S-expression block。
    例如 keys = ["segment", "via", "pad", "module", "footprint"]

    返回:
        {
            "segment": [...],
            "via": [...],
            ...
        }
    """
    key_set = set(keys)
    result = {k: [] for k in keys}

    n = len(text)
    i = 0

    while i < n:
        if text[i] != "(":
            i += 1
            continue

        # 跳过 '(' 后的空白
        j = i + 1
        while j < n and text[j].isspace():
            j += 1

        # 提取 keyword
        k = j
        while k < n and (text[k].isalnum() or text[k] in "._-"):
            k += 1

        key = text[j:k]

        # 如果不是目标 key，就继续向后扫
        if key not in key_set:
            i += 1
            continue

        # 用括号深度提取完整 block
        start = i
        depth = 0
        in_string = False
        escape = False

        while i < n:
            ch = text[i]

            # 处理字符串，避免字符串里的括号干扰
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break

            i += 1

        result[key].append(text[start:i])

    return result

# =========================================================
# nets
# =========================================================


def parse_nets_from_text(text: str) -> List[Net]: #解析网络信息，返回一个列表，每个元素是一个 Net 对象，包含网络的 id 和 name 等信息
    nets = []

    pattern = re.compile(
        r'\(\s*net\s+(\d+)\s+("([^"]*)"|([^\s\)]+))\s*\)'
    )

    for m in pattern.finditer(text):
        net_id = int(m.group(1))
        if m.group(3) is not None:
            net_name = m.group(3)   # quoted
        else:
            net_name = m.group(4)   # unquoted

        nets.append(Net(id=net_id, name=net_name))

    dedup = {}
    for n in nets:
        if n.id in dedup and dedup[n.id] != n.name:
            print(f"Warning: duplicate net id {n.id} with different names: '{dedup[n.id]}' vs '{n.name}'")
        dedup[n.id] = n.name

    return [Net(id=k, name=v) for k, v in sorted(dedup.items())]

def build_net_map(nets: List[Net]) -> Dict[int, str]:
    return {n.id: n.name for n in nets}



# =========================================================
# layers
# =========================================================

# 目前解析层信息主要是为了更准确地识别 pad/via/segment 的层，暂时不深入理解层的其他属性。
def parse_layer_table_from_text(text: str) -> Dict[str, Dict]:
    """
    Parse global (layers ...) table from KiCad PCB text.
    Returns several lookup maps for layer id / name / alias.
    """
    out = {
        "id_to_name": {},
        "name_to_id": {},
        "id_to_alias": {},
        "alias_to_id": {},
    }

    m = re.search(r'\(\s*layers\b(.*?)\)\s*\)', text, flags=re.DOTALL)
    if not m:
        return out

    body = m.group(1)

    row_pattern = re.compile(
        r'\(\s*(\d+)\s+("([^"]+)"|([^\s\)]+))\s+([^\s\)"]+)(?:\s+"([^"]+)")?\s*\)'
    )

    for mm in row_pattern.finditer(body):
        layer_id = int(mm.group(1))

        if mm.group(3) is not None:
            layer_name = mm.group(3)
        else:
            layer_name = mm.group(4)

        layer_type = mm.group(5)  # 先不一定要用
        layer_alias = mm.group(6) or ""

        out["id_to_name"][layer_id] = layer_name
        out["name_to_id"][layer_name] = layer_id

        if layer_alias:
            out["id_to_alias"][layer_id] = layer_alias
            out["alias_to_id"][layer_alias] = layer_id

    return out

def resolve_layer_id(layer_name: str, layer_table: dict) -> int:
    if not layer_name or not layer_table:
        return -1

    name_to_id = layer_table.get("name_to_id", {})
    alias_to_id = layer_table.get("alias_to_id", {})

    if layer_name in name_to_id:
        return name_to_id[layer_name]

    if layer_name in alias_to_id:
        return alias_to_id[layer_name]

    return -1

# =========================================================
# BGA helpers
# =========================================================
# BGA 的 pad 名称通常是字母+数字的组合，字母表示行，数字表示列。例如 A1 表示第一行第一列，B3 表示第二行第三列，AA12 表示第27行第12列，AB30 表示第28行第30列。
def _looks_like_bga_pad_name(pad_name: str) -> bool:
    """
    Strict BGA pad names:
      A1, B3, AA12, AB30

    Reject:
      null1, MH1, PAD1, TP1
    """
    if not pad_name:
        return False

    pad_name = pad_name.strip().upper()

    # 必须是 字母 + 数字
    m = re.match(r"^([A-Z]+)([0-9]+)$", pad_name)
    if not m:
        return False

    row = m.group(1)

    # ❗关键：排除常见非阵列前缀
    if row in {"NULL"}:
        return False

    return True


def _parse_bga_pin_name(pad_name: str):
    i = 0
    while i < len(pad_name) and pad_name[i].isalpha():
        i += 1

    if i == 0 or i == len(pad_name):
        return None, None

    row = pad_name[:i].upper()
    try:
        col = int(pad_name[i:])
    except Exception:
        return None, None

    return row, col


def _is_bga_package(name: str) -> bool:
    name_u = name.upper()
    bga_keywords = [
        "BGA",
        "FBGA",
        "FCBGA",
        "FFG",
    ]
    return any(k in name_u for k in bga_keywords)

# 有些库的 BGA 没有在封装名里明确标注，但 pad 名称又很像 BGA，这时可以通过 pad 名称来推断。
def _infer_bga_from_pad_names_block(block: str) -> bool:

    pad_blocks = extract_blocks_multi(block, "pad")
    count = 0
    matched = 0

    for pb in pad_blocks:
        m = re.search(r'\(\s*pad\s+([^\s\)"]+|"[^"]+")', pb)
        if not m:
            continue
        count += 1
        pad_name = m.group(1).strip('"')
        if _looks_like_bga_pad_name(pad_name):
            matched += 1

    if count == 0:
        return False

    return count >= 16 and matched / count >= 0.7

# =========================================================
# modules

def _extract_component_ref_from_block(block: str) -> str: #解析器件名字，不是封装名
    """
    KiCad 5:
      (fp_text reference U1 ...)
    KiCad 6/7:
      (property "Reference" "U1" ...)
    """
    m = re.search(
        r'\(\s*fp_text\s+reference\s+([^\s\)"]+|"[^"]+")',
        block,
        re.IGNORECASE
    )
    if m:
        return m.group(1).strip('"')

    m = re.search(
        r'\(\s*property\s+"Reference"\s+"([^"]+)"',
        block,
        re.IGNORECASE
    )
    if m:
        return m.group(1).strip('"')

    return "UNKNOWN"


def _extract_package_name_from_block(block: str) -> str: #解析封装名字
    """
    Matches:
      (module BGA_BENCHMARK ...)
      (footprint "Package_BGA:UFBGA-64" ...)
    """
    m = re.match(r'\(\s*(?:module|footprint)\s+([^\s\)"]+|"[^"]+")', block)
    if m:
        return m.group(1).strip('"')
    return ""


def _extract_fp_at_from_block(block: str): #解析器件位置和旋转角度
    """
    Extract footprint/module top-level (at x y [angle]).

    To reduce false matches inside inner objects, anchor near block head.
    """
    header_slice = block[:1200]
    m = re.search(r'\(\s*at\s+([-\d\.]+)\s+([-\d\.]+)(?:\s+([-\d\.]+))?', header_slice)
    if not m:
        return 0.0, 0.0, 0.0

    x = float(m.group(1))
    y = float(m.group(2))
    a = float(m.group(3)) if m.group(3) else 0.0
    return x, y, a

def _extract_fp_layer_from_block(block: str) -> str: #解析器件所在层，虽然大多数封装默认在顶层，但有些特殊封装可能在底层或者其他层，这个函数可以帮助识别。
    """
    Extract footprint/module top-level layer, e.g.
      (layer F.Cu)
      (layer Bottom)
    """
    header_slice = block[:1200]
    m = re.search(r'\(\s*layer\s+([^\s\)"]+|"[^"]+")', header_slice, re.IGNORECASE)
    if not m:
        return "UNKNOWN"

    return m.group(1).strip('"')

def parse_modules_from_text(text: str, layer_table: Dict[str, Dict]):#解析器件信息，返回一个列表，每个元素是一个字典，包含器件的 reference、package、位置和旋转角度等信息
    modules = []
    blocks = extract_blocks_multi(text, ["module", "footprint"])
    fp_blocks = blocks["module"] + blocks["footprint"]

    for block in fp_blocks:
        comp_ref = _extract_component_ref_from_block(block)
        package_name = _extract_package_name_from_block(block)
        fp_x, fp_y, fp_angle = _extract_fp_at_from_block(block)
        fp_layer = _extract_fp_layer_from_block(block)
        fp_layer_id = resolve_layer_id(fp_layer, layer_table)

        modules.append({
            "component": comp_ref,
            "package": package_name,
            "x": fp_x,
            "y": fp_y,
            "angle": fp_angle,
            "layer": fp_layer,
            "layer_id": fp_layer_id,
        })

    return modules

# =========================================================
# segments
# =========================================================

def parse_segments_from_text(
        text: str, 
        net_map: Dict[int, str],
        layer_table: Dict[str, Dict],
) -> List[Segment]: #解析线段信息，返回一个列表，每个元素是一个 Segment 对象，包含线段的起点、终点、宽度、所在层和所属网络等信息
    
    segments = []
    blocks = extract_blocks_multi(text, ["segment"])
    seg_blocks = blocks["segment"]

    for i, block in enumerate(seg_blocks):
        s = re.search(r'\(\s*start\s+([-\d\.]+)\s+([-\d\.]+)', block)
        e = re.search(r'\(\s*end\s+([-\d\.]+)\s+([-\d\.]+)', block)
        w = re.search(r'\(\s*width\s+([-\d\.]+)', block)
        l = re.search(r'\(\s*layer\s+([^\s\)"]+|"[^"]+")', block)
        n = re.search(r'\(\s*net\s+(\d+)', block)

        if not (s and e and n):
            continue

        start = (float(s.group(1)), float(s.group(2)))
        end = (float(e.group(1)), float(e.group(2)))
        width = float(w.group(1)) if w else 0.0
        layer_name = l.group(1).strip('"') if l else "UNKNOWN"
        layer_id = resolve_layer_id(layer_name, layer_table)
        net_id = int(n.group(1))
        net_name = net_map.get(net_id, "")

        segments.append(
            Segment(
                id=f"SEG_{i}",
                net=net_name,
                layer=layer_name,
                layer_id=layer_id,
                width=width,
                start=start,
                end=end,
            )
        )

    return segments


# =========================================================
# vias
# =========================================================

def parse_vias_from_text(text: str, net_map: Dict[int, str], layer_table: Dict[str, Dict]) -> List[Via]:
    vias = []

    blocks = extract_blocks_multi(text, ["via"])
    via_blocks = blocks["via"]

    for i, block in enumerate(via_blocks):
        via_kind = "THROUGH"
        if re.match(r'\(\s*via\s+blind\b', block):
            via_kind = "BLIND"
        elif re.match(r'\(\s*via\s+micro\b', block):
            via_kind = "MICRO"

        at_m = re.search(r'\(\s*at\s+([-\d\.]+)\s+([-\d\.]+)', block)
        drill_m = re.search(r'\(\s*drill\s+([-\d\.]+)', block)
        size_m = re.search(r'\(\s*size\s+([-\d\.]+)', block)
        net_m = re.search(r'\(\s*net\s+(\d+)', block)
        layers_m = re.search(
            r'\(\s*layers\s+([^\s\)"]+|"[^"]+")\s+([^\s\)"]+|"[^"]+")',
            block
        )

        if not (at_m and net_m):
            continue

        x = float(at_m.group(1))
        y = float(at_m.group(2))
        drill = float(drill_m.group(1)) if drill_m else 0.0
        size = float(size_m.group(1)) if size_m else 0.0
        net_id = int(net_m.group(1))
        net_name = net_map.get(net_id, "")

        start_layer = ""
        end_layer = ""
        start_layer_id = -1
        end_layer_id = -1
        if layers_m:
            start_layer = layers_m.group(1).strip('"')
            end_layer = layers_m.group(2).strip('"')
            start_layer_id = resolve_layer_id(start_layer, layer_table)
            end_layer_id = resolve_layer_id(end_layer, layer_table)

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
                start_layer_id=start_layer_id,
                end_layer_id=end_layer_id,
            )
        )

    return vias


# =========================================================
# pads
# =========================================================

def parse_pads_from_text(text: str, net_map: Dict[int, str], layer_table: Dict[str, Dict]) -> List[Pad]:
    pads = []
    blocks = extract_blocks_multi(text, ["module", "footprint"])
    fp_blocks = blocks["module"] + blocks["footprint"]


    for block in fp_blocks:
        comp_ref = _extract_component_ref_from_block(block)
        package_name = _extract_package_name_from_block(block)
        is_bga = _is_bga_package(package_name) or _infer_bga_from_pad_names_block(block)
        fp_x, fp_y, fp_angle = _extract_fp_at_from_block(block)

        pad_blocks = extract_blocks_multi(block, ["pad"])["pad"]

        for pb in pad_blocks:
            # KiCad 5 pad head usually:
            # (pad A1 smd circle ...)
            head = re.search(
                r'\(\s*pad\s+([^\s\)"]+|"[^"]+")\s+([^\s\)"]+|"[^"]+")\s+([^\s\)"]+|"[^"]+")',
                pb
            )
            if not head:
                continue

            pad_name = head.group(1).strip('"')
            pad_shape = head.group(3).strip('"')

            at_m = re.search(r'\(\s*at\s+([-\d\.]+)\s+([-\d\.]+)', pb)
            size_m = re.search(r'\(\s*size\s+([-\d\.]+)\s+([-\d\.]+)', pb)
            layers_m = re.search(r'\(\s*layers\s+([^\s\)"]+|"[^"]+")', pb)
            net_m = re.search(r'\(\s*net\s+(\d+)', pb)

            if not at_m:
                continue

            px = float(at_m.group(1))
            py = float(at_m.group(2))
            rx, ry = rotate_point(px, py, -fp_angle)
            x = fp_x + rx
            y = fp_y + ry

            size_x = float(size_m.group(1)) if size_m else 0.0
            size_y = float(size_m.group(2)) if size_m else 0.0
            layer_name = layers_m.group(1).strip('"') if layers_m else "UNKNOWN"
            layer_id = resolve_layer_id(layer_name, layer_table)

            net_name = ""
            if net_m:
                net_id = int(net_m.group(1))
                net_name = net_map.get(net_id, "")

            bga_row = None
            bga_col = None
            if is_bga and _looks_like_bga_pad_name(pad_name):
                bga_row, bga_col = _parse_bga_pin_name(pad_name)
            else:
                is_bga = False

            pads.append(
                Pad(
                    id=f"{comp_ref}.{pad_name}",
                    net=net_name,
                    x=x,
                    y=y,
                    layer=layer_name,
                    layer_id=layer_id,
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
# main parser entry
# =========================================================

def parse_kicad(path: str) -> Board:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")

    board = Board()

    board.nets = parse_nets_from_text(text)
    net_map = build_net_map(board.nets)

    board.layers_table = parse_layer_table_from_text(text)

    board.modules = parse_modules_from_text(text, board.layers_table)
    board.pads = parse_pads_from_text(text, net_map, board.layers_table)
    board.vias = parse_vias_from_text(text, net_map, board.layers_table)
    board.segments = parse_segments_from_text(text, net_map, board.layers_table)

    return board