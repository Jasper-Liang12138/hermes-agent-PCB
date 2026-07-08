from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, List

from .utils import RoutingTask


QueryBuilder = Callable[[RoutingTask, dict[str, Any], dict[str, Any]], str]


def retrieval_tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?", text.lower()))


class SkillCardBank:
    """Portable read-only JSONL skill bank used by VSEA repair prompts."""

    def __init__(
        self,
        path: str | Path,
        query_builder: QueryBuilder | None = None,
        read_only: bool = True,
    ):
        self.path = Path(path)
        self.query_builder = query_builder or _default_query_builder
        self.read_only = read_only
        self.cards = self._load()

    def _load(self) -> List[dict[str, Any]]:
        if not self.path.exists():
            return []
        cards: List[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and (item.get("card_text") or item.get("card")):
                    cards.append(item)
        return cards

    def retrieve(
        self,
        task: RoutingTask,
        repair_contract: dict[str, Any],
        report: dict[str, Any],
        positive_k: int = 1,
        negative_k: int = 1,
    ) -> List[dict[str, Any]]:
        query = self.query_builder(task, repair_contract, report)
        positive = self._top_cards(query, task.task_id, "positive", positive_k)
        negative = self._top_cards(query, task.task_id, "negative", negative_k)
        return negative + positive

    def _top_cards(
        self,
        query: str,
        task_id: str,
        polarity: str,
        limit: int,
    ) -> List[dict[str, Any]]:
        if limit <= 0:
            return []
        query_tokens = retrieval_tokens(query)
        scored: List[tuple[float, dict[str, Any]]] = []
        for card in self.cards:
            if card.get("source_task_id") == task_id or card.get("polarity") != polarity:
                continue
            card_text = _retrieval_text(card)
            card_tokens = retrieval_tokens(card_text)
            if not card_tokens:
                continue
            overlap = len(query_tokens & card_tokens)
            net_bonus = 0.5 * len(
                {
                    str(conn.get("net"))
                    for conn in card.get("expected_connections") or []
                    if str(conn.get("net")) in query
                }
            )
            score = overlap + net_bonus
            if score > 0:
                scored.append((score, card))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [card for _, card in scored[:limit]]


def _default_query_builder(
    task: RoutingTask,
    repair_contract: dict[str, Any],
    report: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "task_prompt": task.task_prompt,
            "repair_contract": repair_contract,
            "hard_rule_counts": report.get("hard_rule_counts"),
            "issue_kind_summary": report.get("issue_kind_summary"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _retrieval_text(card: dict[str, Any]) -> str:
    return json.dumps(
        {
            "card_text": card.get("card_text") or card.get("card") or "",
            "retrieval_keys": card.get("retrieval_keys") or {},
            "hard_rule_counts": card.get("hard_rule_counts") or {},
            "expected_connections": card.get("expected_connections") or [],
            "route_pattern": card.get("route_pattern") or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
