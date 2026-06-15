"""PCB procedural skill bank retrieval."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from hermes_constants import get_skills_dir


def iter_pcb_skill_paths(extra_roots: Iterable[Path] | None = None) -> list[Path]:
    repo_root = Path(__file__).resolve().parents[3]
    roots = [
        repo_root / "skills" / "hardware",
        get_skills_dir() / "hardware",
        get_skills_dir() / "pcb",
    ]
    if extra_roots:
        roots.extend(extra_roots)

    paths: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            key = str(skill_md.resolve())
            if key in seen:
                continue
            name = skill_md.parent.name.lower()
            text = str(skill_md)
            if "pcb" in name or "reroute" in name or "fanout" in name or "pcb" in text.lower():
                seen.add(key)
                paths.append(skill_md)
    return paths


def procedural_hints_from_skills(query: str, workflow_state: str, limit: int = 3) -> list[dict[str, object]]:
    from agent.swsd.skill_grounding import retrieve_skill_memory

    return [item.as_dict() for item in retrieve_skill_memory(query, workflow_state, limit=limit)]
