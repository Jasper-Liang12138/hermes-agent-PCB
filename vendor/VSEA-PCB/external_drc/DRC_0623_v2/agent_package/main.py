import argparse
from email import parser
import json 
import time
import os
from collections import Counter #计数统计
from rules.rule_helpers.constraints_loader import (
    load_external_constraints,
    attach_constraints_to_board,
    validate_constraints_against_board,
)

from model import board
from parser.kicad_parser_fast import (
    parse_kicad,
    #parse_modules_from_text,
)

from llm.analyzer import analyze_issues_with_llm
from engine.runner import (
    run_hard_checks,
    run_optimization_checks,
    run_differential_checks,
    #run_all_checks,
)
from rules.precheck_rules import run_precheck
from rules.metrics import compute_escape_completion_rate
from zh_report_builder import build_agent_ready_payload, build_chinese_payload


# =========================
# Logging helpers
# =========================

def _write_log_line(line: str, log_file: str = ""):
    print(line)
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def log(msg, log_file=""):
    """
    向后兼容旧接口：
    以前所有 log("xxx", log_file) 仍然可用，默认按 INFO 输出。
    """
    _write_log_line(f"[INFO] {msg}", log_file)


def log_info(msg, log_file=""):
    _write_log_line(f"[INFO] {msg}", log_file)


def log_warn(msg, log_file=""):
    _write_log_line(f"[WARN] {msg}", log_file)


def log_error(msg, log_file=""):
    _write_log_line(f"[ERROR] {msg}", log_file)


def log_summary(msg, log_file=""):
    _write_log_line(f"[SUMMARY] {msg}", log_file)


def timed_run(label, fn, log_file="", *args, **kwargs):
    """运行一个函数并测量其执行时间，同时记录日志。"""
    t0 = time.perf_counter()
    log_info(f"{label}: start", log_file)
    result = fn(*args, **kwargs)
    dt = time.perf_counter() - t0
    log_info(f"{label}: done in {dt:.3f}s", log_file)
    return result, dt

# 下面是一些辅助函数，用于输出板级信息、统计数据、调试BGA封装等。

def dump_board(board, limit=50, log_file=""):
    """输出板级的详细信息，包括网络、焊盘、过孔和线段。为了避免控制台输出过多，限制每类对象的显示数量，但日志文件会记录全部信息。"""
    def dual_log(title, items): # 既输出到控制台，也写入日志文件
        log(f"\n====== {title} ======", log_file)

        total = len(items)

        f = None
        if log_file:
            f = open(log_file, "a", encoding="utf-8")

        try:
            for i, x in enumerate(items):
                # 控制台
                if i <= limit:
                    print(x)

                # log 文件（全部）
                if f:
                    f.write(str(x) + "\n")

            if total > limit:
                print(f"... (showing {limit}/{total})")

            log(f"[DUMP] {title}: total={total}, console={min(limit, total)}", log_file)

        finally:
            if f:
                f.close()

    dual_log("NETS", board.nets)
    dual_log("PADS", board.pads)
    dual_log("VIAS", board.vias)
    dual_log("SEGMENTS", board.segments)


