"""Stable human-readable summaries for structured restore results."""

from __future__ import annotations

from typing import Any


def _format_version(value: Any) -> str:
    if value in (None, "", "current", "latest", "last"):
        if value in {"current", "latest", "last"}:
            return str(value)
        return "unknown"
    try:
        return f"v{int(value):03d}"
    except (TypeError, ValueError):
        return str(value)


def _format_mapping(mapping: Any) -> str:
    if not isinstance(mapping, dict):
        return ""
    parts: list[str] = []
    for key, value in mapping.items():
        if value in (None, "", [], {}):
            continue
        parts.append(f"{key}={value}")
    return "，".join(parts)


def render_restore_summary(fields: dict[str, Any] | None) -> str:
    data = fields if isinstance(fields, dict) else {}
    restored_kind = str(data.get("restoredKind") or "").strip().lower()
    restored_version = _format_version(data.get("restoredFromVersion"))
    changed_fields = [str(item) for item in (data.get("changedFields") or []) if str(item).strip()]
    changed_text = "、".join(changed_fields) if changed_fields else "关键字段"
    previous_text = _format_mapping(data.get("previousValues"))
    current_text = _format_mapping(data.get("currentValues"))

    if restored_kind == "layout":
        lines = [
            f"已恢复版图检查点 {restored_version}。",
            f"本次变更涉及：{changed_text}。",
        ]
        if current_text:
            lines.append(f"当前状态：{current_text}。")
        if data.get("requiresReimport"):
            lines.append("下一步通常需要重新导入或继续后续版图动作。")
        return "\n".join(lines)

    lines = [
        f"已恢复 fanout 参数版本 {restored_version}。",
        f"本次变更涉及：{changed_text}。",
    ]
    if previous_text:
        lines.append(f"恢复前：{previous_text}。")
    if current_text:
        lines.append(f"恢复后：{current_text}。")
    if data.get("requiresReroute"):
        lines.append("请确认是否按这份参数重新执行布线。")
    return "\n".join(lines)
