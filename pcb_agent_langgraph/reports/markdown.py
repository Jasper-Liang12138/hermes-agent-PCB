from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


# ====== 功能：把当前 PCB reroute/QA 结果整理成前端可渲染的 Markdown 报告。 ======
def build_markdown_report(task_type: str, cache: dict[str, Any]) -> dict[str, Any]:
    drc = cache.get("drcResult") if isinstance(cache.get("drcResult"), dict) else {}
    explain = cache.get("explainabilityReport") if isinstance(cache.get("explainabilityReport"), dict) else {}
    title = "PCB Reroute Report" if task_type == "reroute" else "PCB Fanout Report" if task_type == "global_fanout" else "PCB Report"
    passed = drc.get("passed")
    status = "passed" if passed is True else str(drc.get("status") or "unknown")
    lines = [f"# {title}", "", "## DRC", *_drc_lines(drc), "", "## Explainability", *_explain_lines(explain)]
    report = "\n".join(lines).strip() + "\n"
    debug = {}
    drc_debug = drc.get("debug") if isinstance(drc.get("debug"), dict) else {}
    smoke = drc_debug.get("explainabilitySmoke") if isinstance(drc_debug.get("explainabilitySmoke"), dict) else {}
    if smoke:
        debug["explainabilitySmoke"] = smoke
        debug["explainabilitySmokeReportPath"] = smoke.get("report_path") or drc_debug.get("explainabilitySmokeReportPath") or ""
    return {
        "markdown": report,
        "drcStatus": status,
        "drcPassed": passed is True,
        "routeOutput": "",
        "explainStatus": explain.get("status") if isinstance(explain, dict) else "",
        "debug": debug,
    }


# ====== 功能：导入成功后生成旧 Hermes 风格 fanout 用户报告。 ======
def build_fanout_route_report(cache: dict[str, Any]) -> dict[str, Any]:
    route = cache.get("fanout_routeResult") if isinstance(cache.get("fanout_routeResult"), dict) else {}
    params = cache.get("fanoutParams") if isinstance(cache.get("fanoutParams"), dict) else {}
    import_result = cache.get("importLinesResult") if isinstance(cache.get("importLinesResult"), dict) else {}
    import_file = _first_text(route.get("importLinesFilePath"), route.get("routedLayoutTxtFilePath"), route.get("routingResult"))
    routing_result = _first_text(route.get("routingResult"), route.get("routedLayoutTxtFilePath"))
    router_type = _first_text(params.get("routerType"), route.get("routerType"))
    selected_bga = _first_text(params.get("selectedBGA"), params.get("targetBGA"))
    base_report = _read_work_dir_report(_first_text(route.get("workDir")))
    if base_report == "布线完成（无详细报告）":
        base_report = _first_text(route.get("report")) or base_report
    param_report = _fanout_param_report(params)
    report_text = (base_report.rstrip() + "\n\n层分配和逃逸顺序生成报告：\n" + param_report).strip()
    return {
        "task": "global_fanout",
        "stage": "result_review",
        "selectedBGA": selected_bga,
        "routerType": router_type,
        "routingResult": routing_result,
        "importLinesFilePath": import_file,
        "routeOutput": import_file,
        "workDir": route.get("workDir"),
        "report": report_text,
        "markdown": report_text,
        "importStatus": import_result.get("status") or import_result.get("result") or "completed",
        "layerSummary": _layer_assignment_summary(params)["data"],
        "escapeOrderSummary": _escape_order_summary(params)["data"],
    }


# ====== 功能：生成 DRC 报告段落。 ======


def _read_work_dir_report(work_dir_value: str) -> str:
    if work_dir_value:
        work_dir = Path(work_dir_value)
        for name in ("data.txt", "statistical.out", "statistical.txt", "report.txt", "route_report.txt"):
            path = work_dir / name
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            for encoding in ("utf-8", "gbk", "gb18030"):
                try:
                    text = path.read_text(encoding=encoding).strip()
                    if text:
                        return text
                except UnicodeDecodeError:
                    continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    return "布线完成（无详细报告）"


def _fanout_param_report(params: dict[str, Any]) -> str:
    layer = _layer_assignment_summary(params)
    order = _escape_order_summary(params)
    lines = ["层分配摘要：", *layer["lines"], "", "逃逸顺序摘要：", *order["lines"]]
    return "\n".join(lines).strip()