def board_stats(board, log_file=""):
    log(f"[PARSE] layers_table ids={sorted(board.layers_table.get('id_to_name', {}).keys())}", log_file)
    log(f"[PARSE] layers_table names={sorted(board.layers_table.get('name_to_id', {}).keys())}", log_file)
    log(f"[PARSE] nets={len(board.nets)}", log_file)
    log(f"[PARSE] pads={len(board.pads)}", log_file)
    log(f"[PARSE] vias={len(board.vias)}", log_file)
    log(f"[PARSE] segments={len(board.segments)}", log_file)

    # 1) segment 按层计数
    seg_layer_count = Counter()
    seg_layer_names = set()
    for s in board.segments:
        seg_layer_count[s.layer] += 1
        if s.layer:
            seg_layer_names.add(s.layer)

    # 2) pad 层名集合
    pad_layer_count = Counter()
    pad_layer_names = set()
    for p in board.pads:
        if p.layer:
            pad_layer_count[p.layer] += 1
            pad_layer_names.add(p.layer)

    # 3) via 起止层集合
    via_layer_pairs = Counter()
    via_layer_names = set()
    for v in board.vias:
        start_layer = getattr(v, "start_layer", None)
        end_layer = getattr(v, "end_layer", None)

        if start_layer or end_layer:
            via_layer_pairs[(start_layer, end_layer)] += 1
        if start_layer:
            via_layer_names.add(start_layer)
        if end_layer:
            via_layer_names.add(end_layer)

    # 4) 所有解析到的层名总集合
    all_layer_names = sorted(seg_layer_names | pad_layer_names | via_layer_names)

    log(f"[PARSE] unique layer names total={len(all_layer_names)}", log_file)
    log(f"[PARSE] all layer names={all_layer_names}", log_file)

    log("\nSEGMENT LAYERS", log_file)
    log(f"count={len(seg_layer_names)} names={sorted(seg_layer_names)}", log_file)
    for k, v in sorted(seg_layer_count.items()):
        log(f"{k}: {v}", log_file)

    log("\nPAD LAYERS", log_file)
    log(f"count={len(pad_layer_names)} names={sorted(pad_layer_names)}", log_file)
    for k, v in sorted(pad_layer_count.items()):
        log(f"{k}: {v}", log_file)

    log("\nVIA LAYER PAIRS", log_file)
    if via_layer_pairs:
        for (sl, el), cnt in sorted(via_layer_pairs.items(), key=lambda x: (str(x[0][0]), str(x[0][1]))):
            log(f"{sl} -> {el}: {cnt}", log_file)
    else:
        log("(none)", log_file)

    log("\nVIA LAYER NAMES", log_file)
    log(f"count={len(via_layer_names)} names={sorted(via_layer_names)}", log_file)

    # 5) 直接输出涉及的层ID集合，看看是否有异常值
    seg_layer_ids = sorted({s.layer_id for s in board.segments})
    pad_layer_ids = sorted({p.layer_id for p in board.pads})
    via_layer_id_pairs = sorted({(v.start_layer_id, v.end_layer_id) for v in board.vias})

    log(f"[PARSE] segment layer ids={seg_layer_ids}", log_file)
    log(f"[PARSE] pad layer ids={pad_layer_ids}", log_file)
    log(f"[PARSE] via layer id pairs={via_layer_id_pairs}", log_file)


def debug_bga(board, limit=50, log_file=""):
    bga_pads = [p for p in board.pads if p.is_bga]
    total = len(bga_pads)

    log(f"\n[BGA] pads={total}", log_file)

    f = None
    if log_file:
        f = open(log_file, "a", encoding="utf-8")

    try:
        for i, p in enumerate(bga_pads):
            line = (
                f"{p.id} net={p.net} "
                f"xy=({p.x:.6f},{p.y:.6f}) "
                f"row={p.bga_row} col={p.bga_col} "
                f"layer={p.layer} layer_id={p.layer_id} "
            )

            # 控制台
            if i <= limit:
                print(line)

            # log 全量
            if f:
                f.write(line + "\n")

        if total > limit:
            print(f"... (showing {limit}/{total})")
            if f:
                f.write(f"... (showing {limit}/{total})\n")
    finally:
        if f:
            f.close()


def dump_modules_from_board(board, limit=200, log_file=""):
    """
    直接从 board.modules 输出模块/封装信息。
    要求 parser 已经把 modules 挂到 board.modules 上。
    """
    modules = getattr(board, "modules", None)

    if modules is None:
        log("[MODULES] board.modules is missing; parser has not attached module data.", log_file)
        return

    total = len(modules)
    log("\n====== MODULES / FOOTPRINTS ======", log_file)
    log(f"[MODULES] total={total}", log_file)

    f = None
    if log_file:
        f = open(log_file, "a", encoding="utf-8")

    try:
        for i, m in enumerate(modules):
            line = (
                f"{m['component']} | package={m['package']} | "
                f"layer={m.get('layer', '(unknown)')} | "
                f"layer_id={m.get('layer_id', -1)} | "
                f"at=({m['x']:.6f}, {m['y']:.6f}) angle={m['angle']:.2f}"
            )

            # 控制台只显示前 limit 条
            if i < limit:
                print(line)

            # log 文件记录全部
            if f:
                f.write(line + "\n")

        if total > limit:
            print(f"... (showing {limit}/{total})")

    finally:
        if f:
            f.close()

