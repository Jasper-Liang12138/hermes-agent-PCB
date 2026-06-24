"""SWSD-owned PCB response finalization helpers."""

from __future__ import annotations

import re
from typing import Any, Dict



class SWSDResponseBuilder:
    """Build fact-grounded frontend PCB fields.

    LLM/agent narrative may be attached as text, but structured facts are always
    derived from tool/runtime fields passed into this builder.
    """

    @staticmethod
    def reroute_final(fields: Dict[str, Any] | None, *, visible_text: str = "") -> Dict[str, Any]:
        data = dict(fields or {})
        reroute_result = data.get("rerouteResult") if isinstance(data.get("rerouteResult"), dict) else {}
        check_report = data.get("checkReport") if isinstance(data.get("checkReport"), dict) else {}
        explanation = str(data.get("explanation") or visible_text or "").strip()

        drc_passed_hint = SWSDResponseBuilder._mentions_drc_pass(visible_text)
        if not reroute_result.get("status"):
            reroute_result["status"] = "drc_passed_import_pending" if drc_passed_hint else "reroute_finalized"
        if "drcPassed" not in reroute_result:
            reroute_result["drcPassed"] = bool(check_report.get("passed") or drc_passed_hint)
        if "passed" not in check_report:
            check_report["passed"] = bool(reroute_result.get("drcPassed") or drc_passed_hint)
        if not explanation:
            explanation = "已完成拆线重布检查，结果已整理为结构化输出。"

        data["rerouteResult"] = reroute_result
        data["checkReport"] = check_report
        data["explanation"] = explanation
        return data

    @staticmethod
    def recoverable_reroute_error(reason: str, *, status: str = "needs_selection") -> Dict[str, Any]:
        detail = str(reason or "缺少可继续拆线重布的前端上下文。").strip()
        return {
            "rerouteResult": {
                "status": status,
                "recoverable": True,
                "reason": detail,
            },
            "checkReport": {
                "passed": False,
                "errors": [detail],
                "warnings": ["请补充框选走线或恢复可用 reroute 上下文后重试。"],
            },
            "explanation": f"拆线重布未能继续：{detail}",
        }

    @staticmethod
    def attach_narrative(fields: Dict[str, Any], narrative_text: str) -> Dict[str, Any]:
        data = dict(fields or {})
        text = str(narrative_text or "").strip()
        if text:
            data["narrativeText"] = text
        return data

    @staticmethod
    def _mentions_drc_pass(text: str) -> bool:
        return bool(re.search(r"DRC.{0,24}(通过|passed)", str(text or ""), flags=re.IGNORECASE))





