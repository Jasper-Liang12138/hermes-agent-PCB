import pytest
import subprocess
from pathlib import Path

from tools import pcb_bjut_router
from tools.pcb_bjut_router import (
    _expand_path,
    _run_router_main,
    bjut_router_available,
    copy_arc_constrain,
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


def test_parse_layer_grouped_order_input_text():
    text = (
        "U22\n"
        "2\n"
        "2\n"
        "GND Top 1\n"
        "MCLK Top 3\n"
        "1\n"
        "VCC Art03 2\n"
    )
    parsed = parse_order_input_text(text)
    assert parsed["selectedBGA"] == "U22"
    assert parsed["constraints"] == {}
    assert parsed["orderLines"] == [
        {"net": "GND", "layer": "Top", "order": 1},
        {"net": "VCC", "layer": "Art03", "order": 2},
        {"net": "MCLK", "layer": "Top", "order": 3},
    ]


def test_resolve_router_dir_from_config():
    arc_dir = resolve_router_dir("arc")
    assert isinstance(arc_dir, Path)
    assert arc_dir.name


@pytest.mark.skipif(not bjut_router_available("135"), reason="BJUT 135 router binaries not configured")
def test_bjut_router_available_when_configured():
    assert bjut_router_available("135")
    assert bjut_router_available("arc")


def test_expand_path_absolute_unchanged_with_base_dir():
    result = _expand_path("D:/Routers/arc_windows_0519", base_dir=Path("/app/config"))
    assert result == Path("D:/Routers/arc_windows_0519")


def test_expand_path_relative_resolved_against_base_dir():
    result = _expand_path("../routers/arc_windows_0519", base_dir=Path("/app/config"))
    assert result == Path("/app/routers/arc_windows_0519").resolve()


def test_expand_path_relative_without_base_dir_unchanged():
    result = _expand_path("./router_work", base_dir=None)
    assert result == Path("./router_work")


def test_expand_path_env_var_expanded_to_absolute_not_re_resolved(monkeypatch):
    monkeypatch.setenv("ROUTER_ROOT", "D:/Projects/routers")
    result = _expand_path("%ROUTER_ROOT%/arc_windows_0519", base_dir=Path("/app/config"))
    assert result == Path("D:/Projects/routers/arc_windows_0519")


def test_copy_arc_constrain_uses_router_profile_file(tmp_path):
    router_dir = tmp_path / "arc_runtime"
    work_dir = tmp_path / "work"
    router_dir.mkdir()
    work_dir.mkdir()
    (router_dir / "constrain.txt").write_text("PROFILE_CONSTRAINT\n", encoding="utf-8")

    copied = copy_arc_constrain(work_dir, router_dir)

    assert copied == work_dir / "constrain.txt"
    assert copied.read_text(encoding="utf-8") == "PROFILE_CONSTRAINT\n"


def test_copy_arc_constrain_missing_file_fails_clearly(tmp_path):
    router_dir = tmp_path / "arc_runtime"
    work_dir = tmp_path / "work"
    router_dir.mkdir()
    work_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="constrain.txt"):
        copy_arc_constrain(work_dir, router_dir)