def dump_pad_count_by_component(board, topn=50, log_file=""):
    """统计并输出每个组件的焊盘数量，特别关注BGA封装的焊盘数量和比例。输出前topn个组件，按焊盘总数排序。"""
    from collections import defaultdict

    groups = defaultdict(list)

    for p in board.pads:
        comp = p.component or "UNKNOWN"
        groups[comp].append(p)

    items = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

    log("\n====== PAD COUNT BY COMPONENT ======", log_file)

    for comp, pads in items[:topn]:
        total = len(pads)
        bga_count = sum(1 for p in pads if p.is_bga)

        ratio = bga_count / total if total > 0 else 0

        log(
            f"{comp}: total={total}, bga={bga_count}, ratio={ratio:.2%}",
            log_file
        )


def print_issues(issues, title="CHECK ISSUES", log_file=""):
    log(f"\n====== {title} ======", log_file)
    log(f"[CHECK] issues={len(issues)}", log_file)

    for i, issue in enumerate(issues, start=1):
        line = (
            f"{i:03d}. {issue.rule} | {issue.severity} | {issue.message} " 
            f"[obj1={issue.obj1} obj2={issue.obj2} "
            f"[net={issue.net} layer={issue.layer} xy=({issue.x},{issue.y})]"
        )

        # 控制台
        if i <= 50:  # 假设只显示前50个问题
            print(line)

        # log 文件
        if log_file:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def summarize_issues_by_rule(issues):
    counter = Counter()
    for it in issues:
        counter[it.rule] += 1
    return counter

def print_rule_summary(title, issues, log_file=""):
    #log_info("", log_file)
    log_summary(f"{title}", log_file)

    counter = summarize_issues_by_rule(issues)
    if not counter:
        log_info("  (none)", log_file)
        return

    for rule, count in sorted(counter.items()):
        log_info(f"  {rule}: {count}", log_file)

def build_board_result(result):
    timing = result.get("timing", {})
    hard_issues = result.get("hard_issues", [])
    opt_issues = result.get("opt_issues", [])
    diff_issues = result.get("diff_issues", [])
    all_issues = result.get("all_issues", [])
    precheck_summary = result.get("precheck_summary", {})
    


    return {
        "board_name": result.get("board_name", ""),
        "pcb_path": result.get("pcb_path", ""),
        "check_mode": result.get("check_mode", ""),
        "target_bga": precheck_summary.get("target_bga", ""),
        "precheck_summary": precheck_summary,
        "checked_diff_pair_count": precheck_summary.get("checked_diff_pair_count", 0),
        "checked_diff_pairs": precheck_summary.get("checked_diff_pairs", []),
        
        "routing_metrics": result.get("routing_metrics", {}),
        "counts": {
            "hard_issues": len(hard_issues),
            "opt_issues": len(opt_issues),
            "diff_issues": len(diff_issues),
            "total_issues": len(all_issues),
        },
        "timing": {
            "parse_time": timing.get("parse_time", 0.0),
            "precheck_time": timing.get("precheck_time", 0.0),
            "hard_time": timing.get("hard_time", 0.0),
            "opt_time": timing.get("opt_time", 0.0),
            "diff_time": timing.get("diff_time", 0.0),
            "modules_time": timing.get("modules_time", 0.0),
            "llm_time": timing.get("llm_time", 0.0),
            "total_check_time": timing.get("total_check_time", 0.0),
            "engine_elapsed_time": timing.get("engine_elapsed_time", 0.0),
            "whole_program_time": timing.get("whole_program_time", 0.0),
        },
        "rule_timings": {
            "hard": result.get("hard_rule_timings", []),
            "opt": result.get("opt_rule_timings", []),
            "diff": result.get("diff_rule_timings", []),
        },
    }

