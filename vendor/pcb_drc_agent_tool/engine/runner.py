import time
import traceback
from typing import List,Tuple

from model.issue import Issue
from rules.hard_rules import (
    check_p1_all_named_bga_pads_escaped,
    check_p2_single_escape_per_bga_pad,
    check_p7_segment_crossing,
    check_h6_pad_segment_crossing,
    #check_h2_dangling_segment,
    check_h3_branch_escape_incomplete,
    #check_h4_endpoint_not_unique,
    #check_h5_escape_path_no_fork,
)
from rules.optimization_rules import (
    check_p3_inner_pad_must_use_via,
    check_p4_via_at_cell_center,
    check_p5_via_45deg,
    #check_w123_top_fanout_width,
    check_w9_w10_w11_fanout_via_size,
)

from rules.differential_rules import (
    check_d1_pair_net_not_found,
    check_d2_pair_via_count_mismatch,
    check_d3_diff_length_mismatch,
    check_d4_diff_width_invalid,
    check_d5_diff_pair_gap_invalid,
)

#定义一个通用的函数来运行规则并测量时间，同时捕获异常并转换为问题
def _timed_rule(label, fn, board, log_fn=None, isolate_exceptions=True):
    t0 = time.perf_counter()

    try:
        issues = fn(board)
        dt = time.perf_counter() - t0

        if log_fn:
            log_fn(f"[RULE TIMING] {label}: {dt:.3f}s ({len(issues)} issues)")

        return issues, dt

    except Exception as e:
        dt = time.perf_counter() - t0

        if log_fn:
            log_fn(f"[RULE ERROR] {label}: {type(e).__name__}: {e}")
            log_fn(traceback.format_exc().rstrip())
            log_fn(f"[RULE TIMING] {label}: failed after {dt:.3f}s (0 issues)")

        if not isolate_exceptions:
            raise

        error_issue = Issue(
            rule=label,
            severity="ERROR",
            message=f"Rule execution failed: {type(e).__name__}: {e}",
            category="internal",
            suggestion="Check this rule implementation and inspect the traceback in the log.",
            extra={
                "exception_type": type(e).__name__,
                "exception_message": str(e),
                "traceback": traceback.format_exc(),
            },
        )

        return [error_issue], dt
#提供一些方便的函数来获取问题数量
def get_hard_issue_count(board) -> int:
    issues, _ = run_hard_checks(board)
    return len(issues)


def get_optimization_issue_count(board) -> int:
    issues, _ = run_optimization_checks(board)
    return len(issues)

#提供一个函数来运行所有Hard规则并返回问题列表
def run_hard_checks(board, log_fn=None, with_timing=False):
    issues = []
    timings = []

    hard_rules = [
        ("HR_CONNECT_PAD_NOT_ESCAPED", check_p1_all_named_bga_pads_escaped),
        # Temporarily disabled: multiple initial escape branch check.
        # ("HR_TOPO_MULTIPLE_ESCAPE", lambda board: check_p2_single_escape_per_bga_pad(board, log_fn=log_fn)),
        ("HR_DRC_SEGMENT_CROSSING", lambda board: check_p7_segment_crossing(board, log_fn=log_fn)),
        ("HR_DRC_PAD_SEGMENT_CROSSING", lambda board: check_h6_pad_segment_crossing(board, log_fn=log_fn)),
        ("HR_CONNECT_BRANCH_INCOMPLETE", lambda board: check_h3_branch_escape_incomplete(board, log_fn=log_fn)),
        #("HR_TOPO_ENDPOINT_NOT_UNIQUE", check_h4_endpoint_not_unique),
        #("HR_CONNECT_ESCAPE_PATH_FOR", check_h5_escape_path_no_fork),
    ]

    for label, fn in hard_rules:
        sub_issues, dt = _timed_rule(label, fn, board, log_fn=log_fn)
        issues.extend(sub_issues)
        timings.append((label, dt, len(sub_issues)))

    if with_timing:
        return issues, timings
    return issues

def run_optimization_checks(board, log_fn=None, with_timing=False):
    issues = []
    timings = []

    opt_rules = [
        ("OR_INNER_PAD_MUST_VIA", check_p3_inner_pad_must_use_via),
        ("OR_VIA_NOT_AT_CELL_CENTER", check_p4_via_at_cell_center),
        ("OR_VIA_NOT_45_DEG", check_p5_via_45deg),
        #("OR_TOP_FANOUT_WIDTH_TOO_SMALL", check_w123_top_fanout_width),
        ("OR_FANOUT_VIA_SIZE_INVALID", check_w9_w10_w11_fanout_via_size),
    ]

    for label, fn in opt_rules:
        sub_issues, dt = _timed_rule(label, fn, board, log_fn=log_fn)
        issues.extend(sub_issues)
        timings.append((label, dt, len(sub_issues)))

    if with_timing:
        return issues, timings
    return issues

def run_differential_checks(board, log_fn=None, with_timing=False):
    issues = []
    timings = []

    diff_rules = [
        ("DR_DIFF_PAIR_NET_NOT_FOUND", lambda board: check_d1_pair_net_not_found(board, log_fn=log_fn)),
        ("DR_PAIR_VIA_COUNT_MISMATCH", lambda board: check_d2_pair_via_count_mismatch(board, log_fn=log_fn)),
        ("DR_DIFF_LENGTH_MISMATCH", lambda board: check_d3_diff_length_mismatch(board, log_fn=log_fn)),
        ("DR_DIFF_WIDTH_INVALID", lambda board: check_d4_diff_width_invalid(board, log_fn=log_fn)),
        ("DR_DIFF_PAIR_GAP_INVALID", lambda board: check_d5_diff_pair_gap_invalid(board, log_fn=log_fn)),
    ]

    for label, fn in diff_rules:
        sub_issues, dt = _timed_rule(label, fn, board, log_fn=log_fn)
        issues.extend(sub_issues)
        timings.append((label, dt, len(sub_issues)))

    if with_timing:
        return issues, timings
    return issues

#提供一个函数来运行所有规则并返回问题列表，同时可选地返回每条规则的执行时间
def run_all_checks(board, log_fn=None, with_timing=False):
    """
    Run all checks and return issues and timings.
    """

    hard_issues, hard_timings = run_hard_checks(board, log_fn=log_fn, with_timing=True)
    opt_issues, opt_timings = run_optimization_checks(board, log_fn=log_fn, with_timing=True)

    if with_timing:
        return hard_issues, opt_issues, hard_timings, opt_timings
    return hard_issues, opt_issues
"""
def run_regular_checks(board) -> List[Issue]:
    hard_issues, opt_issues = run_all_checks(board)
    return hard_issues + opt_issues
"""
