"""Skill memory grounding for SWSD decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SkillGroundingItem:
    principle: str = ""
    strategy: str = ""
    operation: str = ""
    positive_rule: str = ""
    failure_mode: str = ""
    elimination_rule: str = ""
    source: str = ""
    score: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "principle": self.principle,
            "strategy": self.strategy,
            "operation": self.operation,
            "positive_rule": self.positive_rule,
            "failure_mode": self.failure_mode,
            "elimination_rule": self.elimination_rule,
            "source": self.source,
            "score": self.score,
        }


def _candidate_skill_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    return [
        root / "skills" / "hardware" / "pcb-intelligence" / "SKILL.md",
        root / "skills" / "hardware" / "pcb-reroute" / "SKILL.md",
    ]


def _score_skill(text: str, workflow_state: str, content: str) -> float:
    haystack = content.lower()
    terms = set(str(text or "").lower().replace("，", " ").replace("。", " ").split())
    score = 0.0
    for term in terms:
        if len(term) >= 2 and term in haystack:
            score += 1.0
    if "pcb" in haystack:
        score += 0.5
    if workflow_state and workflow_state.lower() in haystack:
        score += 0.5
    return score


def _item_from_content(path: Path, content: str, score: float) -> SkillGroundingItem:
    preview = " ".join(line.strip() for line in content.splitlines() if line.strip())[:500]
    name = path.parent.name
    return SkillGroundingItem(
        principle=f"Use procedural PCB skill memory from {name}.",
        strategy=preview,
        operation=name,
        positive_rule="Use this grounding only to support probabilistic decisions.",
        failure_mode="Do not let skill memory directly override workflow constraints.",
        elimination_rule="Reject tool execution when probability or workflow constraints do not allow it.",
        source=str(path),
        score=round(score, 3),
    )


def retrieve_skill_memory(
    query: str,
    workflow_state: str = "idle",
    *,
    skill_paths: Iterable[Path] | None = None,
    limit: int = 3,
) -> list[SkillGroundingItem]:
    items: list[SkillGroundingItem] = []
    for path in skill_paths or _candidate_skill_paths():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        score = _score_skill(query, workflow_state, content)
        if score <= 0:
            continue
        items.append(_item_from_content(path, content, score))
    return sorted(items, key=lambda item: item.score, reverse=True)[: max(0, limit)]