def build_issue_report(result):
    check_mode = (result.get("check_mode", "") or "hard").strip().lower()

    if check_mode == "all":
        selected_modes = {"hard", "opt", "diff"}
    else:
        selected_modes = {x.strip() for x in check_mode.split(",") if x.strip()}

    selected_issues = []
    if "hard" in selected_modes:
        selected_issues.extend(result.get("hard_issues", []))
    if "opt" in selected_modes:
        selected_issues.extend(result.get("opt_issues", []))
    if "diff" in selected_modes:
        selected_issues.extend(result.get("diff_issues", []))

    return {
        "board_name": result.get("board_name", ""),
        "pcb_path": result.get("pcb_path", ""),
        "check_mode": check_mode,
        "issues": [x.to_dict() for x in selected_issues],
    }


def build_ui_payload(result):
    precheck = result.get("precheck_summary", {}) or {}
    hard_issues = result.get("hard_issues", [])
    opt_issues = result.get("opt_issues", [])
    diff_issues = result.get("diff_issues", [])
    hard_error_count = sum(
        1 for issue in hard_issues if (issue.severity or "").upper() == "ERROR"
    )
    hard_warning_count = sum(
        1 for issue in hard_issues if (issue.severity or "").upper() == "WARNING"
    )

    return {
        "board_name": result.get("board_name", ""),
        "pcb_path": result.get("pcb_path", ""),
        "check_mode": result.get("check_mode", ""),
        "target_bga": precheck.get("target_bga", ""),
        "status": "failed" if hard_error_count else "passed",
        "counts": {
            "hard": hard_error_count,
            "warnings": hard_warning_count,
            "opt": len(opt_issues),
            "diff": len(diff_issues),
            "total": len(result.get("all_issues", [])),
        },
        "routing_metrics": result.get("routing_metrics", {}),
    }


def parse_check_modes(mode_text: str):
    text = (mode_text or "hard").strip().lower()
    if text == "all":
        return {"hard", "opt", "diff"}

    selected = {x.strip() for x in text.split(",") if x.strip()}
    valid = {"hard", "opt", "diff"}
    unknown = selected - valid
    if unknown:
        raise ValueError(f"Unknown check_mode(s): {sorted(unknown)}")
    return selected

