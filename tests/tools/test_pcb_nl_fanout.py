from tools.pcb_nl_fanout import (
    merge_explicit_order_lines,
    parse_fanout_constraints_from_text,
    parse_natural_language_order_lines,
)


def test_parse_fanout_constraints_from_chinese_text():
    assert parse_fanout_constraints_from_text("线宽 4mil，线距为 3.5mil") == {
        "LineWidth": 4,
        "LineSpacing": 3.5,
    }


def test_parse_and_merge_natural_language_order_lines():
    fallback = [
        {"net": "GND", "layer": "SIG01", "order": 1},
        {"net": "VDD_CORE", "layer": "SIG02", "order": 2},
        {"net": "NET_A", "layer": "SIG03", "order": 3},
    ]
    explicit = parse_natural_language_order_lines(
        "GND 放 SIG03，VDD_CORE 走第 2 层",
        fallback_order_lines=fallback,
    )

    merged = merge_explicit_order_lines(fallback, explicit)

    assert merged[0] == {"net": "GND", "layer": "SIG03", "order": 1}
    assert merged[1] == {"net": "VDD_CORE", "layer": "SIG02", "order": 2}
    assert merged[2] == {"net": "NET_A", "layer": "SIG03", "order": 3}
