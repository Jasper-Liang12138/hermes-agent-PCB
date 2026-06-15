from collections import Counter
from typing import Any, Dict, List

from rules.rule_helpers.connectivity import _build_bga_bbox, _estimate_bga_pitch


RULE_ZH = {
    "HR_CONNECT_PAD_NOT_ESCAPED": {
        "name": "BGA焊盘未逃逸",
        "explanation": "该BGA信号焊盘没有检测到从焊盘引出的初始逃逸线段。",
        "impact": "该焊盘不能视为已完成逃逸，布线结果存在硬性连通性问题。",
        "suggestion": "从该焊盘补充fanout线段，或连接到有效过孔/逃逸路径。",
    },
    "HR_TOPO_MULTIPLE_ESCAPE": {
        "name": "同一焊盘存在多条初始逃逸",
        "explanation": "同一个BGA焊盘检测到多个初始逃逸选择。",
        "impact": "逃逸拓扑不唯一，可能导致分叉、重复连接或后续布线歧义。",
        "suggestion": "保留一条明确的逃逸路径，移除多余分支。",
    },
    "HR_DRC_SEGMENT_CROSSING": {
        "name": "同层异网线段冲突",
        "explanation": "不同网络的铜线段在同一层发生交叉或重叠。",
        "impact": "这是直接的电气/几何硬性违规，通常会导致布线不可用。",
        "suggestion": "调整其中一条线段的路径、层或过孔位置，消除同层异网冲突。",
    },
    "HR_DRC_PAD_SEGMENT_CROSSING": {
        "name": "BGA区域内异网焊盘与线段冲突",
        "explanation": "目标BGA区域内，不同网络的焊盘与铜线段发生实体重叠。",
        "impact": "该冲突可能造成不同网络短路，属于Hard错误。",
        "suggestion": "调整线段路径、层或焊盘附近的扇出方式，消除异网重叠。",
    },
    "HR_CONNECT_BRANCH_INCOMPLETE": {
        "name": "逃逸分支未完成",
        "explanation": "从BGA焊盘出发的逃逸分支没有有效走出BGA区域。",
        "impact": "该逃逸路径不完整，不能视为有效escape routing结果。",
        "suggestion": "继续延伸该分支，直到其连通到BGA区域外的有效终点。",
    },
    "HR_TOPO_ENDPOINT_NOT_UNIQUE": {
        "name": "逃逸终点不唯一",
        "explanation": "单个BGA焊盘的逃逸路径对应多个外部终点。",
        "impact": "拓扑不清晰，可能说明路径存在分叉或重复连接。",
        "suggestion": "检查并整理该焊盘的逃逸路径，使其只有一个明确外部终点。",
    },
    "HR_CONNECT_ESCAPE_PATH_FORK": {
        "name": "BGA区域内逃逸路径分叉",
        "explanation": "BGA内部逃逸路径出现分叉。",
        "impact": "逃逸拓扑不再是单一路径，可能导致连接关系不可解释。",
        "suggestion": "移除分叉，保留一条从焊盘到外部终点的连续路径。",
    },
}

SEVERITY_ZH = {
    "ERROR": "错误",
    "WARNING": "警告",
    "INFO": "信息",
}


def _selected_modes(result: Dict[str, Any]) -> set:
    check_mode = (result.get("check_mode", "") or "hard").strip().lower()
    if check_mode == "all":
        return {"hard", "opt", "diff"}
    return {x.strip() for x in check_mode.split(",") if x.strip()}


def collect_selected_issues(result: Dict[str, Any]) -> List:
    modes = _selected_modes(result)
    issues = []
    if "hard" in modes:
        issues.extend(result.get("hard_issues", []))
    if "opt" in modes:
        issues.extend(result.get("opt_issues", []))
    if "diff" in modes:
        issues.extend(result.get("diff_issues", []))
    return issues


def _board_layer_names(board) -> List[str]:
    if not board:
        return []

    layers_table = getattr(board, "layers_table", {}) or {}
    id_to_name = layers_table.get("id_to_name", {}) or {}
    if id_to_name:
        return sorted({str(name) for name in id_to_name.values() if name})

    names = set()
    for seg in getattr(board, "segments", []):
        if getattr(seg, "layer", ""):
            names.add(seg.layer)
    for pad in getattr(board, "pads", []):
        if getattr(pad, "layer", ""):
            names.add(pad.layer)
    for via in getattr(board, "vias", []):
        if getattr(via, "start_layer", ""):
            names.add(via.start_layer)
        if getattr(via, "end_layer", ""):
            names.add(via.end_layer)
    return sorted(names)


