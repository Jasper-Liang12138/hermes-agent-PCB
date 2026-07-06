from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# ====== 功能：按字符窗口把 KiCad 版图文本切成可检索片段。 ======
def chunk_text_for_reroute(text: str, *, max_chars: int = 1600, overlap_chars: int = 600) -> list[str]:
    if max_chars <= 0:
        return [text]
    overlap_chars = max(0, min(overlap_chars, max_chars - 1))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap_chars
    return chunks


# ====== 功能：把用户任务、网络名和 KiCad 片段统一拆成检索词。 ======
def tokenize_reroute_query(text: Any) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]*|\d+(?:\.\d+)?", str(text or "").lower())


# ====== 功能：从前端局部上下文和拆线结果中提取目标网络。 ======
def target_nets_from_context(*items: Any) -> list[str]:
    nets: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("selectedNets", "nets", "localRouteNets", "missingRoutes", "missing_routes"):
            value = item.get(key)
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        candidate = entry.get("net") or entry.get("net_name") or entry.get("netName") or entry.get("name")
                    else:
                        candidate = entry
                    text = str(candidate or "").strip()
                    if text and text not in nets:
                        nets.append(text)
    return nets


# ====== 功能：读取 boardData 字符串或文件路径里的 KiCad 版图文本。 ======
def board_text_from_payload(payload: Any) -> tuple[str, str]:
    if isinstance(payload, str):
        path = Path(payload)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore"), str(path)
        return payload, ""
    if isinstance(payload, dict):
        for key in ("boardData", "droppedBoardData", "data", "content", "layout", "projectData"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text, path = board_text_from_payload(value)
                if text:
                    return text, path
        for key in ("droppedBoardDataFilePath", "originalBoardDataFilePath", "boardDataFilePath", "projectDataFilePath", "absolute_path", "filePath"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text, path = board_text_from_payload(value)
                if text:
                    return text, path
    return "", ""


# ====== 功能：对 KiCad 版图执行本地分块检索并生成 reroute 压缩上下文。 ======
def build_reroute_context(
    *,
    board_text: str,
    task_description: str = "",
    selected_trace_ids: list[str] | None = None,
    nets: list[str] | None = None,
    local_context: Any = None,
    chunk_chars: int = 1600,
    overlap_chars: int = 600,
    retrieve_k: int = 2,
) -> dict[str, Any]:
    chunk_chars = max(512, min(6000, int(chunk_chars or 1600)))
    retrieve_k = max(1, min(8, int(retrieve_k or 2)))
    chunks = chunk_text_for_reroute(board_text or "", max_chars=chunk_chars, overlap_chars=int(overlap_chars or 0))
    query = "\n".join([task_description, " ".join(selected_trace_ids or []), " ".join(nets or []), json.dumps(local_context or {}, ensure_ascii=False)])
    query_tokens = set(tokenize_reroute_query(query))

    scored: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        tokens = tokenize_reroute_query(chunk)
        score = sum(1 for token in tokens if token in query_tokens) if query_tokens else 0
        scored.append((score, index, chunk))
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    selected = [(index, chunk) for score, index, chunk in scored[:retrieve_k] if score > 0]
    if not selected:
        selected = [(index, chunk) for _score, index, chunk in scored[:retrieve_k]]
    context_text = "\n".join(f"[片段 {index + 1}]\n{chunk}" for index, chunk in selected)
    return {
        "status": "ok",
        "tool": "compress_reroute_context",
        "contextText": context_text,
        "stats": {
            "strategy": "local_keyword_chunk_retrieval",
            "chunkCount": len(chunks),
            "chunkChars": chunk_chars,
            "overlapChars": max(0, int(overlap_chars or 0)),
            "retrievedSegmentCount": len(selected),
            "retrievedSegmentIndexes": [index for index, _chunk in selected],
            "contextChars": len(context_text),
            "queryTokenCount": len(query_tokens),
        },
    }

