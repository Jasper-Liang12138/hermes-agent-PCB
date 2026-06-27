"""Markdown report helpers for SWSD reroute."""

from __future__ import annotations

import json
from typing import Any, Dict


def build_reroute_markdown_report(fields: Dict[str, Any] | None, *, visible_text: str = "") -> str:
    data = dict(fields or {})
    reroute_result = data.get("rerouteResult") if isinstance(data.get("rerouteResult"), dict) else {}
    check_report = data.get("checkReport") if isinstance(data.get("checkReport"), dict) else {}
    explanation = str(data.get("explanation") or visible_text or "").strip()
    report_text = str(data.get("report") or visible_text or explanation or "").strip()

    lines: list[str] = ["# 拆线重布报告", ""]
    status = reroute_result.get("status") or "未知"
    drc_passed = check_report.get("passed") is True or reroute_result.get("drcPassed") is True
    lines.extend([
        "## 结果概览",
        "",
        f"- 状态：{status}",
        f"- DRC：{'通过' if drc_passed else '未通过或未获得明确结论'}",
    ])
    selected_nets = reroute_result.get("selectedNets")
    if selected_nets:
        lines.append(f"- 网络：{', '.join(str(item) for item in selected_nets)}")
    import_path = data.get("importLinesFilePath") or reroute_result.get("importLinesFilePath")
    routed_path = data.get("routedLayoutTxtFilePath") or reroute_result.get("routedLayoutTxtFilePath")
    if routed_path:
        lines.append(f"- routed layout txt：`{routed_path}`")
    if import_path:
        lines.append(f"- import lines：`{import_path}`")
    lines.append("")

    if explanation:
        lines.extend(["## 说明", "", explanation, ""])

    checks = check_report.get("checks") if isinstance(check_report.get("checks"), list) else []
    if checks:
        lines.extend(["## DRC 检查", "", "| 检查项 | 结果 | 说明 |", "| --- | --- | --- |"])
        for item in checks:
            if isinstance(item, dict):
                name = item.get("name") or item.get("id") or "check"
                passed = item.get("passed")
                detail = item.get("message") or item.get("detail") or item.get("reason") or ""
            else:
                name = "check"
                passed = ""
                detail = str(item)
            result = "通过" if passed is True else ("未通过" if passed is False else "未知")
            lines.append(f"| { _escape_table(name) } | {result} | { _escape_table(detail) } |")
        lines.append("")

    errors = check_report.get("errors") if isinstance(check_report.get("errors"), list) else []
    warnings = check_report.get("warnings") if isinstance(check_report.get("warnings"), list) else []
    if errors or warnings:
        lines.extend(["## 风险与提示", ""])
        for item in errors:
            lines.append(f"- 错误：{item}")
        for item in warnings:
            lines.append(f"- 提示：{item}")
        lines.append("")

    lines.extend(["## 完整原始报告", ""])
    if report_text:
        lines.append(report_text)
    else:
        lines.append("未获得额外文本报告。")
    lines.append("")

    lines.extend(["## 结构化事实", "", "```json", json.dumps({"rerouteResult": reroute_result, "checkReport": check_report}, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines).strip()


def _escape_table(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
