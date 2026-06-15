from typing import Dict, Any, List

from rules.rule_helpers.connectivity import _build_bga_bbox, _estimate_bga_pitch


def collect_selected_issues(result: Dict[str, Any]) -> List:
    check_mode = (result.get("check_mode", "") or "hard").strip().lower()

    if check_mode == "all":
        selected_modes = {"hard", "opt", "diff"}
    else:
        selected_modes = {x.strip() for x in check_mode.split(",") if x.strip()}

    issues = []
    if "hard" in selected_modes:
        issues.extend(result.get("hard_issues", []))
    if "opt" in selected_modes:
        issues.extend(result.get("opt_issues", []))
    if "diff" in selected_modes:
        issues.extend(result.get("diff_issues", []))

    return issues


def build_agent_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    precheck = result.get("precheck_summary", {}) or {}
    issues = collect_selected_issues(result)
    hard_timings = result.get("hard_rule_timings", []) or []
    board = result.get("board")
    target_bga = precheck.get("target_bga", "")
    pitch = _estimate_bga_pitch(board, target_bga) if board and target_bga else None
    bga_bbox = (
        _build_bga_bbox(board, target_bga, margin=pitch * 0.5 if pitch else 0.0)
        if board and target_bga
        else None
    )

    return {
        "schema_version": "drc_agent_v3",
        "status": "failed" if result.get("hard_issues", []) else "passed",
        "board": {
            "board_name": result.get("board_name", ""),
            "pcb_path": result.get("pcb_path", ""),
            "target_bga": target_bga,
            "target_bga_pad_count": precheck.get("target_bga_pad_count", 0),
        },
        "constraints": {
            "check_mode": result.get("check_mode", ""),
            "check_scope": "target_bga_region",
            "signal_net_count": precheck.get("signal_net_count", 0),
            "filtered_out_net_count": precheck.get("filtered_out_net_count", 0),
            "candidate_diff_pair_count": precheck.get("candidate_diff_pair_count", 0),
        },
        "scope": {
            "type": "target_bga_region",
            "target_bga": target_bga,
            "bga_bbox": bga_bbox,
        },
        "enabled_rules": [item.get("rule", "") for item in hard_timings],
        "summary": {
            "hard_issue_count": len(result.get("hard_issues", [])),
            "selected_issue_count": len(issues),
        },
        "issues": [x.to_dict() for x in issues],
    }
