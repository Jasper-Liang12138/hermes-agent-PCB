from __future__ import annotations

import os
import json
import re
import hashlib
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .utils import (
    RoutingTask,
    chunk_text,
    compact_context_summary,
    import_ai_pcb_eval,
    top_k_by_token_overlap,
)

try:
    from semantic_database import SemanticDatabase
    from retriever import RetrievalConfig, SemanticRetriever
    from prompt_builder import SemanticPromptBuilder
except ImportError:  # Keep legacy runners usable when semantic modules are absent.
    SemanticDatabase = None  # type: ignore[assignment]
    RetrievalConfig = None  # type: ignore[assignment]
    SemanticRetriever = None  # type: ignore[assignment]
    SemanticPromptBuilder = None  # type: ignore[assignment]


class PCBPipelineWrapper:
    """Unified LLM routing interface around the PCB context pipeline.

    The repository version available on this machine exposes the evaluation
    stages directly. This wrapper keeps the requested routing steps explicit:
    context chunking, retrieval, summary, prompt construction, optional
    experience prior, and OpenAI-compatible LLM inference.
    """

    def __init__(
        self,
        ai_pcb_eval_path: str | Path,
        model: str = "qwen3-32b",
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 65536,
        chunk_chars: int = 1600,
        retrieve_k: int = 2,
    ):
        self.ai_pcb_eval_path = Path(ai_pcb_eval_path).resolve()
        import_ai_pcb_eval(self.ai_pcb_eval_path)
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.chunk_chars = chunk_chars
        self.retrieve_k = retrieve_k
        self.context_builder = os.getenv("VSEA_PCB_CONTEXT_BUILDER", "baseline").strip().lower()
        self.semantic_radius_mm = _env_float("VSEA_PCB_SEMANTIC_RADIUS_MM", 10.0)
        self.semantic_max_region_objects = _env_int("VSEA_PCB_SEMANTIC_MAX_REGION_OBJECTS", 40)
        self.semantic_max_layer_objects = _env_int("VSEA_PCB_SEMANTIC_MAX_LAYER_OBJECTS", 60)
        self._last_context_stats: dict[str, Any] = {}

    def context_chunk(self, task: RoutingTask) -> List[str]:
        return chunk_text(task.context_kicad, max_chars=self.chunk_chars, overlap_chars=600)

    def retrieve_relevant_segments(self, task: RoutingTask, chunks: Sequence[str]) -> List[str]:
        query = "\n".join([task.task_prompt, task.board_id, task.task_id])
        return [chunk for _, chunk in top_k_by_token_overlap(query, chunks, self.retrieve_k)]

    def summarize_context(self, task: RoutingTask, retrieved_segments: Sequence[str]) -> str:
        return compact_context_summary(task, retrieved_segments)

    def build_context_summary(self, task: RoutingTask) -> tuple[str, dict[str, Any]]:
        if self.context_builder == "semantic":
            return self._build_semantic_context_summary(task)
        chunks = self.context_chunk(task)
        retrieved = self.retrieve_relevant_segments(task, chunks)
        summary = self.summarize_context(task, retrieved)
        stats = {
            "context_builder": "baseline",
            "context_chunk_count": len(chunks),
            "context_chunk_chars": [len(chunk) for chunk in chunks],
            "retrieved_segment_count": len(retrieved),
            "retrieved_segments": list(retrieved),
            "prompt_context_chars": len(summary),
            "prompt_token_length_est": _estimate_tokens(summary),
            "semantic_retrieval_object_count": 0,
        }
        self._last_context_stats = stats
        return summary, stats

    def _build_semantic_context_summary(self, task: RoutingTask) -> tuple[str, dict[str, Any]]:
        if SemanticDatabase is None or SemanticRetriever is None or RetrievalConfig is None or SemanticPromptBuilder is None:
            raise RuntimeError("Semantic context modules are not available")
        db = SemanticDatabase.from_kicad(task.context_kicad)
        retriever = SemanticRetriever(
            db,
            RetrievalConfig(
                radius_mm=self.semantic_radius_mm,
                max_region_objects=self.semantic_max_region_objects,
                max_layer_objects=self.semantic_max_layer_objects,
            ),
        )
        retrieval = retriever.retrieve(task.task_prompt)
        summary = SemanticPromptBuilder().build(retrieval)
        stats = {
            "context_builder": "semantic",
            "semantic_radius_mm": self.semantic_radius_mm,
            "prompt_context_chars": len(summary),
            "prompt_token_length_est": _estimate_tokens(summary),
            **retrieval.stats,
        }
        self._last_context_stats = stats
        return summary, stats

    def build_final_routing_prompt(
        self,
        task: RoutingTask,
        context_summary: str,
        experience_prompt: Optional[str] = None,
    ) -> str:
        prior = f"\n\n经验先验：\n{experience_prompt.strip()}" if experience_prompt else ""
        return (
            "/no_think\n"
            "你是一个 PCB 逃逸布线智能体。请根据 PCB 上下文和缺失走线描述，"
            "只生成补全缺失网络所需的 KiCad 走线对象。\n"
            "只允许输出纯文本形式的 (segment ...) 和必要时的 (via ...) 对象。\n"
            "不要输出 Markdown、代码块、解释、分析过程或 <think> 内容。\n\n"
            f"{context_summary}"
            f"{prior}\n\n"
            "布线约束：\n"
            "- 必须保留 PCB 上下文中的 net id、层名、线宽和坐标含义。\n"
            "- 优先生成短的曼哈顿或近似曼哈顿路径。\n"
            "- 避免不同网络在同一铜层发生交叉。\n"
            "- 除非必须换层，否则尽量不要使用 via。\n"
            "- 最终只输出缺失走线对象，不要输出其它内容。\n\n"
            "必须使用下面这种 KiCad 语法格式：\n"
            "(segment (start 47.300000 62.300000) (end 47.300000 68.300000) "
            "(width 0.152400) (layer Top) (net 73))\n"
            "(via (at 47.300000 62.300000) (size 0.457200) (drill 0.203200) "
            "(layers Top In1.Cu) (net 73))"
        )

    def build_cot_plan_routing_prompt(
        self,
        task: RoutingTask,
        context_summary: str,
        experience_prompt: Optional[str] = None,
    ) -> str:
        prior = f"\n\n经验先验：\n{experience_prompt.strip()}" if experience_prompt else ""
        return (
            "你是一个 PCB 逃逸布线智能体。请根据 PCB 上下文和缺失走线描述，"
            "先规划布线路径，再生成补全缺失网络所需的 KiCad 走线对象。\n\n"
            f"{context_summary}"
            f"{prior}\n\n"
            "规划要求：\n"
            "- 在 <think>...</think> 中分析缺失网络、起点终点、可用层、障碍和最短可行路径。\n"
            "- 思考时可以比较 1-3 个候选走法，但必须选择一个最可能 DRC 通过的方案。\n"
            "- 优先短的曼哈顿或近似曼哈顿路径，避免不同网络在同一铜层交叉。\n"
            "- 除非必须换层，否则尽量不要使用 via。\n\n"
            "最终输出要求：\n"
            "- 最终答案必须放在 <answer> 和 </answer> 标签之间。\n"
            "- <answer> 内只允许包含纯文本形式的 (segment ...) 和必要时的 (via ...) 对象。\n"
            "- 不要在 <answer> 内输出 Markdown、代码块、解释或自然语言。\n"
            "- 不要输出省略号、占位符或未完成内容；如果只需要一条线，也必须写完整的 (segment ...)。\n"
            "- 必须保留 PCB 上下文中的 net id、层名、线宽和坐标含义。\n\n"
            "KiCad 语法示例：\n"
            "(segment (start 47.300000 62.300000) (end 47.300000 68.300000) "
            "(width 0.152400) (layer Top) (net 73))\n"
            "(via (at 47.300000 62.300000) (size 0.457200) (drill 0.203200) "
            "(layers Top In1.Cu) (net 73))"
        )

    def call_llm(self, messages: list[dict[str, str]], call_name: str = "llm") -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is required for LLM routing") from exc

        api_key = os.getenv("LLM_API_KEY")
        base_url = os.getenv("LLM_BASE_URL")
        if not api_key or not base_url:
            raise RuntimeError("LLM_API_KEY and LLM_BASE_URL must be set")

        request_timeout = _env_float("LLM_REQUEST_TIMEOUT_SECONDS", 180.0)
        stream_enabled = _env_bool("LLM_STREAM", False)
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=request_timeout,
            max_retries=0,
        )
        request = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_completion_tokens": self.max_tokens,
        }
        if stream_enabled:
            request["stream"] = True
        last_empty_response = ""
        max_attempts = max(1, _env_int("LLM_MAX_ATTEMPTS", 3))
        for attempt in range(1, max_attempts + 1):
            self._log_llm_event(call_name, attempt, messages, status="request")
            self._dump_full_debug_record(
                call_name,
                attempt,
                "request",
                {
                    "request": {
                        "model": self.model,
                        "temperature": self.temperature,
                        "top_p": self.top_p,
                        "max_completion_tokens": self.max_tokens,
                        "stream": stream_enabled,
                    },
                    "messages": _summarize_messages(messages),
                },
            )
            try:
                try:
                    response = client.chat.completions.create(**request)
                except TypeError:
                    request.pop("max_completion_tokens", None)
                    request["max_tokens"] = self.max_tokens
                    response = client.chat.completions.create(**request)
            except Exception as exc:
                last_empty_response = f"{exc.__class__.__name__}: {exc}"
                self._log_llm_event(
                    call_name,
                    attempt,
                    messages,
                    status="exception",
                    extra={
                        "error": last_empty_response,
                        "timeout_seconds": request_timeout,
                        "stream": stream_enabled,
                    },
                )
                self._dump_full_debug_record(
                    call_name,
                    attempt,
                    "exception",
                    {
                        "error": last_empty_response,
                        "timeout_seconds": request_timeout,
                        "stream": stream_enabled,
                    },
                )
                time.sleep(min(2 * attempt, 6))
                continue

            if stream_enabled:
                raw_content, finish_reason, chunk_count, usage = self._consume_stream_response(response)
                if raw_content:
                    stripped_content = strip_think_content(raw_content)
                    self._log_llm_event(
                        call_name,
                        attempt,
                        messages,
                        status="ok",
                        extra={
                            "choices": 1,
                            "finish_reason": finish_reason,
                            "content_chars": len(raw_content),
                            "chunk_count": chunk_count,
                            "usage": usage,
                            "stream": True,
                        },
                    )
                    self._dump_full_debug_record(
                        call_name,
                        attempt,
                        "response",
                        {
                            "choices": 1,
                            "finish_reason": finish_reason,
                            "usage": usage,
                            "raw_content": raw_content,
                            "stripped_content": stripped_content,
                            "stream": True,
                            "chunk_count": chunk_count,
                        },
                    )
                    return stripped_content
                last_empty_response = (
                    f"stream returned no content; finish_reason={finish_reason}; chunks={chunk_count}"
                )
                self._log_llm_event(
                    call_name,
                    attempt,
                    messages,
                    status="empty_choices",
                    extra={"response": last_empty_response, "stream": True},
                )
                self._dump_full_debug_record(
                    call_name,
                    attempt,
                    "empty_choices",
                    {"response": last_empty_response, "stream": True, "chunk_count": chunk_count, "usage": usage},
                )
                self._dump_empty_prompt(call_name, attempt, messages, last_empty_response)
                time.sleep(min(2 * attempt, 6))
                continue

            if response.choices:
                raw_content = response.choices[0].message.content or ""
                stripped_content = strip_think_content(raw_content)
                self._log_llm_event(
                    call_name,
                    attempt,
                    messages,
                    status="ok",
                    extra={
                        "choices": len(response.choices),
                        "finish_reason": response.choices[0].finish_reason,
                        "content_chars": len(raw_content),
                        "usage": response.usage.model_dump() if response.usage else None,
                        "stream": False,
                    },
                )
                self._dump_full_debug_record(
                    call_name,
                    attempt,
                    "response",
                    {
                        "choices": len(response.choices),
                        "finish_reason": response.choices[0].finish_reason,
                        "usage": response.usage.model_dump() if response.usage else None,
                        "raw_content": raw_content,
                        "stripped_content": stripped_content,
                        "stream": False,
                    },
                )
                return stripped_content
            last_empty_response = json.dumps(response.model_dump(), ensure_ascii=False, default=str)[:1200]
            self._log_llm_event(
                call_name,
                attempt,
                messages,
                status="empty_choices",
                extra={"response": last_empty_response, "stream": False},
            )
            self._dump_full_debug_record(
                call_name,
                attempt,
                "empty_choices",
                {"response": response.model_dump(), "stream": False},
            )
            self._dump_empty_prompt(call_name, attempt, messages, last_empty_response)
            time.sleep(min(2 * attempt, 6))
        raise RuntimeError(f"LLM returned no choices after retries. response={last_empty_response}")

    def _consume_stream_response(self, response: Any) -> tuple[str, str, int, dict[str, Any] | None]:
        content_parts: list[str] = []
        finish_reason = ""
        usage: dict[str, Any] | None = None
        chunk_count = 0
        for chunk in response:
            chunk_count += 1
            if getattr(chunk, "usage", None):
                usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else dict(chunk.usage)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = str(choice.finish_reason)
            delta = getattr(choice, "delta", None)
            piece = getattr(delta, "content", None) if delta is not None else None
            if piece:
                content_parts.append(piece)
        return "".join(content_parts), finish_reason, chunk_count, usage

    def run_llm_routing(
        self,
        task: RoutingTask,
        experience_prompt: Optional[str] = None,
        call_name: str = "routing",
    ) -> str:
        summary, context_stats = self.build_context_summary(task)
        prompt = self.build_final_routing_prompt(task, summary, experience_prompt)
        self._dump_full_debug_record(
            call_name,
            0,
            "routing_pipeline",
            {
                "task_id": task.task_id,
                "board_id": task.board_id,
                "sample_dir": task.sample_dir,
                "task_prompt": task.task_prompt,
                **context_stats,
                "context_summary": summary,
                "experience_prompt": experience_prompt or "",
                "final_prompt": prompt,
                "final_prompt_token_length_est": _estimate_tokens(prompt),
            },
        )
        raw = self.call_llm(
            [
                {
                    "role": "system",
                    "content": (
                        "你是资深 PCB 布线工程师和 KiCad 代码生成器。"
                        "只输出合法 KiCad 走线对象，不要输出推理过程。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            call_name=call_name,
        )
        return normalize_routing_response(raw)

    def run_llm_cot_plan_routing(
        self,
        task: RoutingTask,
        experience_prompt: Optional[str] = None,
        call_name: str = "routing",
    ) -> str:
        summary, context_stats = self.build_context_summary(task)
        prompt = self.build_cot_plan_routing_prompt(task, summary, experience_prompt)
        self._dump_full_debug_record(
            call_name,
            0,
            "routing_pipeline",
            {
                "task_id": task.task_id,
                "board_id": task.board_id,
                "sample_dir": task.sample_dir,
                "task_prompt": task.task_prompt,
                **context_stats,
                "context_summary": summary,
                "experience_prompt": experience_prompt or "",
                "final_prompt": prompt,
                "final_prompt_token_length_est": _estimate_tokens(prompt),
            },
        )
        raw = self.call_llm(
            [
                {
                    "role": "system",
                    "content": (
                        "你是资深 PCB 布线工程师和 KiCad 代码生成器。"
                        "请先规划，再在 <answer> 中只输出合法 KiCad 走线对象。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            call_name=call_name,
        )
        return normalize_routing_response(raw)

    def _log_llm_event(
        self,
        call_name: str,
        attempt: int,
        messages: list[dict[str, str]],
        status: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        log_path = os.getenv("REROUTE_DEBUG_LOG")
        if not log_path:
            return
        record: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "call": call_name,
            "attempt": attempt,
            "status": status,
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "message_count": len(messages),
            "total_chars": sum(len(item.get("content", "")) for item in messages),
            "messages": [
                {"role": item.get("role", ""), "chars": len(item.get("content", ""))}
                for item in messages
            ],
        }
        if extra:
            record.update(extra)
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _dump_empty_prompt(
        self,
        call_name: str,
        attempt: int,
        messages: list[dict[str, str]],
        response_summary: str,
    ) -> None:
        dump_dir = os.getenv("REROUTE_DEBUG_DIR")
        if not dump_dir:
            return
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", call_name)[:120]
        path = Path(dump_dir) / f"{int(time.time())}_{os.getpid()}_{safe_name}_attempt{attempt}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "call": call_name,
                    "attempt": attempt,
                    "model": self.model,
                    "max_completion_tokens": self.max_tokens,
                    "response": response_summary,
                    "messages": messages,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _dump_full_debug_record(
        self,
        call_name: str,
        attempt: int,
        stage: str,
        payload: dict[str, Any],
    ) -> None:
        dump_dir = os.getenv("REROUTE_FULL_DEBUG_DIR")
        if not dump_dir or not self._should_full_debug(call_name):
            return
        task_id = self._task_id_from_call(call_name) or "unknown_task"
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", call_name)[:120]
        sequence = int(time.time() * 1000)
        path = (
            Path(dump_dir)
            / task_id
            / f"{sequence}_{safe_name}_attempt{attempt}_{stage}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "call": call_name,
            "attempt": attempt,
            "stage": stage,
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            **_sanitize_debug_payload(payload),
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def _should_full_debug(self, call_name: str) -> bool:
        if os.getenv("REROUTE_ENABLE_DEBUG") != "1":
            return False
        task_id = self._task_id_from_call(call_name)
        if not task_id:
            return False
        try:
            first_n = int(os.getenv("REROUTE_FULL_DEBUG_FIRST_N", "0"))
        except ValueError:
            first_n = 0
        try:
            task_number = int(task_id[1:])
        except ValueError:
            return False
        if first_n <= 0 or task_number > first_n:
            return False

        methods = {
            item.strip()
            for item in os.getenv("REROUTE_FULL_DEBUG_METHODS", "vsea_reroute").split(",")
            if item.strip()
        }
        if not methods:
            return True
        return any(call_name.startswith(method) for method in methods)

    @staticmethod
    def _task_id_from_call(call_name: str) -> Optional[str]:
        match = re.search(r"\b(S\d{4,})\b", call_name)
        return match.group(1) if match else None


_DEFAULT_WRAPPER: Optional[PCBPipelineWrapper] = None


def configure_default_wrapper(wrapper: PCBPipelineWrapper) -> None:
    global _DEFAULT_WRAPPER
    _DEFAULT_WRAPPER = wrapper


def run_llm_routing(
    task: RoutingTask,
    experience_prompt: Optional[str] = None,
    call_name: str = "routing",
) -> str:
    """Module-level interface requested by the experiment plan."""

    if _DEFAULT_WRAPPER is None:
        raise RuntimeError("Default PCBPipelineWrapper is not configured")
    return _DEFAULT_WRAPPER.run_llm_routing(task, experience_prompt, call_name=call_name)


def normalize_routing_response(text: str) -> str:
    """Remove Qwen thinking text and repair a common flat segment format."""

    text = extract_answer_content(strip_think_content(text))
    repaired: List[str] = []
    flat_segment = re.compile(
        r"\(\s*segment\s+start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"width\s+(-?\d+(?:\.\d+)?)\s+layer\s+([A-Za-z0-9_.-]+)\s+net\s+(\d+)\s*\)",
        flags=re.IGNORECASE,
    )
    for line in text.splitlines():
        match = flat_segment.search(line.strip())
        if match:
            x1, y1, x2, y2, width, layer, net = match.groups()
            repaired.append(
                f"(segment (start {x1} {y1}) (end {x2} {y2}) "
                f"(width {width}) (layer {layer}) (net {net}))"
            )
        else:
            repaired.append(line)
    return "\n".join(repaired).strip()


def extract_answer_content(text: str) -> str:
    """Prefer explicit final-answer blocks only when they contain routing code."""

    answer_bodies = [
        match.group(1).strip()
        for match in re.finditer(r"<answer>\s*(.*?)\s*</answer>", text, flags=re.IGNORECASE | re.DOTALL)
    ]
    for body in answer_bodies:
        if contains_kicad_routing(body):
            return body
    if answer_bodies:
        without_tags = re.sub(r"</?answer>", "", text, flags=re.IGNORECASE).strip()
        if contains_kicad_routing(without_tags):
            return without_tags
        return answer_bodies[0]
    return text.strip()


def contains_kicad_routing(text: str) -> bool:
    return bool(re.search(r"\(\s*(segment|via)\b", text, flags=re.IGNORECASE))


def strip_think_content(text: str) -> str:
    """Drop Qwen-style thinking blocks before reusing model text in prompts."""

    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"^\s*<think>.*", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _estimate_tokens(text: str) -> int:
    # A deterministic approximation is enough for comparing context builders.
    return max(1, len(text) // 4)


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _summarize_text(text: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "chars": len(text),
        "sha256": _text_digest(text),
    }
    if os.getenv("REROUTE_ENABLE_FULL_PROMPT_DUMP") == "1":
        summary["content"] = text
    return summary


def _summarize_messages(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "role": item.get("role", ""),
            "content": _summarize_text(item.get("content", "")),
        }
        for item in messages
    ]


def _sanitize_debug_payload(value: Any) -> Any:
    if os.getenv("REROUTE_ENABLE_FULL_PROMPT_DUMP") == "1":
        return value
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "messages" and isinstance(item, list):
                sanitized[key] = _summarize_messages(item)
            elif key in {
                "final_prompt",
                "context_summary",
                "experience_prompt",
                "raw_content",
                "stripped_content",
            } and isinstance(item, str):
                sanitized[key] = _summarize_text(item)
            else:
                sanitized[key] = _sanitize_debug_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_debug_payload(item) for item in value]
    return value
