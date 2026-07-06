# drc_backend/api.py
import os
import time

from parser.kicad_parser_fast import parse_kicad
from engine.runner import run_hard_checks, run_optimization_checks


HARD_RULE_WEIGHTS = {
    "HR_CONNECT_PAD_NOT_ESCAPED": 5.0,
    "HR_TOPO_MULTIPLE_ESCAPE": 3.0,
    "HR_DRC_SEGMENT_CROSSING": 4.0,
    "HR_CONNECT_BRANCH_INCOMPLETE": 4.0,
   # "HR_TOPO_ENDPOINT_NOT_UNIQUE": 3.0,
   # "HR_CONNECT_ESCAPE_PATH_FORK": 3.0,
}


def summarize_issues_by_rule(issues):
    counter = {}
    for it in issues:
        counter[it.rule] = counter.get(it.rule, 0) + 1
    return dict(sorted(counter.items()))


def compute_hard_score(hard_issues):
    hard_rule_counts = summarize_issues_by_rule(hard_issues)

    hard_penalty = 0.0
    for rule, count in hard_rule_counts.items():
        hard_penalty += HARD_RULE_WEIGHTS.get(rule, 1.0) * count

    hard_score = max(0.0, 100.0 - hard_penalty * 5.0)
    hard_pass = len(hard_issues) == 0

    return {
        "hard_pass": hard_pass,
        "hard_penalty": round(hard_penalty, 3),
        "hard_score": round(hard_score, 2),
        "hard_issue_count": len(hard_issues),
        "hard_rule_counts": hard_rule_counts,
    }


def evaluate_board(board_path: str, check_mode: str = "hard"):
    t0 = time.perf_counter()

    board = parse_kicad(board_path)
    parse_time = time.perf_counter() - t0

    hard_issues = []
    opt_issues = []
    all_issues = []

    hard_time = 0.0
    opt_time = 0.0
    hard_score = None

    if check_mode in ("hard", "all"):
        h0 = time.perf_counter()
        hard_issues, _ = run_hard_checks(board, log_fn=None, with_timing=True)
        hard_time = time.perf_counter() - h0
        hard_score = compute_hard_score(hard_issues)

    if check_mode in ("opt", "all"):
        o0 = time.perf_counter()
        opt_issues, _ = run_optimization_checks(board, log_fn=None, with_timing=True)
        opt_time = time.perf_counter() - o0

    if check_mode == "hard":
        all_issues = hard_issues
    elif check_mode == "opt":
        all_issues = opt_issues
    else:
        all_issues = hard_issues + opt_issues

    whole_program_time = time.perf_counter() - t0

    return {
        "board_name": os.path.basename(board_path),
        "board_path": board_path,
        "check_mode": check_mode,
        "hard_score": hard_score,
        "hard_issues": hard_issues,
        "opt_issues": opt_issues,
        "all_issues": all_issues,
        "timing": {
            "parse_time": parse_time,
            "hard_time": hard_time,
            "opt_time": opt_time,
            "total_check_time": hard_time + opt_time,
            "whole_program_time": whole_program_time,
        },
    }


def evaluate_drc_score(board_path: str, check_mode: str = "hard"):
    try:
        result = evaluate_board(board_path, check_mode=check_mode)
        hard_score = result.get("hard_score") or {}

        return {
            "ok": True,
            "input": {
                "board_path": board_path,
                "check_mode": check_mode,
            },
            "score_name": "drc_hard_score",
            "score": hard_score.get("hard_score", 0.0),   # 0-100
            "pass": hard_score.get("hard_pass", False),
            "details": {
                "hard_penalty": hard_score.get("hard_penalty", 0.0),
                "hard_issue_count": hard_score.get("hard_issue_count", 0),
                "hard_rule_counts": hard_score.get("hard_rule_counts", {}),
            },
            "artifacts": {
                "issues": [x.to_dict() for x in result.get("all_issues", [])],
                "timing": result.get("timing", {}),
            },
            "error": None,
        }

    except Exception as e:
        return {
            "ok": False,
            "input": {
                "board_path": board_path,
                "check_mode": check_mode,
            },
            "score_name": "drc_hard_score",
            "score": 0.0,
            "pass": False,
            "details": {},
            "artifacts": {},
            "error": {
                "type": type(e).__name__,
                "message": str(e),
            },
        }