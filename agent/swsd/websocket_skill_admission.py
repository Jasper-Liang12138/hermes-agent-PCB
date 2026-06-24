"""Admission helpers for PCB WebSocket auto-loaded skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent.skill_utils import get_all_skills_dirs, get_disabled_skill_names, parse_frontmatter, skill_matches_platform
from hermes_constants import get_skills_dir


@dataclass(frozen=True)
class PCBWebSocketSkillSpec:
    """Resolved metadata for a skill eligible for PCB WebSocket admission."""

    name: str
    identifier: str
    intents: tuple[str, ...]
    priority: int
    persistent: bool
    mode: str
    path: Path


_BUILTIN_INTENT_DEFAULTS: dict[str, tuple[str, ...]] = {
    "pcb-intelligence": ("fanout", "mixed"),
    "pcb-reroute": ("reroute", "mixed"),
}

_BUILTIN_PERSISTENT_DEFAULTS: dict[str, bool] = {
    "pcb-intelligence": True,
    "pcb-reroute": True,
}

_BUILTIN_PRIORITY_DEFAULTS: dict[str, int] = {
    "pcb-intelligence": 200,
    "pcb-reroute": 220,
}


def _normalize_intent(intent: str) -> str:
    value = str(intent or "").strip().lower()
    if value in {"global_fanout", "fanout", "bga", "escape"}:
        return "fanout"
    if value in {"reroute", "local_reroute", "ripup"}:
        return "reroute"
    if value == "mixed":
        return "mixed"
    return value or "mixed"


def _normalize_skill_identifier(identifier: str) -> str:
    return str(identifier or "").replace("\\", "/").strip("/")


def _coerce_intents(raw: Any, *, skill_name: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []

    normalized = []
    for value in values:
        intent = _normalize_intent(str(value or ""))
        if intent and intent not in normalized:
            normalized.append(intent)

    if normalized:
        return tuple(normalized)

    builtin = _BUILTIN_INTENT_DEFAULTS.get(skill_name.lower())
    if builtin:
        return builtin
    return ()


def _frontmatter_websocket_pcb(frontmatter: dict[str, Any]) -> dict[str, Any]:
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    hermes_meta = metadata.get("hermes")
    if not isinstance(hermes_meta, dict):
        return {}
    websocket_pcb = hermes_meta.get("websocket_pcb")
    return websocket_pcb if isinstance(websocket_pcb, dict) else {}


def _is_pcb_category(frontmatter: dict[str, Any], skill_dir: Path) -> bool:
    metadata = frontmatter.get("metadata")
    hermes_meta = metadata.get("hermes") if isinstance(metadata, dict) else {}
    category = ""
    if isinstance(hermes_meta, dict):
        category = str(hermes_meta.get("category") or "").strip().lower()
    category_parts = {part.lower() for part in skill_dir.parts}
    return category == "hardware" or "pcb" in category_parts or "hardware" in category_parts


def _all_skill_paths(extra_roots: Iterable[Path] | None = None) -> list[Path]:
    repo_skills_root = Path(__file__).resolve().parents[2] / "skills"
    paths: list[Path] = []
    seen: set[str] = set()
    for root in [repo_skills_root, *list(get_all_skills_dirs()), *list(extra_roots or ())]:
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            key = str(skill_md.resolve())
            if key in seen:
                continue
            if any(part in (".git", ".github", ".hub") for part in skill_md.parts):
                continue
            seen.add(key)
            paths.append(skill_md)
    return paths


def _identifier_for_path(skill_dir: Path, *, roots: Iterable[Path] | None = None) -> str:
    repo_skills_root = Path(__file__).resolve().parents[2] / "skills"
    for root in [repo_skills_root, get_skills_dir(), *(roots or ())]:
        try:
            return _normalize_skill_identifier(str(skill_dir.resolve().relative_to(root.resolve())))
        except ValueError:
            continue
    return _normalize_skill_identifier(skill_dir.name)


def iter_admitted_pcb_websocket_skills(
    *,
    intent: str,
    include_builtin: bool = True,
    extra_roots: Iterable[Path] | None = None,
) -> list[PCBWebSocketSkillSpec]:
    """Return PCB skills admitted for WebSocket auto-loading."""

    normalized_intent = _normalize_intent(intent)
    disabled = get_disabled_skill_names(platform="websocket")
    specs: list[PCBWebSocketSkillSpec] = []

    root_candidates = [Path(__file__).resolve().parents[2] / "skills", *list(get_all_skills_dirs()), *list(extra_roots or ())]
    for skill_md in _all_skill_paths(extra_roots):
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue

        frontmatter, _ = parse_frontmatter(content)
        if not skill_matches_platform(frontmatter):
            continue

        skill_name = str(frontmatter.get("name") or skill_md.parent.name).strip()
        if not skill_name or skill_name in disabled:
            continue

        websocket_meta = _frontmatter_websocket_pcb(frontmatter)
        is_builtin = skill_name.lower() in _BUILTIN_INTENT_DEFAULTS
        if not websocket_meta and not (include_builtin and is_builtin):
            continue
        if websocket_meta and not bool(websocket_meta.get("enabled", False)):
            continue
        if not _is_pcb_category(frontmatter, skill_md.parent) and not is_builtin:
            continue

        intents = _coerce_intents(websocket_meta.get("intents"), skill_name=skill_name)
        if normalized_intent not in intents and "mixed" not in intents:
            continue

        priority = int(
            websocket_meta.get(
                "priority",
                _BUILTIN_PRIORITY_DEFAULTS.get(skill_name.lower(), 100),
            )
        )
        persistent = bool(
            websocket_meta.get(
                "persistent_websocket_session",
                _BUILTIN_PERSISTENT_DEFAULTS.get(skill_name.lower(), False),
            )
        )
        mode = str(websocket_meta.get("mode") or "inject_only").strip().lower() or "inject_only"
        if mode != "inject_only":
            continue

        specs.append(
            PCBWebSocketSkillSpec(
                name=skill_name,
                identifier=_identifier_for_path(skill_md.parent, roots=root_candidates),
                intents=intents,
                priority=priority,
                persistent=persistent,
                mode=mode,
                path=skill_md,
            )
        )

    return sorted(specs, key=lambda item: (-item.priority, item.name.lower()))


def resolve_auto_skills_for_pcb_turn(
    *,
    forced_global_fanout: bool,
    forced_reroute: bool,
    extra_roots: Iterable[Path] | None = None,
) -> list[str]:
    """Resolve ordered skill identifiers for a PCB WebSocket turn."""

    if forced_global_fanout:
        intent = "fanout"
    elif forced_reroute:
        intent = "reroute"
    else:
        intent = "mixed"
    return [
        _normalize_skill_identifier(spec.identifier)
        for spec in iter_admitted_pcb_websocket_skills(intent=intent, extra_roots=extra_roots)
    ]


def auto_skill_persists_for_websocket(
    auto_skill: Any,
    *,
    extra_roots: Iterable[Path] | None = None,
) -> bool:
    """Return True when any requested skill is admitted as turn-persistent."""

    if not auto_skill:
        return False
    if isinstance(auto_skill, str):
        requested = {auto_skill, _normalize_skill_identifier(auto_skill)}
    elif isinstance(auto_skill, (list, tuple, set)):
        requested = set()
        for item in auto_skill:
            text = str(item).strip()
            if not text:
                continue
            requested.add(text)
            requested.add(_normalize_skill_identifier(text))
    else:
        requested = {str(auto_skill), _normalize_skill_identifier(str(auto_skill))}

    specs: list[PCBWebSocketSkillSpec] = []
    seen: set[str] = set()
    for intent_name in ("fanout", "reroute", "mixed"):
        for spec in iter_admitted_pcb_websocket_skills(intent=intent_name, extra_roots=extra_roots):
            if spec.identifier in seen:
                continue
            seen.add(spec.identifier)
            specs.append(spec)
    admitted = {spec.identifier: spec for spec in specs}
    admitted.update({_normalize_skill_identifier(spec.identifier): spec for spec in specs})
    admitted.update({spec.name: spec for spec in specs})
    return any(admitted.get(name) and admitted[name].persistent for name in requested)