def _copper_layer_names(board) -> List[str]:
    if not board:
        return []

    layers_table = getattr(board, "layers_table", {}) or {}
    id_to_name = layers_table.get("id_to_name", {}) or {}
    standard_copper_names = {
        str(name)
        for name in id_to_name.values()
        if name and str(name).lower().endswith(".cu")
    }
    if standard_copper_names:
        return sorted(standard_copper_names)

    # Older/custom boards may use names such as Top/GND02/POWER05/Bottom.
    # For those files, copper layers normally occupy KiCad IDs 0..31.
    copper_from_table = {
        str(name)
        for layer_id, name in id_to_name.items()
        if isinstance(layer_id, int) and 0 <= layer_id <= 31 and name
    }
    if copper_from_table:
        return sorted(copper_from_table)

    names = set()
    for seg in getattr(board, "segments", []):
        if getattr(seg, "layer", ""):
            names.add(seg.layer)
    for pad in getattr(board, "pads", []):
        if getattr(pad, "layer", ""):
            names.add(pad.layer)
    return sorted(names)


def _board_info(result: Dict[str, Any]) -> Dict[str, Any]:
    board = result.get("board")
    precheck = result.get("precheck_summary", {}) or {}
    all_layer_names = _board_layer_names(board)
    copper_layer_names = _copper_layer_names(board)
    return {
        "target_bga": precheck.get("target_bga", ""),
        "target_bga_pad_count": precheck.get("target_bga_pad_count", 0),
        "layer_count": len(copper_layer_names),
        "layer_names": copper_layer_names,
        "all_layer_count": len(all_layer_names),
        "all_layer_names": all_layer_names,
        "net_count": precheck.get("all_net_count", 0),
        "signal_net_count": precheck.get("signal_net_count", 0),
    }


def _format_point(x, y) -> str:
    if x is None or y is None:
        return "坐标未知"
    return f"({x:.6f}, {y:.6f})"


def _location_text(issue) -> str:
    parts = []
    if issue.obj1:
        parts.append(f"对象1={issue.obj1}")
    if issue.obj2:
        parts.append(f"对象2={issue.obj2}")
    if issue.net:
        parts.append(f"网络={issue.net}")
    if issue.layer:
        parts.append(f"层={issue.layer}")
    parts.append(f"坐标={_format_point(issue.x, issue.y)}")

    extra = issue.extra or {}
    if extra.get("seg1_start") and extra.get("seg1_end"):
        parts.append(f"线段1={extra.get('seg1_start')}->{extra.get('seg1_end')}")
    if extra.get("seg2_start") and extra.get("seg2_end"):
        parts.append(f"线段2={extra.get('seg2_start')}->{extra.get('seg2_end')}")
    return "，".join(parts)


def issue_to_zh_dict(issue) -> Dict[str, Any]:
    rule_info = RULE_ZH.get(issue.rule, {})
    extra = issue.extra or {}
    component = issue.component or extra.get("component", "")
    pad_id = issue.pad_id or extra.get("pad_id", "") or issue.obj1

    return {
        "issue_id": issue.normalized_issue_id(),
        "rule": issue.rule,
        "rule_name_zh": rule_info.get("name", issue.rule),
        "severity": issue.severity,
        "severity_zh": SEVERITY_ZH.get(issue.severity, issue.severity),
        "problem_zh": rule_info.get("explanation", issue.message),
        "impact_zh": rule_info.get("impact", "该问题可能影响当前布线结果的合法性或可用性。"),
        "suggestion_zh": rule_info.get("suggestion", issue.suggestion or "请结合对象、网络、层和坐标检查该问题。"),
        "location_zh": _location_text(issue),
        "source_message": issue.message,
        "location": {
            "component": component,
            "pad_id": pad_id,
            "net": issue.net,
            "layer": issue.layer,
            "x": issue.x,
            "y": issue.y,
            "obj1": issue.obj1,
            "obj2": issue.obj2,
        },
        "extra": extra,
    }


def _routing_metrics_zh(result: Dict[str, Any]) -> Dict[str, Any]:
    metrics = result.get("routing_metrics", {}) or {}
    return {
        "signal_pad_count": metrics.get("signal_pad_count", 0),
        "valid_escape_pad_count": metrics.get("valid_escape_pad_count", 0),
        "failed_escape_pad_count": metrics.get("failed_escape_pad_count", 0),
        "escape_completion_rate": metrics.get("escape_completion_rate", 0.0),
        "escape_completion_rate_text": f"{metrics.get('escape_completion_rate', 0.0):.2f}%",
    }


