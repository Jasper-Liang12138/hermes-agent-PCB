from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from pcb_agent_langgraph.entry import ENTRY_ACTION_KEYS, ENTRY_MODULE_KEYS, normalize_entry_module


@dataclass(frozen=True)
class UserMessage:
    session_id: str
    project_id: str
    content: str
    entry_module: str = ""
    entry_action: str = ""
    entry_payload: dict[str, Any] = field(default_factory=dict)

    # 兼容旧代码和旧测试中的三元组解包：session_id, project_id, content = parsed。
    def __iter__(self):
        yield self.session_id
        yield self.project_id
        yield self.content

    def __len__(self) -> int:
        return 3

    def __getitem__(self, index: int) -> str:
        return (self.session_id, self.project_id, self.content)[index]


# ====== 功能：兼容前端直接消息和 {payload: ...} 包裹消息。 ======
def _payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload") if isinstance(data, dict) else None
    return payload if isinstance(payload, dict) else data


# ====== 功能：把前端返回的 JSON 字符串结果解析成结构化对象。 ======
def _decode_result(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


# ====== 功能：解析前端发来的用户消息。 ======
def parse_user_message(data: dict[str, Any]) -> UserMessage | None:
    raw = data if isinstance(data, dict) else {}
    data = _payload(raw)
    message_type = str(data.get("type") or "")
    is_bga_escape = message_type.lower() == "bga_escape_routing"
    if message_type != "message" and not is_bga_escape:
        return None
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    if not is_bga_escape and body.get("role") != "user":
        return None
    content_value = body.get("content")
    content_object = content_value if isinstance(content_value, dict) else {}
    session_id = str(data.get("sessionId") or body.get("sessionId") or "")
    project_id = str(data.get("projectid") or data.get("projectId") or data.get("projectID") or body.get("projectid") or body.get("projectId") or body.get("projectID") or "")
    content = _message_content(content_value)
    entry_module = "global_fanout" if is_bga_escape else normalize_entry_module(_first_field((content_object, body, data, raw), ENTRY_MODULE_KEYS))
    decision = str(body.get("decision") or "").strip()
    entry_action = decision.lower() if is_bga_escape and decision else str(_first_field((content_object, body, data, raw), ENTRY_ACTION_KEYS) or "").strip()
    entry_payload = _entry_payload((data, body, content_object), entry_module, entry_action)
    if is_bga_escape:
        entry_payload["decision"] = decision or "NEW"
        if body.get("type") not in (None, ""):
            entry_payload["routingParamType"] = body.get("type")
    return UserMessage(session_id, project_id, content, entry_module=entry_module, entry_action=entry_action, entry_payload=entry_payload)


# ====== 功能：兼容 content 为字符串或结构化对象的用户消息正文。 ======
def _message_content(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            nested = value.get(key)
            if nested not in (None, "", [], {}):
                return str(nested)
        return ""
    return str(value or "")


# ====== 功能：从多个候选对象中读取第一个非空字段。 ======
def _first_field(sources: tuple[dict[str, Any], ...], keys: tuple[str, ...]) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


# ====== 功能：保留入口消息中的结构化附加信息，供后续节点逐步使用。 ======
def _entry_payload(sources: tuple[dict[str, Any], ...], entry_module: str, entry_action: str) -> dict[str, Any]:
    skip = {
        "payload",
        "body",
        "type",
        "role",
        "content",
        "sessionId",
        "projectid",
        "projectId",
        "projectID",
        *ENTRY_MODULE_KEYS,
        *ENTRY_ACTION_KEYS,
    }
    payload: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key in skip or value in (None, "", [], {}):
                continue
            payload[key] = value
    if entry_module:
        payload["entry_module"] = entry_module
    if entry_action:
        payload["entry_action"] = entry_action
    return payload


# ====== 功能：构造发送给前端的 Agent 文本消息。 ======
def agent_message(session_id: str, project_id: str, content: str, *, isFinal: bool = True, msgId: str | None = None, **fields: Any) -> dict[str, Any]:
    body = {"role": "agent", "msgId": str(msgId or uuid.uuid4()), "content": content, "isFinal": isFinal}
    body.update({key: value for key, value in fields.items() if value is not None})
    return {"sessionId": session_id, "projectid": project_id, "projectId": project_id, "projectID": project_id, "type": "message", "body": body}


# ====== 功能：按 v0.6 前端协议归一化需要前端执行的工具参数。 ======
def _frontend_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if name in {"getProjectData", "deleteTracesForRerouting"}:
        return None
    if name == "importLines":
        return {
            "filePath": str(arguments.get("filePath") or arguments.get("path") or ""),
            "successPins": list(arguments.get("successPins") or []),
            "failedPins": list(arguments.get("failedPins") or []),
            "requireApproval": bool(arguments.get("requireApproval", True)),
        }
    return dict(arguments)

# ====== 功能：构造发送给前端的工具调用消息。 ======
def tool_call_message(session_id: str, project_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    content = {"id": call_id, "name": name}
    normalized_arguments = _frontend_tool_arguments(name, arguments)
    if normalized_arguments is not None:
        content["arguments"] = normalized_arguments
    return {
        "sessionId": session_id,
        "projectid": project_id,
        "projectId": project_id,
        "projectID": project_id,
        "type": "tool-calls",
        "body": {"role": "agent", "content": content},
    }


# ====== 功能：构造发送给前端的错误消息。 ======
def error_message(session_id: str, project_id: str, message: str, code: int = 50001) -> dict[str, Any]:
    return {"sessionId": session_id, "projectid": project_id, "projectId": project_id, "projectID": project_id, "type": "error", "body": {"role": "agent", "code": code, "message": message}}


# ====== 功能：解析前端返回的工具结果。 ======
def parse_tool_result(data: dict[str, Any]) -> tuple[str, Any] | None:
    data = _payload(data)
    if data.get("type") not in {"tool-results", "tool-result", "tool-result-approve"}:
        return None
    body = data.get("body") or {}
    content = body.get("content") if isinstance(body.get("content"), dict) else body
    if not isinstance(content, dict):
        return None
    call_id = str(content.get("id") or content.get("callId") or body.get("id") or body.get("callId") or "")
    if not call_id:
        return None
    result = content.get("result", content.get("data", content))
    return call_id, _decode_result(result)
