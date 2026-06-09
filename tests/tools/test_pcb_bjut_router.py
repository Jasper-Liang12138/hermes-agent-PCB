import configparser
import pytest
import subprocess
from pathlib import Path

from tools import pcb_bjut_router
from tools.pcb_bjut_router import (
    _expand_path,
    _combine_route_reports,
    _compact_rl_explanation_report,
    _fallback_rl_explanation_report,
    _rl_eval_budget,
    _rl_python_executable,
    _run_rl_fanout_search,
    _read_rl_explanation_report,
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


def test_load_router_config_accepts_utf8_bom(monkeypatch, tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text("\ufeff# comment\n[router]\nrl_eval_budget = 7\n", encoding="utf-8")
    monkeypatch.setattr(pcb_bjut_router, "_config_paths", lambda: [config_path])

    parser, base_dir = pcb_bjut_router.load_router_config()

    assert base_dir == tmp_path.resolve()
    assert parser.has_section("router")
    assert parser.get("router", "rl_eval_budget") == "7"


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


def test_rl_explanation_report_is_read_from_search_runs(tmp_path):
    work_dir = tmp_path / "work"
    router_dir = tmp_path / "135_windows"
    explanation_dir = router_dir / "rl" / "search_runs" / "demo_entry_layer_order_seed0_budget4"
    explanation_dir.mkdir(parents=True)
    (explanation_dir / "explanation.md").write_text("# RL 解释\n优化了层分配。", encoding="utf-8")

    explanation = _read_rl_explanation_report(work_dir, router_dir, "rl")
    report = _combine_route_reports("布线连通率: 100%", explanation)

    assert "布线连通率: 100%" in report
    assert "层分配和逃逸顺序生成报告" in report
    assert "优化了层分配" in report


def test_non_rl_router_does_not_read_rl_explanation(tmp_path):
    work_dir = tmp_path / "work"
    router_dir = tmp_path / "135_windows"
    explanation_dir = router_dir / "rl" / "search_runs" / "demo_entry_layer_order_seed0_budget4"
    explanation_dir.mkdir(parents=True)
    (explanation_dir / "explanation.md").write_text("不应追加", encoding="utf-8")

    assert _read_rl_explanation_report(work_dir, router_dir, "135") == ""


def test_rl_fallback_explanation_summarizes_order_input(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "order_input.txt").write_text(
        "U22\n"
        "2\n"
        "2\n"
        "NET_A Top 1\n"
        "NET_B Top 2\n"
        "1\n"
        "NET_C Art03 1\n",
        encoding="utf-8",
    )

    report = _fallback_rl_explanation_report(work_dir, "rl")

    assert "本次未读取到 RL explanation.md" in report
    assert "orderLines 数量：3" in report
    assert "Top、Art03" in report
    assert "NET_A->Top#1" in report


def test_compact_rl_explanation_report_converts_markdown_table_to_text():
    report = _compact_rl_explanation_report(
        "# 135 走线优化解释\n\n"
        "## 指标对比\n"
        "| 指标 | 初始方案 | 最佳方案 | 变化 |\n"
        "| --- | ---: | ---: | ---: |\n"
        "| 布通网络 | 148/206 | 150/206 | +2 |\n"
        "| 总线长 | 18466.90 | 18201.19 | -265.71 |\n"
        "## 层和顺序变化\n"
        "- 层负载：Art03 68 -> 68。\n"
        "- 代表性变化：NET_A: Top#1 -> Art03#2。\n"
    )

    assert "135 走线优化解释" in report
    assert "布通网络：148/206 -> 150/206，变化 +2" in report
    assert "总线长：18466.90 -> 18201.19，变化 -265.71" in report
    assert "|" not in report
    assert "##" not in report


def test_run_rl_fanout_search_copies_best_order_and_explanation(monkeypatch, tmp_path):
    work_dir = tmp_path / "work"
    router_dir = tmp_path / "135_windows"
    rl_dir = router_dir / "rl"
    work_dir.mkdir()
    rl_dir.mkdir(parents=True)
    (rl_dir / "train_dqn_135.py").write_text("print('fake')\n", encoding="utf-8")
    layout_path = work_dir / "layout_input.txt"
    layout_path.write_text("board", encoding="utf-8")
    (work_dir / "order_input.txt").write_text("U22\n1\n1\nBASE Top 1\n", encoding="utf-8")

    def fake_run_process(args, cwd, timeout=300):
        output_root = Path(args[args.index("--output-root") + 1])
        run_dir = output_root / "hermes_rl_entry_layer_order_seed20260425_budget1"
        run_dir.mkdir(parents=True)
        (run_dir / "best_layer_order.txt").write_text("U22\n1\n1\nRL_NET Art03 1\n", encoding="utf-8")
        (run_dir / "explanation.md").write_text("# RL explanation\nbetter order", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(pcb_bjut_router, "_run_process", fake_run_process)
    monkeypatch.setenv("PCB_RL_EVAL_BUDGET", "1")

    best_order = _run_rl_fanout_search(work_dir, router_dir, "rl", layout_path)

    assert best_order.name == "best_layer_order.txt"
    assert "RL_NET Art03 1" in (work_dir / "order_input.txt").read_text(encoding="utf-8")
    assert (work_dir / "explanation.md").read_text(encoding="utf-8").startswith("# RL explanation")
    assert (router_dir / "402Pin_08BGA_8L_S_01141700.txt").read_text(encoding="utf-8") == "board"


def test_rl_runtime_config_resolves_relative_python(monkeypatch, tmp_path):
    parser = configparser.ConfigParser()
    parser["router"] = {
        "rl_python": r".\python_runtime\python.exe",
        "rl_eval_budget": "12",
    }

    monkeypatch.delenv("PCB_RL_PYTHON", raising=False)
    monkeypatch.delenv("PCB_RL_EVAL_BUDGET", raising=False)
    monkeypatch.setattr(pcb_bjut_router, "load_router_config", lambda: (parser, tmp_path))

    assert _rl_python_executable() == str((tmp_path / "python_runtime" / "python.exe").resolve())
    assert _rl_eval_budget("rl") == 12


def test_rl_runtime_env_overrides_config(monkeypatch, tmp_path):
    parser = configparser.ConfigParser()
    parser["router"] = {
        "rl_python": r".\python_runtime\python.exe",
        "rl_eval_budget": "12",
    }

    monkeypatch.setenv("PCB_RL_PYTHON", r"D:\Python\python.exe")
    monkeypatch.setenv("PCB_RL_EVAL_BUDGET", "3")
    monkeypatch.setattr(pcb_bjut_router, "load_router_config", lambda: (parser, tmp_path))

    assert _rl_python_executable() == str(Path(r"D:\Python\python.exe"))
    assert _rl_eval_budget("rl") == 3