def build_message_zh(result: Dict[str, Any], issues_zh: List[Dict[str, Any]]) -> str:
    board_info = _board_info(result)
    routing = _routing_metrics_zh(result)
    hard_count = len(result.get("hard_issues", []))
    rule_counts = Counter(issue["rule_name_zh"] for issue in issues_zh)

    lines = [
        "DRC规则检查结果：",
        (
            f"目标BGA为 {board_info['target_bga'] or '未指定/未识别'}，目标BGA焊盘数为 "
            f"{board_info['target_bga_pad_count']}。"
        ),
        (
            f"板文件共识别到 {board_info['layer_count']} 层；信号网络数为 "
            f"{board_info['signal_net_count']}，总网络数为 {board_info['net_count']}。"
        ),
        (
            f"当前只执行 hard 规则检查；BGA逃逸布通率为 "
            f"{routing['escape_completion_rate_text']} "
            f"({routing['valid_escape_pad_count']}/{routing['signal_pad_count']} 个信号焊盘已通过逃逸连通检查)。"
        ),
        f"Hard错误总数为 {hard_count}。"
    ]

    if not issues_zh:
        lines.append("当前 hard 规则范围内未发现错误，可进入后续布线质量或差分专项检查。")
        return "\n".join(lines)

    if rule_counts:
        counts_text = "；".join(f"{name} {count} 个" for name, count in rule_counts.items())
        lines.append(f"错误类型统计：{counts_text}。")

    lines.append("具体错误位置如下：")
    for idx, issue in enumerate(issues_zh, start=1):
        lines.append(
            f"{idx}. {issue['rule_name_zh']}：{issue['location_zh']}。"
            f"建议：{issue['suggestion_zh']}"
        )

    return "\n".join(lines)


def build_chinese_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    precheck = result.get("precheck_summary", {}) or {}
    timing = result.get("timing", {}) or {}
    issues = collect_selected_issues(result)
    issues_zh = [issue_to_zh_dict(issue) for issue in issues]
    rule_counts = Counter(issue.rule for issue in issues)
    hard_count = len(result.get("hard_issues", []))
    board_info = _board_info(result)
    routing = _routing_metrics_zh(result)

    return {
        "schema_version": "drc_zh_v2",
        "language": "zh-CN",
        "status": "failed" if hard_count else "passed",
        "message_zh": build_message_zh(result, issues_zh),
        "summary_zh": {
            "conclusion": "Hard检查未通过" if hard_count else "Hard检查通过",
            "check_mode": result.get("check_mode", ""),
            "target_bga": precheck.get("target_bga", ""),
            "target_bga_pad_count": precheck.get("target_bga_pad_count", 0),
            "layer_count": board_info["layer_count"],
            "escape_completion_rate": routing["escape_completion_rate"],
            "escape_completion_rate_text": routing["escape_completion_rate_text"],
            "hard_issue_count": hard_count,
            "selected_issue_count": len(issues),
            "rule_counts": dict(sorted(rule_counts.items())),
        },
        "board_info": board_info,
        "routing_metrics": routing,
        "precheck_zh": {
            "target_bga": precheck.get("target_bga", ""),
            "target_bga_pad_count": precheck.get("target_bga_pad_count", 0),
            "all_net_count": precheck.get("all_net_count", 0),
            "signal_net_count": precheck.get("signal_net_count", 0),
            "filtered_out_net_count": precheck.get("filtered_out_net_count", 0),
            "candidate_diff_pair_count": precheck.get("candidate_diff_pair_count", 0),
        },
        "timing": timing,
        "issues_zh": issues_zh,
    }


def build_agent_ready_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    zh_payload = build_chinese_payload(result)
    hard_timings = result.get("hard_rule_timings", []) or []
    board = result.get("board")
    target_bga = zh_payload["summary_zh"].get("target_bga", "")
    pitch = _estimate_bga_pitch(board, target_bga) if board and target_bga else None
    bga_bbox = (
        _build_bga_bbox(board, target_bga, margin=pitch * 0.5 if pitch else 0.0)
        if board and target_bga
        else None
    )
    return {
        "schema_version": "drc_agent_v3",
        "language": "zh-CN",
        "tool": {
            "name": "pcb_drc_checker",
            "purpose": "检查BGA逃逸布线结果是否存在hard规则问题",
            "default_check_mode": "hard",
            "diff_check_enabled": False,
        },
        "input": {
            "check_mode": result.get("check_mode", ""),
            "target_bga": target_bga,
            "check_scope": "target_bga_region",
        },
        "scope": {
            "type": "target_bga_region",
            "target_bga": target_bga,
            "bga_bbox": bga_bbox,
            "description_zh": "交叉检查仅覆盖目标BGA焊盘包围框向外扩展半个pitch的区域。",
        },
        "enabled_rules": [item.get("rule", "") for item in hard_timings],
        "message_zh": zh_payload["message_zh"],
        "result": zh_payload["summary_zh"],
        "board_info": zh_payload["board_info"],
        "routing_metrics": zh_payload["routing_metrics"],
        "precheck": zh_payload["precheck_zh"],
        "issues": zh_payload["issues_zh"],
    }
