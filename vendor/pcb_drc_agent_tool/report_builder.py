from typing import Dict, Any, List

from llm.prompt_builder import build_issue_analysis_prompt


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


def build_prompt_text(result: Dict[str, Any]) -> str:
    board = result["board"]
    issues = collect_selected_issues(result)
    return build_issue_analysis_prompt(board, issues)