def _drc_lines(drc: dict[str, Any]) -> list[str]:
    if not drc:
        return ["- DRC result is not available."]
    lines = [
        f"- Status: `{drc.get('status', 'unknown')}`",
        f"- Passed: `{bool(drc.get('passed'))}`",
    ]
    for key, label in (
        ("drcExecutionValid", "Execution valid"),
        ("fullBoardPassed", "Full-board passed"),
        ("targetScopedPassed", "Target-scoped passed"),
    ):
        if key in drc:
            lines.append(f"- {label}: `{bool(drc.get(key))}`")
    if drc.get("drcInputMode"):
        lines.append(f"- Input mode: `{drc.get('drcInputMode')}`")
    if drc.get("drcInputSource"):
        lines.append(f"- Input source: `{drc.get('drcInputSource')}`")
    if drc.get("routedBoardPath"):
        lines.append(f"- Routed board: `{drc.get('routedBoardPath')}`")
    if drc.get("routedTextChars") is not None:
        lines.append(f"- Routed text chars: `{drc.get('routedTextChars')}`")
    if drc.get("targetNets"):
        lines.append(f"- Target nets: `{', '.join(str(item) for item in drc.get('targetNets') or [])}`")
    if drc.get("targetIssueCount") is not None:
        lines.append(f"- Target issue count: `{drc.get('targetIssueCount')}`")
    if drc.get("fullBoardIssueCount") is not None:
        lines.append(f"- Full-board issue count: `{drc.get('fullBoardIssueCount')}`")
    if drc.get("score") is not None:
        lines.append(f"- Score: `{drc.get('score')}`")
    errors = drc.get("errors") or []
    if "fullBoardPassed" in drc:
        lines.append(f"- Full-board DRC passed: `{bool(drc.get('fullBoardPassed'))}`")
    if "targetScopedPassed" in drc:
        lines.append(f"- Target-scoped DRC passed: `{bool(drc.get('targetScopedPassed'))}`")
    target_nets = drc.get("targetNets") or []
    if target_nets:
        lines.append(f"- Target nets: `{', '.join(str(item) for item in target_nets)}`")
    if drc.get("targetIssueCount") is not None:
        lines.append(f"- Target issue count: `{drc.get('targetIssueCount')}`")
    if drc.get("fullBoardIssueCount") is not None:
        lines.append(f"- Full-board issue count: `{drc.get('fullBoardIssueCount')}`")
    input_mode = drc.get("drcInputMode")
    input_source = drc.get("drcInputSource")
    routed_board_path = drc.get("routedBoardPath")
    routed_text_chars = drc.get("routedTextChars")
    if input_mode:
        lines.append(f"- DRC input mode: `{input_mode}`")
    if input_source:
        lines.append(f"- DRC input source: `{input_source}`")
    if routed_board_path:
        lines.append(f"- DRC board input: `{routed_board_path}`")
    if routed_text_chars is not None:
        lines.append(f"- Routed text chars: `{routed_text_chars}`")
    if errors:
        lines.append("- Errors:")
        lines.extend(f"  - {_short_text(item)}" for item in errors[:8])
    for key, title in (("targetIssues", "Target issues"), ("fullBoardIssues", "Full-board/residual issues")):
        issues = drc.get(key) or []
        if issues:
            lines.append(f"- {title}:")
            lines.extend(f"  - {_short_text(item)}" for item in issues[:5])
    detail = drc.get("detail") if isinstance(drc.get("detail"), dict) else {}
    summary = detail.get("failure_summary") or detail.get("reason")
    if summary:
        lines.append(f"- Detail: {_short_text(summary)}")
    target_issues = drc.get("targetDrcIssues") if isinstance(drc.get("targetDrcIssues"), list) else []
    residual_issues = drc.get("fullBoardResidualIssues") if isinstance(drc.get("fullBoardResidualIssues"), list) else []
    if target_issues:
        lines.append("- Target DRC issues:")
        lines.extend(f"  - {_short_text((item or {}).get('message') or item, limit=500)}" for item in target_issues[:8])
    if residual_issues:
        lines.append("- Full-board residual issues not on target nets:")
        lines.extend(f"  - {_short_text((item or {}).get('message') or item, limit=500)}" for item in residual_issues[:8])
    tool_path = drc.get("tool_path")
    eval_root = drc.get("eval_root")
    if tool_path:
        lines.append(f"- DRC tool: `{tool_path}`")
    if eval_root:
        lines.append(f"- Eval root: `{eval_root}`")
    return lines

# ====== 功能：生成可解释性报告段落。 ======
def _explain_lines(explain: dict[str, Any]) -> list[str]:
    if not explain:
        return ["- Explainability report is not available."]
    lines = [f"- Status: `{explain.get('status', 'unknown')}`"]
    prediction = explain.get("prediction")
    if prediction:
        lines.append(f"- Prediction: `{_short_text(json.dumps(prediction, ensure_ascii=False))}`")
    reason = explain.get("reason")
    if reason:
        lines.append(f"- Reason: {_short_text(reason)}")
    stderr = explain.get("stderr")
    if stderr:
        lines.append(f"- Stderr: {_short_text(stderr, limit=800)}")
    stdout = explain.get("stdout")
    if stdout and not explain.get("report"):
        lines.append(f"- Stdout: {_short_text(stdout, limit=800)}")
    command = explain.get("command")
    if command:
        lines.append(f"- Command: `{_short_text(command, limit=500)}`")
    report = explain.get("report")
    if report:
        lines.append("")
        lines.append(_short_text(report, limit=1600))
    report_path = explain.get("report_path")
    if report_path:
        lines.append(f"- Report path: `{report_path}`")
    return lines


