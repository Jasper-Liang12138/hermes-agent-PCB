from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read_skill(name: str) -> str:
    return (ROOT / "skills" / "hardware" / name / "SKILL.md").read_text(encoding="utf-8")


def test_pcb_intelligence_documents_dual_router_bga_flow():
    content = _read_skill("pcb-intelligence")

    assert content.startswith("---\n")
    assert "routerType" in content
    assert '"arc"' in content
    assert '"135"' in content
    assert "hardware/pcb-reroute" in content
    assert "不要在本技能中调用 `drop_net` 或 `reroute`" in content


def test_pcb_reroute_documents_selected_trace_flow():
    content = _read_skill("pcb-reroute")

    assert content.startswith("---\n")
    assert "getSelectedElements" in content
    assert 'PFindType="TRACES"' in content
    assert "deleteTracesById" in content
    assert "Do not call `route`" in content
    assert "Do not ask the user to choose `arc` or `135`" in content
    assert "rerouteResult" in content
    assert "routedBoardDataFilePath" in content
