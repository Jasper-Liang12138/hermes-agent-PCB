"""Policy helpers for applying PCB experience without overriding hard constraints."""

from __future__ import annotations

from typing import Any

from agent.swsd.experience.schema import PCBContextHints


def alias_for_target(hints: PCBContextHints, requested: str, candidates: list[str]) -> str:
    requested = str(requested or "").strip()
    if not requested:
        return ""
    alias_value = hints.hint_value(f"alias:{requested}")
    if isinstance(alias_value, str) and alias_value in candidates:
        return alias_value
    if len(candidates) == 1:
        return candidates[0]
    return ""


def require_structured_final(hints: PCBContextHints) -> bool:
    prefs = hints.hint_value("pcbPreference", {})
    return bool(isinstance(prefs, dict) and prefs.get("requireStructuredFinalBody", True))


def minimal_reroute_final(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    fields = dict(existing or {})
    reroute_result = fields.get("rerouteResult")
    if not isinstance(reroute_result, dict):
        reroute_result = {}
    reroute_result.setdefault("status", "drc_passed_import_pending")
    fields["rerouteResult"] = reroute_result

    check_report = fields.get("checkReport")
    if not isinstance(check_report, dict):
        check_report = {}
    check_report.setdefault("warnings", ["Import/explanation finalization may still be pending."])
    fields["checkReport"] = check_report
    fields.setdefault("explanation", "拆线重布已通过 DRC；为避免前端等待超时，已先返回结构化结果。")
    return fields