def evaluate_board(
    pcb_path: str,
    check_mode: str = "hard",
    log_file: str = "",
    debug_log: bool = False,
    target_bga: str = "",
    diff_pairs_path: str = "",
    routing_rules_path: str = "",
    diff_groups_path: str = "",
    net_roles_path: str = "",
):
    program_t0 = time.perf_counter()

    # ========== local logger ==========
    def local_logger(*log_args):
        if len(log_args) == 1:
            level = "info"
            text = str(log_args[0])
        elif len(log_args) == 2:
            level = str(log_args[0]).strip().lower()
            text = str(log_args[1])
        else:
            raise TypeError("logger() expects 1 or 2 arguments")

        if level == "debug":
            if debug_log:
                _write_log_line(f"[DEBUG] {text}", log_file)
        elif level == "info":
            log_info(text, log_file)
        elif level == "warn":
            log_warn(text, log_file)
        elif level == "error":
            log_error(text, log_file)
        elif level == "summary":
            log_summary(text, log_file)
        else:
            log_info(text, log_file)

    # ========== parse ==========
    board, parse_time = timed_run("parse_kicad", parse_kicad, log_file, pcb_path)
    constraints = load_external_constraints(
        diff_pairs_path=diff_pairs_path,
        routing_rules_path=routing_rules_path,
        diff_groups_path=diff_groups_path,
    )
    attach_constraints_to_board(board, constraints)

    constraint_errors = validate_constraints_against_board(board)

    if constraint_errors:
        for err in constraint_errors:
            local_logger("warn", f"[CONSTRAINTS] {err}")
    else:
        local_logger("info", "[CONSTRAINTS] validation passed")

    if constraints:
        local_logger(
            "info",
            f"[CONSTRAINTS] diff_pairs={len(board.diff_pairs)} "
            f"single_rules={len(board.single_rules)} "
            f"diff_rules={len(board.diff_rules)} "
            f"diff_groups={len(board.diff_groups)}"
        )
    precheck_summary, precheck_time = timed_run(
        "run_precheck",
        run_precheck,
        log_file,
        board,
        target_bga,
        net_roles_path,
        )


    modules_time = 0.0
    llm_time = 0.0
   # precheck_time = 0.0

    issues = []
    hard_issues = []
    opt_issues = []
    diff_issues = []

    hard_time = 0.0
    opt_time = 0.0
    diff_time = 0.0

    hard_rule_timings = []
    opt_rule_timings = []
    diff_rule_timings = []


    selected_modes = parse_check_modes(check_mode)

    routing_metrics = {}

    # ========== checks ==========

    if "hard" in selected_modes:
        t0 = time.perf_counter()
        log("[STAGE] run_hard_checks: start", log_file)
        hard_issues, hard_rule_timings = run_hard_checks(board, log_fn=local_logger, with_timing=True)

        routing_metrics = compute_escape_completion_rate(board, hard_issues)

        hard_time = time.perf_counter() - t0
        log(f"[STAGE] run_hard_checks: done in {hard_time:.3f}s", log_file)


    if "opt" in selected_modes:
        t1 = time.perf_counter()
        log("[STAGE] run_optimization_checks: start", log_file)
        opt_issues, opt_rule_timings = run_optimization_checks(board, log_fn=local_logger, with_timing=True)
        opt_time = time.perf_counter() - t1
        log(f"[STAGE] run_optimization_checks: done in {opt_time:.3f}s", log_file)

    if "diff" in selected_modes:
        t2 = time.perf_counter()
        log("[STAGE] run_differential_checks: start", log_file)
        diff_issues, diff_rule_timings = run_differential_checks(board, log_fn=local_logger, with_timing=True)
        diff_time = time.perf_counter() - t2
        log(f"[STAGE] run_differential_checks: done in {diff_time:.3f}s", log_file)

    issues = []
    if "hard" in selected_modes:
        issues.extend(hard_issues)
    if "opt" in selected_modes:
        issues.extend(opt_issues)
    if "diff" in selected_modes:
        issues.extend(diff_issues)


    total_check_time = hard_time + opt_time + diff_time
    engine_elapsed_time = time.perf_counter() - program_t0

    result = {
        "board_name": os.path.basename(pcb_path),
        "pcb_path": pcb_path,
        "check_mode": check_mode,
        "precheck_summary": precheck_summary,
        "routing_metrics": routing_metrics,
        "hard_issues": hard_issues,
        "opt_issues": opt_issues,
        "diff_issues": diff_issues,
        "all_issues": issues,
        "hard_rule_timings": [
            {"rule": label, "time": dt, "issues": n}
            for label, dt, n in hard_rule_timings
        ],
        "opt_rule_timings": [
            {"rule": label, "time": dt, "issues": n}
            for label, dt, n in opt_rule_timings
        ],
        "diff_rule_timings": [
            {"rule": label, "time": dt, "issues": n}
            for label, dt, n in diff_rule_timings
        ],
        "timing": {
            "parse_time": parse_time,
            "hard_time": hard_time,
            "opt_time": opt_time,
            "diff_time": diff_time,
            "modules_time": modules_time,
            "llm_time": llm_time,
            "total_check_time": total_check_time,
            "engine_elapsed_time": engine_elapsed_time,
            "precheck_time": precheck_time,
        },
        "board": board,
    }
    return result


