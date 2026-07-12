import importlib.util
import sys
from pathlib import Path

from pcb_agent_langgraph.utils.config import RerouteLoopConfig, load_config


def test_example_config_uses_project_relative_agent_drc_python():
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.ini")

    assert config.reroute_loop.agent_drc_python == r".\runtime\explain_python\python.exe"


def test_example_config_uses_default_relative_external_drc_package():
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.ini")

    assert config.reroute_loop.drc_agent_package == r".\vendor\VSEA-PCB\external_drc\DRC_0623_v2\agent_package"


def test_missing_config_uses_default_relative_external_drc_package():
    assert RerouteLoopConfig().drc_agent_package == r".\vendor\VSEA-PCB\external_drc\DRC_0623_v2\agent_package"


def test_vsea_drc_adapter_empty_python_uses_current_interpreter(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHON", raising=False)
    repo_root = Path(__file__).resolve().parents[1]
    adapter_path = repo_root / "vendor" / "VSEA-PCB" / "reroute_pipeline" / "drc_adapter.py"
    spec = importlib.util.spec_from_file_location("vsea_drc_adapter_for_test", adapter_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    adapter = module.AgentHardDRCAdapter(tmp_path, python_executable="")

    assert adapter.python_executable == sys.executable


def test_relative_vsea_paths_resolve_from_config_directory(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools.external import _resolve_path, _vsea_dependency_paths

    project_root = tmp_path / "project"
    project_root.mkdir()
    config_path = project_root / "config.live.ini"
    config_path.write_text(
        "[reroute_loop]\n"
        "pipeline_root = .\\vendor\\VSEA-PCB\n"
        "ai_pcb_eval_path = .\\vendor\\AI-PCB-Eval\n"
        "drc_agent_package = .\\vendor\\VSEA-PCB\\external_drc\\DRC_0623_v2\\agent_package\n"
        "agent_drc_python = .\\runtime\\explain_python\\python.exe\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(config_path)
    ai_eval, drc_package, agent_python = _vsea_dependency_paths(config, {})

    assert config.root == project_root
    assert _resolve_path(config.root, config.reroute_loop.pipeline_root) == project_root / "vendor" / "VSEA-PCB"
    assert ai_eval == project_root / "vendor" / "AI-PCB-Eval"
    assert drc_package == project_root / "vendor" / "VSEA-PCB" / "external_drc" / "DRC_0623_v2" / "agent_package"
    assert _resolve_path(config.root, agent_python) == project_root / "runtime" / "explain_python" / "python.exe"