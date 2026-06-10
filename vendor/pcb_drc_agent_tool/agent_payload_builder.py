from typing import Dict, Any, List


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

    return {
        "board": {
            "board_name": result.get("board_name", ""),
            "pcb_path": result.get("pcb_path", ""),
            "target_bga": precheck.get("target_bga", ""),
            "target_bga_pad_count": precheck.get("target_bga_pad_count", 0),
        },
        "constraints": {
            "check_mode": result.get("check_mode", ""),
            "signal_net_count": precheck.get("signal_net_count", 0),
            "filtered_out_net_count": precheck.get("filtered_out_net_count", 0),
            "candidate_diff_pair_count": precheck.get("candidate_diff_pair_count", 0),
        },
        "issues": [x.to_dict() for x in issues],
    }
