from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path
from typing import Any

from pcb_agent_langgraph.agent import PCBLangGraphAgent
from pcb_agent_langgraph.evaluation.simulated_tools import SimulatedExternalTool, SimulatedFrontend
from pcb_agent_langgraph.evaluation.trace import EvaluationTrace
from pcb_agent_langgraph.utils.config import load_config


# ====== 功能：执行 live-eval 数据集并生成评测报告。 ======
class LiveEvaluationRunner:
    # live-eval 支持三种模式：全真实、模拟全部工具、只模拟前端但调用真实 router/DRC。
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, config_path: str | None = None, *, output_dir: str | Path = "eval_runs", simulate_tools: bool = False, simulate_frontend_only: bool = False, require_model_planner: bool = False) -> None:
        self.config = load_config(config_path)
        self.output_dir = Path(output_dir)
        self.simulate_tools = simulate_tools
        self.simulate_frontend_only = simulate_frontend_only
        self.require_model_planner = require_model_planner
        self.agent = PCBLangGraphAgent(self.config, frontend_sender=None, use_model_planner=True, require_model_planner=require_model_planner)

    # ====== 功能：执行整个 live-eval 数据集。 ======
    async def run_dataset(self, dataset_path: str | Path) -> dict[str, Any]:
        samples = json.loads(Path(dataset_path).read_text(encoding="utf-8-sig"))
        if not isinstance(samples, list):
            raise ValueError("Evaluation dataset must be a JSON array.")
        rows = []
        for index, sample in enumerate(samples):
            rows.append(await self.run_sample(sample, index))
        report = self._build_report(rows)
        self._write_report(rows, report)
        return report

    # ====== 功能：执行单条评测样本的多轮对话。 ======
    async def run_sample(self, sample: dict[str, Any], index: int) -> dict[str, Any]:
        sample_id = str(sample.get("id") or f"sample-{index}")
        trace = EvaluationTrace(sample_id)
        start = time.perf_counter()
        state = None
        error = ""
        turn_states: list[dict[str, Any]] = []
        agent = self._agent_for_sample(sample, sample_id)
        project_id = str(sample.get("projectid") or sample.get("project_id") or "")
        turns = sample.get("turns")
        if not isinstance(turns, list) or not turns:
            turns = [sample.get("prompt", "")]
        try:
            for turn_index, turn in enumerate(turns):
                prompt = str(turn.get("prompt") if isinstance(turn, dict) else turn)
                state = await agent.ainvoke(sample_id, project_id, prompt)
                snapshot = dict(state)
                turn_states.append(snapshot)
                trace.add("turn_state", {"turn_index": turn_index, "prompt": prompt, "state": snapshot})
            if state is not None:
                trace.add("final_state", dict(state))
        except Exception as exc:
            error = str(exc)
            trace.add("error", {"message": error})
        elapsed_ms = (time.perf_counter() - start) * 1000
        trace.save(self.output_dir / "traces" / f"{sample_id}.json")

        tool_history = _merged_tool_history(turn_states)
        planner_outputs = _planner_outputs(turn_states)
        model_plan_count = sum(1 for item in planner_outputs if item.get("planner_source") == "model")
        model_elapsed_ms = sum(float(item.get("model_elapsed_ms") or 0.0) for item in planner_outputs)
        tool_failures = _tool_failures_for_sample(sample, state or {}, tool_history)
        expected_order = sample.get("expected_tool_order") or []
        tool_order_list = [str(item.get("call", {}).get("name", "")) for item in tool_history]
        expected_order_ok = _contains_subsequence(tool_order_list, expected_order) if expected_order else True
        final_response = str(state.get("final_response", "")) if state else ""
        expected_final_contains = str(sample.get("expected_final_contains") or "")
        final_text_ok = expected_final_contains in final_response if expected_final_contains else True
        assertions = _evaluate_assertions(sample, state or {}, tool_history)
        assertions_ok = all(item.get("ok") for item in assertions)
        return {
            "sample_id": sample_id,
            "expected_task": sample.get("task_type", ""),
            "detected_task": state.get("task_type", "") if state else "",
            "success": bool(state and not error and state.get("current_stage") == "finished" and not tool_failures and expected_order_ok and final_text_ok and assertions_ok and (not self.require_model_planner or model_plan_count > 0)),
            "tool_failure_count": len(tool_failures),
            "expected_order_ok": expected_order_ok,
            "final_text_ok": final_text_ok,
            "assertions_ok": assertions_ok,
            "assertion_failures": "; ".join(item.get("message", "") for item in assertions if not item.get("ok")),
            "error": error,
            "turn_count": len(turns),
            "tool_call_count": len(tool_history),
            "tool_order": " > ".join(tool_order_list),
            "model_plan_count": model_plan_count,
            "model_elapsed_ms": round(model_elapsed_ms, 2),
            "elapsed_ms": round(elapsed_ms, 2),
            "loop_count": state.get("loop_count", 0) if state else 0,
            "workflow_state": state.get("workflow_state", "") if state else "",
            "drc_errors": len(((state or {}).get("intermediate_cache", {}).get("drcResult") or {}).get("errors", [])) if state else 0,
            "final_response": final_response,
        }

    # ====== 功能：根据评测模式创建真实或模拟工具 Agent。 ======
    def _agent_for_sample(self, sample: dict[str, Any], sample_id: str) -> PCBLangGraphAgent:
        if not self.simulate_tools and not self.simulate_frontend_only:
            return self.agent
        simulator = SimulatedFrontend(sample, self.output_dir / "simulated_files")
        agent = PCBLangGraphAgent(self.config, frontend_sender=simulator.send_tool_call, use_model_planner=True, require_model_planner=self.require_model_planner)
        if self.simulate_frontend_only:
            # 只替换 EDA 前端交互，外部 router/DRC/explain 仍走 config.ini 的真实入口。
            return agent
        for name in ("layer_assign", "escape_order", "fanout_route", "reroute", "help_planner", "drc_check", "explainability_report"):
            agent.tools[name] = SimulatedExternalTool(name, self.output_dir / "simulated_files", sample)
        return agent


    # ====== 功能：根据输入结果生成摘要报告。 ======
    def _build_report(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(rows)
        success = sum(1 for row in rows if row["success"])
        return {
            "total": total,
            "success": success,
            "success_rate": success / total if total else 0.0,
            "avg_elapsed_ms": sum(row["elapsed_ms"] for row in rows) / total if total else 0.0,
            "avg_tool_calls": sum(row["tool_call_count"] for row in rows) / total if total else 0.0,
            "failures": [row for row in rows if not row["success"]],
        }

    # ====== 功能：写出评测 JSON 和 CSV 报告。 ======
    def _write_report(self, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if rows:
            with (self.output_dir / "report.csv").open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)


# ====== 功能：合并多轮状态中的工具调用历史。 ======
def _merged_tool_history(turn_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for state in turn_states:
        for item in state.get("tool_history", []) or []:
            call = item.get("call", {}) if isinstance(item, dict) else {}
            key = str(call.get("id") or f"{call.get('name')}:{len(seen)}")
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged

# ====== 功能：收集多轮状态中的 planner 输出，用于验证真实模型是否参与规划。 ======
def _planner_outputs(turn_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for state in turn_states:
        planner_output = state.get("planner_output", {})
        if isinstance(planner_output, dict):
            outputs.append(planner_output)
        for trace_item in state.get("trace", []) or []:
            payload = trace_item.get("payload", {}) if isinstance(trace_item, dict) else {}
            traced_output = payload.get("planner_output") if isinstance(payload, dict) else None
            if isinstance(traced_output, dict):
                outputs.append(traced_output)
    return outputs


# ====== 功能：收集真正应该导致样本失败的工具错误，允许 DRC-loop 的预期失败继续评估最终结果。 ======
def _tool_failures_for_sample(sample: dict[str, Any], state: dict[str, Any], tool_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures = [item for item in tool_history if (not item.get("ok")) or (isinstance(item.get("result"), dict) and str(item.get("result", {}).get("status", "")).lower() in {"failed", "error"})]
    if not int(sample.get("simulate_drc_failures", 0) or 0):
        return failures
    drc_passed = bool(_path_get(state, "report_payload.drcPassed"))
    if not drc_passed:
        return failures
    return [item for item in failures if item.get("call", {}).get("name") != "drc_check"]
# ====== 功能：执行数据集声明的扩展断言，覆盖参数传递、报告字段和工具次数。 ======
def _evaluate_assertions(sample: dict[str, Any], state: dict[str, Any], tool_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for item in sample.get("expected_tool_counts") or []:
        name = str(item.get("name") or "")
        expected = int(item.get("count") or 0)
        actual = sum(1 for record in tool_history if record.get("call", {}).get("name") == name)
        assertions.append({"ok": actual == expected, "message": f"tool_count {name}: expected {expected}, got {actual}"})
    for item in sample.get("expected_min_tool_counts") or []:
        name = str(item.get("name") or "")
        expected = int(item.get("count") or 0)
        actual = sum(1 for record in tool_history if record.get("call", {}).get("name") == name)
        assertions.append({"ok": actual >= expected, "message": f"tool_min_count {name}: expected >= {expected}, got {actual}"})
    for item in sample.get("expected_tool_args") or []:
        name = str(item.get("name") or "")
        path = str(item.get("path") or "")
        expected = item.get("equals")
        records = [record for record in tool_history if record.get("call", {}).get("name") == name]
        actual_values = [_path_get(record.get("call", {}).get("arguments") or {}, path) for record in records]
        assertions.append({"ok": any(value == expected for value in actual_values), "message": f"tool_arg {name}.{path}: expected {expected!r}, got {actual_values!r}"})
    for item in sample.get("expected_state_values") or []:
        path = str(item.get("path") or "")
        expected = item.get("equals")
        actual = _path_get(state, path)
        assertions.append({"ok": actual == expected, "message": f"state {path}: expected {expected!r}, got {actual!r}"})
    for text in sample.get("expected_markdown_contains") or []:
        markdown = str(state.get("markdown_report") or "")
        assertions.append({"ok": str(text) in markdown, "message": f"markdown missing {text!r}"})
    return assertions


# ====== 功能：按点号路径读取嵌套 dict/list 中的值。 ======
def _path_get(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current

# ====== 功能：检查工具调用顺序是否包含期望子序列。 ======
def _contains_subsequence(values: list[str], expected: list[Any]) -> bool:
    if not expected:
        return True
    expected_values = [str(item) for item in expected]
    pos = 0
    for value in values:
        if value == expected_values[pos]:
            pos += 1
            if pos == len(expected_values):
                return True
    return False


# ====== 功能：命令行入口函数。 ======
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset")
    parser.add_argument("--config", default=None)
    parser.add_argument("--output-dir", default="eval_runs")
    parser.add_argument("--simulate-tools", action="store_true", help="Use simulated EDA frontend and external PCB tools for end-to-end evaluation.")
    parser.add_argument("--simulate-frontend-only", action="store_true", help="Use simulated EDA frontend but keep real external PCB tools.")
    parser.add_argument("--require-model-planner", action="store_true", help="Fail evaluation when the real model planner is not called or falls back to deterministic rules.")
    args = parser.parse_args()
    report = asyncio.run(LiveEvaluationRunner(args.config, output_dir=args.output_dir, simulate_tools=args.simulate_tools, simulate_frontend_only=args.simulate_frontend_only, require_model_planner=args.require_model_planner).run_dataset(args.dataset))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()








