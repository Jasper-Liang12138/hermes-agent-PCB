from __future__ import annotations

import json
import uuid
from typing import Any


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
def parse_user_message(data: dict[str, Any]) -> tuple[str, str, str] | None:
    data = _payload(data)
    if data.get("type") != "message":
        return None
    body = data.get("body") or {}
    if body.get("role") != "user":
        return None
    session_id = str(data.get("sessionId") or body.get("sessionId") or "")
    project_id = str(data.get("projectid") or data.get("projectId") or data.get("projectID") or body.get("projectid") or body.get("projectId") or body.get("projectID") or "")
    content = str(body.get("content") or "")
    return session_id, project_id, content


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





