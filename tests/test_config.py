import importlib.util
import sys
from pathlib import Path

from pcb_agent_langgraph.utils.config import load_config


def test_example_config_does_not_force_agent_drc_python():
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.ini")

    assert config.reroute_loop.agent_drc_python == ""


def test_example_config_does_not_force_missing_external_drc_package():
    config = load_config(Path(__file__).resolve().parents[1] / "config.example.ini")

    assert config.reroute_loop.drc_agent_package == ""


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
