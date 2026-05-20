import pytest
from pathlib import Path

from tools.pcb_bjut_router import (
    bjut_router_available,
    normalize_router_type,
    parse_order_input_text,
    resolve_router_dir,
    router_execution_family,
)


def test_normalize_router_type_aliases():
    assert normalize_router_type("135度") == "135"
    assert normalize_router_type("RL") == "rl"
    assert normalize_router_type("rl_arc") == "rl_arc"


def test_router_execution_family():
    assert router_execution_family("135") == "135"
    assert router_execution_family("rl") == "135"
    assert router_execution_family("rl_arc") == "arc"


def test_parse_order_input_text():
    text = "U22\n4\n3\nGND Top 1\nVCC Art03 2\n"
    parsed = parse_order_input_text(text)
    assert parsed["selectedBGA"] == "U22"
    assert parsed["constraints"]["LineWidth"] == 4
    assert parsed["constraints"]["LineSpacing"] == 3
    assert parsed["orderLines"][0] == {"net": "GND", "layer": "Top", "order": 1}
    assert parsed["orderLines"][1] == {"net": "VCC", "layer": "Art03", "order": 2}


def test_resolve_router_dir_from_config():
    arc_dir = resolve_router_dir("arc")
    assert isinstance(arc_dir, Path)
    assert arc_dir.name in {"弧形走线", "work", "."}


@pytest.mark.skipif(not bjut_router_available("135"), reason="BJUT 135 router binaries not configured")
def test_bjut_router_available_when_configured():
    assert bjut_router_available("135")
    assert bjut_router_available("arc")
