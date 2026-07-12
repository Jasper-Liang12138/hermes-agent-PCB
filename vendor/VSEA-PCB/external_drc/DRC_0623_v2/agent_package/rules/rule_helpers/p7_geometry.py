from collections import defaultdict

from geometry.geometry_utils import dist
from rules.rule_helpers.board_filters import(
    _is_signal_net,
    _is_named_net,
)

def _seg_bbox(seg):
    x1, y1 = seg.start
    x2, y2 = seg.end
    return (
        min(x1, x2),
        min(y1, y2),
        max(x1, x2),
        max(y1, y2),
    )


def _bbox_overlap(b1, b2, tol=1e-9):
    return not (
        b1[2] < b2[0] - tol or
        b2[2] < b1[0] - tol or
        b1[3] < b2[1] - tol or
        b2[3] < b1[1] - tol
    )


def _grid_cells_for_bbox(bbox, cell_size):
    minx, miny, maxx, maxy = bbox
    gx1 = int(minx // cell_size)
    gy1 = int(miny // cell_size)
    gx2 = int(maxx // cell_size)
    gy2 = int(maxy // cell_size)

    for gx in range(gx1, gx2 + 1):
        for gy in range(gy1, gy2 + 1):
            yield (gx, gy)


def _build_segments_by_layer(board):
    by_layer = defaultdict(list)
    for seg in board.segments:
        if not _is_signal_net(seg.net):
            continue
        by_layer[seg.layer].append(seg)
    return by_layer


def _choose_grid_cell_size(layer_segments):
    """
    一个比较保守的网格尺寸。
    用 segment 宽度和长度的粗略统计来选，避免格子太大或太小。
    """
    if not layer_segments:
        return 1.0

    lengths = []
    widths = []

    for seg in layer_segments:
        x1, y1 = seg.start
        x2, y2 = seg.end
        lengths.append(dist((x1, y1), (x2, y2)))
        widths.append(seg.width if seg.width > 0 else 0.1)

    lengths.sort()
    widths.sort()

    mid_len = lengths[len(lengths) // 2] if lengths else 1.0
    mid_w = widths[len(widths) // 2] if widths else 0.1

    # 经验值：取“中位长度”的一半，但下限不低于 0.5 mm，上限不高于 5 mm
    cell = max(0.5, min(5.0, max(mid_w * 4.0, mid_len * 0.5)))
    return cell
