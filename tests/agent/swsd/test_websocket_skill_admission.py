from __future__ import annotations

from pathlib import Path

from agent.skill_commands import _build_skill_message, _load_skill_payload
from agent.swsd.websocket_skill_admission import resolve_auto_skills_for_pcb_turn


def _write_skill(
    root: Path,
    rel_dir: str,
    *,
    name: str,
    intents: str = "[mixed]",
    priority: int = 500,
    mode: str = "inject_only",
    persistent: str = "true",
) -> Path:
    skill_dir = root / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / "scripts" / "helper.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: test pcb websocket skill\n"
            "metadata:\n"
            "  hermes:\n"
            "    category: hardware\n"
            "    websocket_pcb:\n"
            "      enabled: true\n"
            f"      intents: {intents}\n"
            f"      priority: {priority}\n"
            f"      mode: {mode}\n"
            f"      persistent_websocket_session: {persistent}\n"
            "---\n\n"
            "# Test Skill\n"
        ),
        encoding="utf-8",
    )
    return skill_dir


def test_resolve_auto_skills_admits_custom_pcb_skill_from_extra_root(tmp_path):
    extra_root = tmp_path / "external-skills"
    _write_skill(
        extra_root,
        "hardware/bga-routing-history",
        name="bga-routing-history",
        intents="[reroute, mixed]",
        priority=500,
    )

    resolved = resolve_auto_skills_for_pcb_turn(
        forced_global_fanout=False,
        forced_reroute=False,
        extra_roots=[extra_root],
    )

    assert resolved[0] == "hardware/bga-routing-history"
    assert "hardware/pcb-reroute" in resolved
    assert "hardware/pcb-intelligence" in resolved


def test_resolve_auto_skills_rejects_non_inject_only_mode(tmp_path):
    extra_root = tmp_path / "external-skills"
    _write_skill(
        extra_root,
        "hardware/bga-routing-history",
        name="bga-routing-history",
        intents="[mixed]",
        mode="execute_scripts",
    )

    resolved = resolve_auto_skills_for_pcb_turn(
        forced_global_fanout=False,
        forced_reroute=False,
        extra_roots=[extra_root],
    )

    assert "hardware/bga-routing-history" not in resolved


def test_load_skill_payload_resolves_external_skill_directory(tmp_path, monkeypatch):
    from agent import skill_utils

    extra_root = tmp_path / "external-skills"
    skill_dir = _write_skill(
        extra_root,
        "hardware/bga-routing-history",
        name="bga-routing-history",
    )

    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda: [extra_root])
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skill_utils.get_skills_dir(), extra_root])

    loaded = _load_skill_payload("hardware/bga-routing-history")

    assert loaded is not None
    loaded_skill, resolved_skill_dir, skill_name = loaded
    assert loaded_skill["success"] is True
    assert skill_name == "bga-routing-history"
    assert resolved_skill_dir == skill_dir

    message = _build_skill_message(
        loaded_skill,
        resolved_skill_dir,
        '[SYSTEM: The "bga-routing-history" skill is auto-loaded.]',
    )
    assert 'skill_view(name="hardware/bga-routing-history", file_path="<path>")' in message
