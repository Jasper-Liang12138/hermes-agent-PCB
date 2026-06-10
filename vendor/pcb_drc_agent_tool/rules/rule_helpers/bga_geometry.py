from collections import defaultdict
from typing import Dict

from rules.rule_helpers.board_filters import _get_all_bga_pads


def _row_to_index(row: str) -> int:
    """将 BGA 行字母转换为数字索引，例如 A->1, B->2, ..., Z->26, AA->27 等。"""
    if not row:
        return -1
    row = row.upper()
    value = 0
    for ch in row:
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"Invalid BGA row: {row}")
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value


def _group_bga_pads_by_component(board):
    """将 BGA pad 按照所属组件进行分组，返回一个字典，键是组件名称，值是该组件下的 BGA pad 列表。"""
    groups = defaultdict(list)
    for pad in _get_all_bga_pads(board):
        if not pad.component:
            continue
        groups[pad.component].append(pad)
    return groups


def _build_bga_component_meta(board):
    """构建 BGA 组件的元信息，包括行列范围等，用于判断内外层 pad。返回一个字典，键是组件名称，值是一个包含 row_min, row_max, col_min, col_max 的字典。"""
    meta = {}

    groups = _group_bga_pads_by_component(board)
    for comp, pads in groups.items():
        row_indices = []
        col_indices = []

        for p in pads:
            if p.bga_row is None or p.bga_col is None:
                continue
            try:
                row_idx = _row_to_index(p.bga_row)
            except ValueError:
                 continue
            row_indices.append(row_idx)
            col_indices.append(p.bga_col)

        if not row_indices or not col_indices:
            continue

        row_min = min(row_indices)
        row_max = max(row_indices)
        col_min = min(col_indices)
        col_max = max(col_indices)

        meta[comp] = {
            "row_min": row_min,
            "row_max": row_max,
            "col_min": col_min,
            "col_max": col_max,
        }

    return meta


def _is_outer_pad(pad, bga_meta: Dict) -> bool:
    """判断一个 BGA pad 是否是外层 pad，即位于行列范围的边界上。"""
    if pad.component not in bga_meta:
        return False
    if pad.bga_row is None or pad.bga_col is None:
        return False
    
    try:
        row_idx = _row_to_index(pad.bga_row)
    except ValueError:
        return False
    
    col_idx = pad.bga_col
    m = bga_meta[pad.component]

    return (
        row_idx == m["row_min"]
        or row_idx == m["row_max"]
        or col_idx == m["col_min"]
        or col_idx == m["col_max"]
    )

"""
def _is_inner_pad(pad, bga_meta: Dict) -> bool:
    判断一个 BGA pad 是否是内层 pad，即不位于行列范围的边界上。
    return not _is_outer_pad(pad, bga_meta)
"""
# 为了更健壮地处理异常情况（例如行列信息缺失或格式错误），我们直接实现内层 pad 的判断逻辑，而不是简单地取反。
def _is_inner_pad(pad, bga_meta: Dict) -> bool:
    if pad.component not in bga_meta:
        return False
    if pad.bga_row is None or pad.bga_col is None:
        return False

    try:
        row_idx = _row_to_index(pad.bga_row)
    except ValueError:
        return False

    m = bga_meta[pad.component]

    return (
        m["row_min"] < row_idx < m["row_max"]
        and m["col_min"] < pad.bga_col < m["col_max"]
    )

def _pad_ring_level(pad, bga_meta: Dict):
    if pad.component not in bga_meta:
        return None
    if pad.bga_row is None or pad.bga_col is None:
        return None

    try:
        row_idx = _row_to_index(pad.bga_row)
    except ValueError:
        return None

    m = bga_meta[pad.component]

    return min(
        row_idx - m["row_min"],
        m["row_max"] - row_idx,
        pad.bga_col - m["col_min"],
        m["col_max"] - pad.bga_col,
    )

def _classify_pitch(pitch: float):
    if abs(pitch - 1.0) <= 0.08:
        return 1.0
    if abs(pitch - 0.8) <= 0.08:
        return 0.8
    if abs(pitch - 0.65) <= 0.06:
        return 0.65
    return None

def _estimate_bga_pitch_for_component(pads):
    xs = sorted({round(p.x, 4) for p in pads if p.bga_col is not None})
    ys = sorted({round(p.y, 4) for p in pads if p.bga_row is not None})

    dxs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > 1e-4]
    dys = [ys[i + 1] - ys[i] for i in range(len(ys) - 1) if ys[i + 1] - ys[i] > 1e-4]

    candidates = dxs + dys
    if not candidates:
        return None

    pitch = sum(candidates) / len(candidates)
    return _classify_pitch(pitch)

def _build_bga_pitch_map(board):
    groups = _group_bga_pads_by_component(board)
    pitch_map = {}

    for comp, pads in groups.items():
        pitch_map[comp] = _estimate_bga_pitch_for_component(pads)

    return pitch_map

def _build_pad_grid(board):
    grid = defaultdict(dict)
    for pad in _get_all_bga_pads(board):
        if pad.bga_row is None or pad.bga_col is None:
            continue
        grid[pad.component][(pad.bga_row, pad.bga_col)] = pad
    return grid

def _build_bga_cell_centers(board):
    grid = _build_pad_grid(board)
    centers = defaultdict(list)

    for comp, pad_map in grid.items():
        keys = list(pad_map.keys())

        for row, col in keys:
            p1 = pad_map.get((row, col))
            p2 = pad_map.get((row, col + 1))

            next_row = None
            for r, c in keys:
                if r != row and c == col:
                    if _row_to_index(r) == _row_to_index(row) + 1:
                        next_row = r
                        break

            if next_row is None:
                continue

            p3 = pad_map.get((next_row, col))
            p4 = pad_map.get((next_row, col + 1))

            if not (p1 and p2 and p3 and p4):
                continue

            cx = (p1.x + p2.x + p3.x + p4.x) / 4.0
            cy = (p1.y + p2.y + p3.y + p4.y) / 4.0
            centers[comp].append((cx, cy))

    return centers

def normalize_layer_name(layer: str) -> str:
    if not layer:
        return ""

    s = str(layer).strip().strip('"')

    if s in ("F.Cu", "Top", "TOP", "top"):
        return "Top"
    if s in ("B.Cu", "Bottom", "BOTTOM", "bottom"):
        return "Bottom"

    return s


def is_top_layer(layer: str) -> bool:
    return normalize_layer_name(layer) == "Top"