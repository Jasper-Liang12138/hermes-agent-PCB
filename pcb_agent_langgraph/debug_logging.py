from __future__ import annotations

import contextlib
import contextvars
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from pcb_agent_langgraph.utils.config import DebugLogConfig


_LOGGER: contextvars.ContextVar[AgentDebugLogger | None] = contextvars.ContextVar("agent_debug_logger", default=None)
_SECRET_KEYS = {"api_key", "apikey", "authorization", "auth", "token", "access_token", "refresh_token", "password", "secret"}


class AgentDebugLogger:
    # ====== 功能：把一次 Agent turn 的完整调试事件写入 JSONL 文件。 ======
    def __init__(self, config: DebugLogConfig, *, run_id: str, session_id: str, project_id: str, root: str | Path | None = None) -> None:
        self.config = config
        self.run_id = str(run_id)
        self.session_id = str(session_id)
        self.project_id = str(project_id)
        self.enabled = bool(config.enabled)
        self._seq = 0
        self._lock = threading.Lock()
        self.path: Path | None = None
        if self.enabled:
            base = Path(config.dir)
            if not base.is_absolute() and root is not None:
                base = Path(root) / base
            day = datetime.now().strftime("%Y-%m-%d")
            session_dir = base / day / f"session-{_safe_path_part(self.session_id)}"
            session_dir.mkdir(parents=True, exist_ok=True)
            self.path = session_dir / f"run-{_safe_path_part(self.run_id)}.jsonl"

    # ====== 功能：记录一个结构化事件，失败时不影响主业务流程。 ======
    def log(self, event: str, payload: dict[str, Any] | None = None) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            with self._lock:
                self._seq += 1
                row = {
                    "ts": time.time(),
                    "seq": self._seq,
                    "run_id": self.run_id,
                    "session_id": self.session_id,
                    "project_id": self.project_id,
                    "event": event,
                    "payload": _redact(payload or {}) if self.config.redact_secrets else _jsonable(payload or {}),
                }
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            if self.config.print:
                print(_summary_line(event, row, self.path))
        except Exception as exc:
            if self.config.print:
                print(f"agent_log_error event={event} run={self.run_id} error={exc}")


@contextlib.contextmanager
def agent_debug_context(logger: AgentDebugLogger | None) -> Iterator[None]:
    # ====== 功能：把 logger 绑定到当前 async context，供模型、工具和节点自动读取。 ======
    token = _LOGGER.set(logger)
    try:
        yield
    finally:
        _LOGGER.reset(token)


def current_debug_logger() -> AgentDebugLogger | None:
    return _LOGGER.get()


def log_debug_event(event: str, payload: dict[str, Any] | None = None) -> None:
    logger = current_debug_logger()
    if logger is not None:
        logger.log(event, payload or {})


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return _jsonable(value)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except Exception:
        return str(value)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return lowered in _SECRET_KEYS or lowered.endswith("_api_key") or lowered.endswith("_token")


def _safe_path_part(value: str) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120]


def _summary_line(event: str, row: dict[str, Any], path: Path) -> str:
    payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
    parts = [f"agent_log event={event}", f"run={row.get('run_id')}"]
    for key in ("tool", "node", "elapsed_ms", "ok"):
        if key in payload:
            parts.append(f"{key}={payload.get(key)}")
    if isinstance(payload.get("usage"), dict):
        parts.append(f"usage={json.dumps(payload.get('usage'), ensure_ascii=False, separators=(',', ':'))}")
    parts.append(f"log={path}")
    return " ".join(parts)
