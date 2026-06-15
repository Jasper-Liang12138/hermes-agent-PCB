from tools.pcb_nl_fanout import (
    merge_explicit_order_lines,
    parse_fanout_constraints_from_text,
    parse_natural_language_order_lines,
)


def test_parse_natural_language_order_lines_groups_by_layer_and_order():
    fallback = [
        {"net": "NET_A", "layer": "SIG01", "order": 1},
        {"net": "NET_B", "layer": "SIG01", "order": 2},
        {"net": "NET_C", "layer": "SIG02", "order": 3},
        {"net": "NET_D", "layer": "SIG02", "order": 4},
    ]
    text = "对 U22 开始布线，用 135 + 北科大，NET_A、NET_B 走 SIG03，NET_C 走 SIG04，线宽 5mil 间距 4mil"

    explicit = parse_natural_language_order_lines(text, fallback)
    merged = merge_explicit_order_lines(fallback, explicit)

    assert explicit == [
        {"net": "NET_A", "layer": "SIG03", "order": 1},
        {"net": "NET_B", "layer": "SIG03", "order": 2},
        {"net": "NET_C", "layer": "SIG04", "order": 3},
    ]
    assert merged == [
        {"net": "NET_A", "layer": "SIG03", "order": 1},
        {"net": "NET_B", "layer": "SIG03", "order": 2},
        {"net": "NET_C", "layer": "SIG04", "order": 3},
        {"net": "NET_D", "layer": "SIG02", "order": 4},
    ]
    assert parse_fanout_constraints_from_text(text) == {"LineWidth": 5, "LineSpacing": 4}