def result_to_summary_row(result):
    hard_score = result.get("hard_score") or {}
    timing = result.get("timing") or {}

    hard_rule_counts = hard_score.get("hard_rule_counts") or {}

    hard_issues = result.get("hard_issues", [])
    hard_error_count = sum(
        1 for issue in hard_issues
        if (issue.severity or "").upper() == "ERROR"
    )
    hard_warning_count = sum(
        1 for issue in hard_issues
        if (issue.severity or "").upper() == "WARNING"
    )

    row = {
        "board_name": result.get("board_name", ""),
        "pcb_path": result.get("pcb_path", ""),
        #"hard_pass": hard_score.get("hard_pass", False),
        "hard_issue_count": hard_error_count,
        "hard_warning_count": hard_warning_count,
        #"hard_issue_count": hard_score.get("hard_issue_count", 0),
        #"hard_penalty": hard_score.get("hard_penalty", 0.0),
        #"hard_score": hard_score.get("hard_score", 0.0),

        "HR_CONNECT_PAD_NOT_ESCAPED": hard_rule_counts.get("HR_CONNECT_PAD_NOT_ESCAPED", 0),
        "HR_TOPO_MULTIPLE_ESCAPE": hard_rule_counts.get("HR_TOPO_MULTIPLE_ESCAPE", 0),
        "HR_DRC_SEGMENT_CROSSING": hard_rule_counts.get("HR_DRC_SEGMENT_CROSSING", 0),
        "HR_CONNECT_BRANCH_INCOMPLETE": hard_rule_counts.get("HR_CONNECT_BRANCH_INCOMPLETE", 0),
        
        "diff_issue_count": len(result.get("diff_issues", [])),
        "diff_time": timing.get("diff_time", 0.0),

        "parse_time": timing.get("parse_time", 0.0),
        "hard_time": timing.get("hard_time", 0.0),
        "total_check_time": timing.get("total_check_time", 0.0),
        "whole_program_time": timing.get("whole_program_time", 0.0),
        "precheck_time": timing.get("precheck_time", 0.0),
    }
    return row


