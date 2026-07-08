from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SEGMENT_RE = re.compile(r"\(\s*segment\b", flags=re.IGNORECASE)
VIA_RE = re.compile(r"\(\s*via\b", flags=re.IGNORECASE)
POINT_RE = re.compile(
    r"\(\s*(start|end|at)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)",
    flags=re.IGNORECASE,
)


@dataclass
class RoutingTask:
    """One real PCB routing sample from the evaluation dataset."""

    task_id: str
    board_id: str
    context_kicad: str
    task_prompt: str
    label_code: str
    complete_kicad: str
    sample_dir: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingResult:
    """Raw routing output produced by a method."""

    method: str
    task_id: str
    routing_output: str
    runtime: float
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalMetrics:
    """Normalized experiment metrics saved to results.csv."""

    method: str
    task_id: str
    score: float
    drc_violation: int
    path_length: float
    via_count: int
    runtime: float
    success: bool
    drc_backend_score: float = 0.0
    status: str = ""
    output_path: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_csv_row(self) -> Dict[str, Any]:
        row = asdict(self)
        row["success"] = int(self.success)
        row["detail"] = json.dumps(self.detail, ensure_ascii=False)
        return row


def import_ai_pcb_eval(ai_pcb_eval_path: str | Path, package_name: str = "eval") -> Any:
    """Load AI-PCB-Eval as an importable package, despite the hyphenated folder name."""

    root = Path(ai_pcb_eval_path).resolve()
    init_file = root / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"AI-PCB-Eval __init__.py not found under {root}")

    # The DRC backend uses top-level imports like parser.kicad_parser_fast.
    # VSEA-PCB also has a local parser package for semantic context, so make
    # the backend package directory win before importing eval/drc modules.
    backend_root = root / "drc_backend"
    if backend_root.exists():
        backend_text = str(backend_root)
        if backend_text in sys.path:
            sys.path.remove(backend_text)
        sys.path.insert(0, backend_text)
        loaded_parser = sys.modules.get("parser")
        loaded_parser_file = str(getattr(loaded_parser, "__file__", "") or "")
        if loaded_parser is not None and backend_text not in loaded_parser_file:
            for module_name in list(sys.modules):
                if module_name == "parser" or module_name.startswith("parser."):
                    sys.modules.pop(module_name, None)
        backend_parser = backend_root / "parser"
        if backend_parser.exists():
            import types
            parser_module = types.ModuleType("parser")
            parser_module.__file__ = str(backend_parser / "_init_.py")
            parser_module.__path__ = [str(backend_parser)]
            sys.modules["parser"] = parser_module

    existing = sys.modules.get(package_name)
    if existing is not None and getattr(existing, "__file__", None) == str(init_file):
        return existing

    spec = importlib.util.spec_from_file_location(
        package_name,
        init_file,
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load package spec from {init_file}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def strip_completion_from_plan(plan_text: str) -> str:
    """Keep missing-net descriptions while removing answer code blocks from *_plan.txt."""

    kept: List[str] = []
    skipping_completion = False
    for line in plan_text.splitlines():
        if "补全代码" in line:
            skipping_completion = True
            continue
        if skipping_completion and re.match(r"^\s*\d+\.\s*网络", line):
            skipping_completion = False
        if not skipping_completion:
            kept.append(line)
    return "\n".join(kept).strip()


def extract_kicad_objects(text: str) -> str:
    """Extract segment/via S-expressions from model text or dataset labels."""

    objects = _extract_sexpressions(text, "segment") + _extract_sexpressions(text, "via")
    return "\n".join(obj.strip() for obj in objects).strip()


def count_vias(routing_output: str) -> int:
    return len(_extract_sexpressions(routing_output, "via"))


def estimate_path_length(routing_output: str) -> float:
    """Approximate copper length from segment start/end points in model output."""

    length = 0.0
    for segment in _extract_sexpressions(routing_output, "segment"):
        points = [(float(x), float(y)) for _, x, y in POINT_RE.findall(segment)]
        if len(points) >= 2:
            (x1, y1), (x2, y2) = points[0], points[1]
            length += math.hypot(x2 - x1, y2 - y1)
    return length


def _extract_sexpressions(text: str, keyword: str) -> List[str]:
    """Extract balanced KiCad S-expressions such as (segment ...) and (via ...)."""

    pattern = re.compile(r"\(\s*" + re.escape(keyword) + r"\b", flags=re.IGNORECASE)
    objects: List[str] = []
    for match in pattern.finditer(text):
        start = match.start()
        depth = 0
        for idx in range(start, len(text)):
            char = text[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    objects.append(text[start : idx + 1])
                    break
    return objects


def chunk_text(text: str, max_chars: int = 6000, overlap_chars: int = 500) -> List[str]:
    """Split large KiCad context into overlapping chunks for retrieval."""

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be in [0, max_chars)")
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap_chars
    return chunks


def tokenize_for_retrieval(text: str) -> List[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?", text.lower())


def top_k_by_token_overlap(query: str, chunks: Sequence[str], k: int) -> List[Tuple[int, str]]:
    """Simple deterministic retrieval used when no repository retriever is available."""

    query_tokens = set(tokenize_for_retrieval(query))
    scored: List[Tuple[float, int, str]] = []
    for idx, chunk in enumerate(chunks):
        chunk_tokens = tokenize_for_retrieval(chunk)
        if not chunk_tokens:
            score = 0.0
        else:
            score = sum(1 for token in chunk_tokens if token in query_tokens)
        scored.append((score, idx, chunk))
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [(idx, chunk) for score, idx, chunk in scored[:k] if score > 0] or [
        (idx, chunk) for _, idx, chunk in scored[:k]
    ]


def compact_context_summary(task: RoutingTask, retrieved_chunks: Sequence[str], max_chars: int = 9000) -> str:
    """Build a concise context summary from the real PCB task and retrieved KiCad snippets."""

    header = [
        f"任务 ID：{task.task_id}",
        f"板卡 ID：{task.board_id}",
        "缺失走线描述：",
        task.task_prompt.strip(),
        "相关 KiCad 上下文片段：",
    ]
    body: List[str] = []
    remaining = max_chars - sum(len(item) + 1 for item in header)
    for idx, chunk in enumerate(retrieved_chunks, start=1):
        if remaining <= 0:
            break
        snippet = chunk[: max(0, remaining)]
        body.append(f"[片段 {idx}]\n{snippet}")
        remaining -= len(snippet)
    return "\n".join(header + body)


def write_jsonl(path: str | Path, records: Iterable[Dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target
