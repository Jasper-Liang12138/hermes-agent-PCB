"""Small local retriever for SWSD jump prior documents."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import RetrievedJumpPrior


DEFAULT_NO_PRIOR_CLARIFICATION = (
    "经检查这个跳转可能是不合规的。请说明你想从当前步骤跳到哪个步骤，"
    "例如重新生成参数、重新选择 BGA、拆线重布，或继续当前流程。"
)


_TOKEN_RE = re.compile(r"[A-Za-z0-9_+\-]+|[\u4e00-\u9fff]")


@dataclass(frozen=True)
class _Doc:
    path: Path
    title: str
    text: str
    tokens: tuple[str, ...]


def retrieve_jump_prior(
    *,
    user_text: str,
    workflow_id: str,
    workflow_state: str,
    docs_root: str | Path | None = None,
    threshold: float = 0.35,
    candidate_action: str = "",
    entities: dict[str, Any] | None = None,
) -> RetrievedJumpPrior | None:
    root = Path(docs_root) if docs_root else _default_docs_root()
    docs = _load_docs(root)
    if not docs:
        return None
    query = _query_text(
        user_text=user_text,
        workflow_id=workflow_id,
        workflow_state=workflow_state,
        candidate_action=candidate_action,
        entities=entities or {},
    )
    query_tokens = _tokens(query)
    if not query_tokens:
        return None
    allow_disambiguation = _allows_disambiguation_as_primary(user_text)
    score_rows: list[tuple[float, float, float, _Doc]] = []
    primary_rows: list[tuple[float, float, float, _Doc]] = []
    for doc in docs:
        bm25 = _bm25_score(query_tokens, doc, docs)
        semantic = _semantic_like_score(user_text, workflow_id, workflow_state, doc)
        score = 0.65 * bm25 + 0.35 * semantic
        row = (score, bm25, semantic, doc)
        score_rows.append(row)
        if _is_primary_candidate(doc, allow_disambiguation=allow_disambiguation):
            primary_rows.append(row)
    score_rows.sort(key=lambda item: _rank_key(item, user_text=user_text, workflow_id=workflow_id, workflow_state=workflow_state, candidate_action=candidate_action), reverse=True)
    primary_rows.sort(key=lambda item: _rank_key(item, user_text=user_text, workflow_id=workflow_id, workflow_state=workflow_state, candidate_action=candidate_action), reverse=True)
    debug_scores = tuple(_score_dict(row) for row in score_rows[:5])
    if not primary_rows:
        return None
    best_score, _bm25, _semantic, best = primary_rows[0]
    if best_score < threshold:
        return None
    supplemental = [row for row in score_rows if _is_supplemental_doc(row[3])][:2]
    content = best.text
    if supplemental:
        content += "\n\n# Supplemental Jump Rules\n" + "\n\n".join(row[3].text for row in supplemental)
    return RetrievedJumpPrior(
        path=str(best.path),
        title=best.title,
        score=round(best_score, 4),
        content=content,
        debug_scores=debug_scores,
    )


def _rank_key(
    row: tuple[float, float, float, _Doc],
    *,
    user_text: str,
    workflow_id: str,
    workflow_state: str,
    candidate_action: str,
) -> tuple[float, int, int, str]:
    score, _bm25, _semantic, doc = row
    priority = _doc_priority(doc, user_text=user_text, workflow_id=workflow_id, workflow_state=workflow_state, candidate_action=candidate_action)
    adjusted_score = score + min(max(priority, -30), 50) * 0.01
    return (adjusted_score, score, priority, -len(doc.tokens), doc.path.name)


def _doc_priority(
    doc: _Doc,
    *,
    user_text: str,
    workflow_id: str,
    workflow_state: str,
    candidate_action: str,
) -> int:
    name = doc.path.name.lower()
    text = str(user_text or "")
    lower_text = text.lower()
    action = str(candidate_action or "").lower()
    priority = 0
    if name == "disambiguation_rules.md":
        priority -= 30
    if name == "cross_workflow_jumps.md":
        priority += 10
    if workflow_id and workflow_state and name == f"{workflow_id}_{workflow_state}.md".lower():
        priority += 8
    rerun_terms = ("重新fanout", "重新 fanout", "重新扇出", "重新布线", "重跑", "改线宽", "改线距")
    reroute_terms = ("拆线重布", "reroute", "rip-up", "ripup", "删线重布")
    change_target_terms = ("重新选择", "换bga", "换 bga", "换目标", "重新选")
    if action == "rerun_fanout" or any(term.lower() in lower_text for term in rerun_terms):
        if name == "pcb_escape_flow_rerun_clean_board.md":
            priority += 40
    if action == "reroute_entry" or any(term.lower() in lower_text for term in reroute_terms):
        if name == "cross_workflow_jumps.md":
            priority += 35
    if action == "pcb_entry" and workflow_id == "pcb_reroute_flow":
        if name == "cross_workflow_jumps.md":
            priority += 35
    if action == "change_target" or any(term.lower() in lower_text for term in change_target_terms):
        if name in {"pcb_escape_flow_rerun_clean_board.md", "pcb_escape_flow_review.md"}:
            priority += 12
    return priority


def _query_text(
    *,
    user_text: str,
    workflow_id: str,
    workflow_state: str,
    candidate_action: str,
    entities: dict[str, Any],
) -> str:
    entity_text = ""
    if entities:
        try:
            entity_text = json.dumps(entities, ensure_ascii=False, sort_keys=True)
        except Exception:
            entity_text = str(entities)
    return " ".join(part for part in (workflow_id, workflow_state, candidate_action, user_text, entity_text) if part)


def _is_primary_candidate(doc: _Doc, *, allow_disambiguation: bool) -> bool:
    name = doc.path.name.lower()
    if name == "index.md":
        return False
    if name == "disambiguation_rules.md" and not allow_disambiguation:
        return False
    return True


def _is_supplemental_doc(doc: _Doc) -> bool:
    return doc.path.name.lower() == "disambiguation_rules.md"


def _allows_disambiguation_as_primary(user_text: str) -> bool:
    text = str(user_text or "")
    return bool(re.search(r"确认|继续|下一步|取消|拒绝|不是|不对|不导入|改一下|修改一下|随便改|再说清楚", text, flags=re.IGNORECASE))


def _score_dict(row: tuple[float, float, float, _Doc]) -> dict[str, Any]:
    score, bm25, semantic, doc = row
    return {
        "path": str(doc.path),
        "name": doc.path.name,
        "title": doc.title,
        "score": round(score, 4),
        "bm25": round(bm25, 4),
        "semantic": round(semantic, 4),
    }


def _default_docs_root() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "rag" / "swsd_jump_priors"


def _load_docs(root: Path) -> list[_Doc]:
    docs: list[_Doc] = []
    if not root.exists():
        return docs
    for path in sorted(root.glob("*.md")):
        if path.name.lower() == "index.md":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        title = _title(text) or path.stem
        docs.append(_Doc(path=path, title=title, text=text, tokens=tuple(_tokens(text))))
    return docs


def _title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _tokens(text: str) -> list[str]:
    base = [item.group(0).lower() for item in _TOKEN_RE.finditer(text or "")]
    grams: list[str] = []
    chars = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    for size in (2, 3):
        grams.extend(chars[i : i + size] for i in range(max(0, len(chars) - size + 1)))
    return base + grams


def _bm25_score(query_tokens: list[str], doc: _Doc, docs: Iterable[_Doc]) -> float:
    all_docs = list(docs)
    if not doc.tokens or not all_docs:
        return 0.0
    avgdl = sum(len(item.tokens) for item in all_docs) / max(len(all_docs), 1)
    freqs: dict[str, int] = {}
    for token in doc.tokens:
        freqs[token] = freqs.get(token, 0) + 1
    score = 0.0
    k1 = 1.5
    b = 0.75
    for token in set(query_tokens):
        df = sum(1 for item in all_docs if token in item.tokens)
        if df <= 0:
            continue
        idf = math.log(1 + (len(all_docs) - df + 0.5) / (df + 0.5))
        tf = freqs.get(token, 0)
        denom = tf + k1 * (1 - b + b * len(doc.tokens) / max(avgdl, 1.0))
        if denom:
            score += idf * (tf * (k1 + 1)) / denom
    return min(1.0, score / 8.0)


def _semantic_like_score(user_text: str, workflow_id: str, workflow_state: str, doc: _Doc) -> float:
    text = f"{doc.path.name}\n{doc.title}\n{doc.text}".lower()
    query = (user_text or "").lower()
    score = 0.0
    if workflow_id and workflow_id.lower() in text:
        score += 0.18
    if workflow_state and workflow_state.lower() in text:
        score += 0.18
    synonym_groups = {
        "rerun_fanout": ("重新fanout", "重新 fanout", "重新扇出", "重新布线", "不满意", "改线宽", "改线距", "重跑"),
        "change_target": ("重新选择", "换bga", "换目标", "选择bga", "选别的"),
        "reroute_entry": ("拆线重布", "reroute", "rip-up", "ripup", "删线重布"),
        "pcb_entry": ("fanout", "扇出", "逃逸", "给", "布线"),
        "resume": ("继续", "确认", "下一步", "接着"),
    }
    for key, terms in synonym_groups.items():
        query_hit = any(term in query for term in terms)
        doc_hit = key in text or any(term in text for term in terms)
        if query_hit and doc_hit:
            score += 0.22
    q_tokens = set(_tokens(query))
    d_tokens = set(doc.tokens)
    if q_tokens and d_tokens:
        score += min(0.42, len(q_tokens & d_tokens) / max(len(q_tokens), 1))
    return min(1.0, score)