def main():
    program_t0 = time.perf_counter()

    parser = argparse.ArgumentParser()

    parser.add_argument("pcb")
    parser.add_argument("--summary", action="store_true") # 输出解析统计信息，如网络、焊盘、过孔和线段的数量，以及涉及的层数和层名等。
    parser.add_argument("--dump", action="store_true") # 输出解析到的板级对象的详细信息，包括网络、焊盘、过孔和线段。为了避免控制台输出过多，限制每类对象的显示数量，但日志文件会记录全部信息。
    parser.add_argument("--debug-bga", action="store_true") # 专门输出BGA封装的焊盘信息，包括它们的行列号和坐标等，帮助调试BGA相关的问题。
    parser.add_argument("--check", action="store_true") # 运行规则检查，找出设计中的潜在问题。可以选择运行不同级别的规则集（硬性规则、优化建议或全部）。检查结果会以列表形式输出，并可选地保存为JSON文件。
    parser.add_argument("--json-out", type=str, default="") # 可选地将检查结果保存为JSON文件，包含问题列表和相关统计数据，方便后续分析或与其他工具集成。
    parser.add_argument("--zh-json-out", type=str, default="", help="Optional Chinese JSON output path. Existing --json-out stays unchanged.")
    parser.add_argument("--agent-json-out", type=str, default="", help="Optional agent-ready Chinese JSON output path.")
    parser.add_argument("--llm-analyze", action="store_true") # 使用大语言模型（LLM）对检查结果进行分析，生成更易理解的报告或建议。可以指定LLM模型和输出文件路径，分析结果会同时输出到控制台和保存为文本文件。
    parser.add_argument("--llm-model", type=str, default="") # 指定用于分析的LLM模型名称或路径，默认为空字符串，表示使用默认模型。用户可以根据需要选择不同的LLM来获得更适合其设计风格和需求的分析结果。
    parser.add_argument("--llm-out", type=str, default="llm_analysis.txt") # 指定LLM分析结果的输出文件路径，默认为"llm_analysis.txt"。分析结果会同时输出到控制台和保存为这个文本文件，方便用户查看和存档。
    parser.add_argument("--llm-prompt-out", type=str, default="llm_prompt.txt") # 可选地将用于LLM分析的提示（prompt）保存为文本文件，默认为"llm_prompt.txt"。这对于调试和优化提示非常有用，用户可以查看生成的提示内容，并根据需要进行调整以获得更好的分析结果。
    parser.add_argument("--dump-modules", action="store_true") # 输出解析到的模块/组件信息，包括它们的名称、封装类型、位置和角度等。为了避免控制台输出过多，限制显示数量，但日志文件会记录全部信息。这有助于检查模块解析是否正确，特别是对于复杂封装如BGA。
    parser.add_argument("--pad-count-by-comp", action="store_true") # 统计并输出每个组件的焊盘数量，特别关注BGA封装的焊盘数量和比例。输出前topn个组件，按焊盘总数排序。这有助于快速识别哪些组件可能存在BGA相关的问题。
    parser.add_argument("--check-mode",type=str,default="hard",help="Comma-separated rule sets to run, e.g. hard, opt, diff, all. Default is hard.",)
    parser.add_argument("--log-file",type=str,default="",help="Optional log file path.",)
    parser.add_argument("--debug-log", action="store_true", help="show verbose debug logs from rules/runner")
    parser.add_argument("--target-bga", type=str, default="", help="Optional target BGA component, e.g. U67")
    parser.add_argument("--diff-pairs", type=str, default="", help="Path to diff_pairs.json")
    parser.add_argument("--routing-rules", type=str, default="", help="Path to routing_rules.json")
    parser.add_argument("--diff-groups", type=str, default="", help="Path to diff_groups.json")
    parser.add_argument("--net-roles", type=str, default="", help="Path to net_roles.json for external signal/power/ground net classification.")

   
    args = parser.parse_args()

    # 如果指定了日志文件，先清空它，以便记录新的日志内容。
    if args.log_file:
        open(args.log_file, "w", encoding="utf-8").close()

    result = evaluate_board(
        pcb_path=args.pcb,
        check_mode=args.check_mode,
        log_file=args.log_file,
        debug_log=args.debug_log,
        target_bga=args.target_bga,
        diff_pairs_path=args.diff_pairs,
        routing_rules_path=args.routing_rules,
        diff_groups_path=args.diff_groups,
        net_roles_path=args.net_roles,
    )

    board = result["board"]

    hard_issues = result.get("hard_issues", [])
    opt_issues = result.get("opt_issues", [])
    diff_issues = result.get("diff_issues" , [])
    issues = result.get("all_issues", [])
    precheck_summary = result.get("precheck_summary", {})

    timing = result.get("timing", {})
    parse_time = timing.get("parse_time", 0.0)
    hard_time = timing.get("hard_time", 0.0)
    opt_time = timing.get("opt_time", 0.0)
    diff_time = timing.get("diff_time", 0.0)
    modules_time = timing.get("modules_time", 0.0)
    llm_time = timing.get("llm_time", 0.0)
    precheck_time = timing.get("precheck_time", 0.0)
    engine_elapsed_time = timing.get("engine_elapsed_time", 0.0)

    hard_rule_timings = result.get("hard_rule_timings", [])
    opt_rule_timings = result.get("opt_rule_timings", [])
    diff_rule_timings = result.get("diff_rule_timings", [])

    selected_modes = parse_check_modes(args.check_mode)

    # 输出输入参数和选项，帮助调试和记录运行配置。
    log(f"[INPUT] pcb={args.pcb}", args.log_file)
    log(f"[MODE] check_mode={args.check_mode}", args.log_file)
    log(f"[MODE] log_file={args.log_file or '(none)'}", args.log_file) # 输出日志文件路径，如果没有指定则显示"(none)"，帮助确认日志记录的目标位置。
   
    if precheck_summary:
        log("[PRECHECK SUMMARY]", args.log_file)
        log(f"target_bga={precheck_summary.get('target_bga', '')}", args.log_file)
        log(f"target_bga_pad_count={precheck_summary.get('target_bga_pad_count', 0)}", args.log_file)
        log(f"all_net_count={precheck_summary.get('all_net_count', 0)}", args.log_file)
        log(f"signal_net_count={precheck_summary.get('signal_net_count', 0)}", args.log_file)
        log(f"filtered_out_net_count={precheck_summary.get('filtered_out_net_count', 0)}", args.log_file)
        log(f"candidate_diff_pair_count={precheck_summary.get('candidate_diff_pair_count', 0)}", args.log_file)

    if args.check or args.llm_analyze:
        if "hard" in selected_modes and hard_rule_timings:
            log("[HARD RULE TIMING]", args.log_file)
            for item in hard_rule_timings:
                log(f"{item['rule']}:{item['time']:.3f}s ({item['issues']} issues)",args.log_file)

        #    for label, dt, n in hard_rule_timings:
         #       log(f"{label}: {dt:.3f}s ({n} issues)", args.log_file)

        if "opt" in selected_modes and opt_rule_timings:
            log("[OPT RULE TIMING]", args.log_file)
            for item in opt_rule_timings:
                log(f"{item['rule']}:{item['time']:.3f}s ({item['issues']} issues)",args.log_file)           
          #  for label, dt, n in opt_rule_timings:
           #     log(f"{label}: {dt:.3f}s ({n} issues)", args.log_file)

        if "diff" in selected_modes and diff_rule_timings:
            log("[DIFF RULE TIMING]", args.log_file)
            for item in diff_rule_timings:
                log(f"{item['rule']}:{item['time']:.3f}s ({item['issues']} issues)", args.log_file)

    if args.check:
        log(f"[CHECK MODE] {args.check_mode}", args.log_file)


        if "hard" in selected_modes:
            print_rule_summary("HARD ISSUE SUMMARY BY RULE", hard_issues, args.log_file)
            print_issues(hard_issues, "HARD ISSUES", log_file=args.log_file)

        if "opt" in selected_modes:
            print_rule_summary("OPT ISSUE SUMMARY BY RULE", opt_issues, args.log_file)
            print_issues(opt_issues, "OPTIMIZATION ISSUES", log_file=args.log_file)

        if "diff" in selected_modes:
            print_rule_summary("DIFF ISSUE SUMMARY BY RULE", diff_issues, args.log_file)
            print_issues(diff_issues, "DIFFERENTIAL ISSUES", log_file=args.log_file)



    if args.llm_analyze:
        (prompt, llm_result), llm_time = timed_run(
            "analyze_issues_with_llm",
            analyze_issues_with_llm,
            args.log_file,
            board=board,
            issues=issues,
            model=args.llm_model,
            save_txt_path=args.llm_out,
            save_prompt_path=args.llm_prompt_out,
        )

        log(f"[LLM] analysis written to: {args.llm_out}", args.log_file)
        log(f"[LLM] prompt written to: {args.llm_prompt_out}", args.log_file)
        print("\n====== LLM ANALYSIS ======\n")
        print(llm_result)

    total_check_time = hard_time + opt_time + diff_time
    whole_program_time = time.perf_counter() - program_t0

    result.setdefault("timing", {})["whole_program_time"] = whole_program_time

    log(
        f"[TIME SUMMARY] parse={parse_time:.3f}s "
        f"precheck={precheck_time:.3f}s "
        f"modules={modules_time:.3f}s "
        f"hard={hard_time:.3f}s "
        f"opt={opt_time:.3f}s "
        f"diff={diff_time:.3f}s "
        f"llm={llm_time:.3f}s "
        f"total_check={total_check_time:.3f}s "
        f"engine_elapsed={engine_elapsed_time:.3f}s",
        args.log_file,
    )
    log(f"[TIME SUMMARY] whole_program={whole_program_time:.3f}s", args.log_file)

    if args.json_out:
        board_result = build_board_result(result)

        issues_report = build_issue_report(result)

        payload = {
            "board_result": board_result,
            #"precheck_summary": precheck_summary,
            "issue_report": issues_report,
                
        }

        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        log(f"[WRITE] issues json -> {args.json_out}", args.log_file)

    if args.zh_json_out:
        out_dir = os.path.dirname(args.zh_json_out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.zh_json_out, "w", encoding="utf-8") as f:
            json.dump(build_chinese_payload(result), f, indent=2, ensure_ascii=False)
        log(f"[WRITE] chinese json -> {args.zh_json_out}", args.log_file)

    if args.agent_json_out:
        out_dir = os.path.dirname(args.agent_json_out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.agent_json_out, "w", encoding="utf-8") as f:
            json.dump(build_agent_ready_payload(result), f, indent=2, ensure_ascii=False)
        log(f"[WRITE] agent json -> {args.agent_json_out}", args.log_file)


if __name__ == "__main__":
    main()
