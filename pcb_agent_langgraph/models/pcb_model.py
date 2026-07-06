from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from pcb_agent_langgraph.graph.state import ChatMessage
from pcb_agent_langgraph.utils.config import ModelConfig


_THINK_BLOCK_RE = re.compile(r"(?is)<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>")
_FINAL_PREFIX_RE = re.compile(r"(?is)(?:^|\n)\s*(?:final|answer|json|最终答案|答案)\s*[:：]\s*")


@dataclass(slots=True)
# ====== 功能：承载模型响应文本和原始返回数据。 ======
class ModelResult:
    content: str
    raw: dict[str, Any]
    elapsed_ms: float
    usage: dict[str, Any]


# ====== 功能：封装统一 pcb-model 的真实 API 调用逻辑。 ======
class PCBModel:
    """OpenAI-compatible wrapper for the single configured pcb-model."""

    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, config: ModelConfig, timeout: float = 300.0) -> None:
        self.config = config
        self.timeout = timeout
        self.public_name = "pcb-model"

    # ====== 功能：按旧 Hermes reroute stage 方式调用配置的模型接口。 ======
    def complete(self, messages: list[ChatMessage], *, temperature: float = 0.0) -> ModelResult:
        if not self.config.base_url or not self.config.model:
            raise RuntimeError("pcb-model is not configured. Please set [reroute-model] base_url/model in config.ini.")

        base_url = _normalize_openai_base_url(self.config.base_url)
        url = base_url + "/chat/completions"
        body = _request_body(self.config, messages, temperature, base_url)
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        start = time.perf_counter()
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if body.get("stream"):
                    content, raw = _read_stream_response(response)
                else:
                    raw = json.loads(response.read().decode("utf-8"))
                    content = _content_from_payload(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"pcb-model HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"pcb-model request failed: {exc}") from exc

        elapsed_ms = (time.perf_counter() - start) * 1000
        content = _strip_think_blocks(content)
        if not content:
            raise RuntimeError(f"pcb-model returned empty content: raw={_safe_raw_summary(raw)}")
        return ModelResult(content=content, raw=raw, elapsed_ms=elapsed_ms, usage=raw.get("usage") or {})


# ====== 功能：规范化 OpenAI-compatible base_url，避免重复拼接 chat/completions。 ======
def _normalize_openai_base_url(value: str) -> str:
    text = str(value or "").strip().strip("`'\"，,;；")
    text = re.sub(r"/chat/completions/?$", "", text.rstrip("/"), flags=re.IGNORECASE)
    return text.rstrip("/")


# ====== 功能：判断当前模型端点是否是 wishub/ctyun。 ======
def _is_wishub_endpoint(base_url: str) -> bool:
    lowered = str(base_url or "").lower()
    return "ctyun.cn" in lowered or "wishub-x5" in lowered


# ====== 功能：构造旧 Hermes STAGE_REROUTE 同款请求体。 ======
def _request_body(config: ModelConfig, messages: list[ChatMessage], temperature: float, base_url: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": config.model,
        "messages": [dict(message) for message in messages],
        "temperature": temperature,
        "top_p": 1,
        "stream": _is_wishub_endpoint(base_url),
    }
    token_key = "max_completion_tokens" if _is_wishub_endpoint(base_url) else "max_tokens"
    body[token_key] = min(int(config.max_tokens or 4096), 4096)
    return body


# ====== 功能：读取 SSE 或非 SSE 流式响应并提取模型文本。 ======
def _read_stream_response(response: Any) -> tuple[str, dict[str, Any]]:
    buffer = ""
    chunks = 0
    last_payload: dict[str, Any] = {}
    non_sse_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            non_sse_lines.append(line)
            continue
        payload_text = line[5:].strip()
        if payload_text == "[DONE]":
            break
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        last_payload = payload
        delta = _delta_text(payload)
        if delta:
            chunks += 1
            buffer += delta
            candidate = _structured_candidate(buffer)
            if candidate:
                return candidate, {"stream_chunks": chunks, "stream_finish_reason": "structured", "last_payload": last_payload}

    if not buffer and non_sse_lines:
        raw_text = "\n".join(non_sse_lines).strip()
        try:
            payload = json.loads(raw_text)
            text = _content_from_payload(payload)
            return text, payload
        except Exception:
            buffer = raw_text
    candidate = _structured_candidate(buffer)
    return candidate or buffer.strip(), {"stream_chunks": chunks, "stream_finish_reason": "done", "last_payload": last_payload}


# ====== 功能：从流式 chunk 中提取 content 或 reasoning 文本。 ======
def _delta_text(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices") if isinstance(chunk, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta") or choices[0].get("message") or {}
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    if content is None:
        content = delta.get("reasoning_content") or delta.get("reasoning")
    return str(content or "")


# ====== 功能：从非流式 OpenAI 响应中提取正文。 ======
def _content_from_payload(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or [] if isinstance(payload, dict) else []
    if not choices:
        raise RuntimeError(f"pcb-model returned no choices: code={payload.get('code')!r}, usage={payload.get('usage')!r}")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    else:
        text = str(content or "")
    if not text:
        text = str(message.get("reasoning_content") or message.get("reasoning") or "")
    return text


# ====== 功能：从混杂文本中提取完整 JSON 候选。 ======
def _structured_candidate(text: str) -> str:
    raw = _tail_after_final_prefix(str(text or "")).strip()
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE)
    for candidate in reversed([item.strip() for item in fenced if item.strip()]):
        if _is_json(candidate):
            return candidate
    starts = [match.start() for match in re.finditer(r"\{", raw)]
    for start in reversed(starts):
        candidate = _balanced_json(raw[start:])
        if candidate and _is_json(candidate):
            return candidate
    return ""


# ====== 功能：去掉模型流式输出中的 final/json 前缀。 ======
def _tail_after_final_prefix(text: str) -> str:
    matches = list(_FINAL_PREFIX_RE.finditer(str(text or "")))
    return text[matches[-1].end():] if matches else text


# ====== 功能：判断字符串是否为 JSON。 ======
def _is_json(value: str) -> bool:
    try:
        json.loads(value)
        return True
    except Exception:
        return False


# ====== 功能：从左花括号开始截取平衡 JSON 对象。 ======
def _balanced_json(text: str) -> str:
    depth = 0
    in_string = False
    escape = False
    for index, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[: index + 1].strip()
    return ""


# ====== 功能：删除 think/thinking 标签内容，只保留可解析规划文本。 ======
def _strip_think_blocks(text: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", str(text or ""))
    cleaned = re.sub(r"(?is)</think(?:ing)?>", "", cleaned)
    cleaned = re.sub(r"(?is)<think(?:ing)?\b[^>]*>.*", "", cleaned)
    return cleaned.strip()


# ====== 功能：生成不含敏感信息的原始响应摘要。 ======
def _safe_raw_summary(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"type": type(raw).__name__}
    return {key: raw.get(key) for key in ("code", "usage", "stream_chunks", "stream_finish_reason") if key in raw}