# ====== 功能：按任务类型选择布线结果对象。 ======
def _route_result(cache: dict[str, Any], task_type: str) -> dict[str, Any]:
    keys = ("rerouteResult", "helpPlannerResult", "fanout_routeResult") if task_type == "reroute" else ("fanout_routeResult", "rerouteResult", "helpPlannerResult")
    for key in keys:
        value = cache.get(key)
        if isinstance(value, dict):
            return value
    return {}


# ====== 功能：返回第一个非空文本。 ======
def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ====== 功能：限制报告内单项文本长度，避免前端消息过大。 ======
def _short_text(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _completion_stats(route: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    rate = _numeric(route.get("completionRate"), route.get("completion_rate"), route.get("successRate"), route.get("success_rate"), route.get("routeRate"), route.get("routingRate"))
    stdout_text = "\n".join(str(route.get(key) or "") for key in ("stdout", "stderr", "report"))
    if rate is None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", stdout_text)
        if match:
            rate = float(match.group(1)) / 100.0
    success = _integer(route.get("successCount"), route.get("success_count"), route.get("routedCount"), route.get("routed_count"))
    failed = _integer(route.get("failedCount"), route.get("failed_count"), route.get("unroutedCount"), route.get("unrouted_count"))
    target = _integer(route.get("targetCount"), route.get("target_count"), route.get("totalCount"), route.get("total_count"))
    if target is None:
        target = _order_target_count(params)
    if success is None and failed is None and rate is not None and target:
        success = int(round(float(rate) * target))
        failed = max(0, target - success)
    elif success is not None and failed is None and target is not None:
        failed = max(0, target - success)
    elif failed is not None and success is None and target is not None:
        success = max(0, target - failed)
    if rate is None and success is not None and target:
        rate = success / target
    return {
        "completionRate": rate,
        "completionRateText": f"{rate * 100:.2f}%" if isinstance(rate, (int, float)) else "未返回布通率",
        "successCount": success if success is not None else "未知",
        "failedCount": failed if failed is not None else "未知",
        "targetCount": target if target is not None else "未知",
    }


def _layer_assignment_summary(params: dict[str, Any]) -> dict[str, Any]:
    path = _first_text(params.get("layerInputPath"))
    rows = _read_lines(path)
    layers = []
    for line in rows:
        parts = line.split()
        if len(parts) >= 2 and not parts[0].isdigit():
            layers.append(parts[-1])
    counts = Counter(layers)
    lines = [f"- 文件: `{path}`"] if path else []
    if counts:
        lines.append("- 层分配统计: " + ", ".join(f"{layer}: {count}" for layer, count in counts.most_common()))
    elif rows:
        lines.append(f"- 层分配文件共 {len(rows)} 行，未解析到层统计。")
    else:
        lines.append("- 未找到层分配明细。")
    examples = rows[:8]
    if examples:
        lines.append("- 示例:")
        lines.extend(f"  - `{_short_text(item, 180)}`" for item in examples)
    return {"lines": lines, "data": {"path": path, "counts": dict(counts), "examples": examples}}


def _escape_order_summary(params: dict[str, Any]) -> dict[str, Any]:
    raw_lines = params.get("orderLines") if isinstance(params.get("orderLines"), list) else []
    rows = [str(item).strip() for item in raw_lines if str(item).strip()]
    if not rows:
        rows = _read_lines(_first_text(params.get("orderInputPath")))
    parsed = []
    for line in rows:
        parts = line.split()
        if len(parts) >= 3 and not parts[0].isdigit():
            parsed.append({"net": parts[0], "layer": parts[-2], "order": parts[-1], "raw": line})
    counts = Counter(item["layer"] for item in parsed)
    lines = [f"- 逃逸顺序总行数: `{len(rows)}`"]
    if parsed:
        lines.append("- 按层统计: " + ", ".join(f"{layer}: {count}" for layer, count in counts.most_common()))
        lines.append("- 示例:")
        for item in parsed[:8]:
            lines.append(f"  - `{item['net']}` -> `{item['layer']}` / order `{item['order']}`")
    else:
        lines.append("- 未解析到 net/layer/order 明细。")
    return {"lines": lines, "data": {"totalRows": len(rows), "counts": dict(counts), "examples": parsed[:8]}}


def _read_lines(path_value: str) -> list[str]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def _order_target_count(params: dict[str, Any]) -> int | None:
    lines = params.get("orderLines") if isinstance(params.get("orderLines"), list) else []
    count = sum(1 for item in lines if isinstance(item, str) and len(item.split()) >= 3 and not item.split()[0].isdigit())
    return count or None


def _numeric(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 100.0 if number > 1.0 else number
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if match:
                number = float(match.group(0))
                return number / 100.0 if "%" in value or number > 1.0 else number
    return None


def _integer(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"\d+", value)
            if match:
                return int(match.group(0))
    return None
