from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, List

from .example_bank import (
    candidate_quality as _candidate_quality,
    format_examples as _format_examples,
    load_example_pool as _load_example_pool,
    select_examples as _select_examples,
)
from .pipeline_wrapper import PCBPipelineWrapper, normalize_routing_response
from .skill_bank import SkillCardBank as PortableSkillCardBank
from .utils import EvalMetrics, RoutingResult, RoutingTask


class DRCEvaluator:
    """Protocol-like base for type hints; concrete adapters live outside vsea_core."""

    ai_pcb_eval_path: Path

    def evaluate_with_drc(
        self,
        routing_output: str,
        task: RoutingTask,
        method: str,
        runtime: float,
        output_path: str = "",
    ) -> EvalMetrics:
        raise NotImplementedError


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


class VSEARerouteRouter:
    """VSEA reroute engine with iterative LLM repair guided by DRC reports."""

    method = "vsea_reroute"

    def __init__(
        self,
        pipeline: PCBPipelineWrapper,
        evaluator: DRCEvaluator,
        samples: int = 3,
        shots: int = 3,
        max_rounds: int = 3,
        repair_samples: int = 3,
        repair_retries: int = 3,
        skill_bank_path: str | Path | None = None,
    ):
        self.pipeline = pipeline
        self.evaluator = evaluator
        self.samples = max(1, samples)
        self.shots = max(0, shots)
        self.max_rounds = max(1, max_rounds)
        self.repair_samples = max(
            0,
            _env_int("REROUTE_REPAIR_SAMPLES", repair_samples),
        )
        self.repair_retries = max(
            1,
            _env_int("REROUTE_REPAIR_RETRIES", repair_retries),
        )
        self.skill_bank = PortableSkillCardBank(
            skill_bank_path
            or os.getenv("REROUTE_SKILL_BANK_PATH")
            or Path.cwd() / "skill_bank.jsonl",
            query_builder=_skill_retrieval_query,
        )
        self._drc_path = getattr(evaluator, "ai_pcb_eval_path", None)
        self._thread_local = threading.local()
        self.example_pool = _load_example_pool(Path.cwd())
        self.occupancy_routing = _env_int(
            "REROUTE_OCCUPANCY_ROUTING",
            0,
        ) > 0
        self.occupancy_net_retries = max(
            1,
            _env_int("REROUTE_OCCUPANCY_NET_RETRIES", 2),
        )

    def route(self, task: RoutingTask) -> RoutingResult:
        start_time = time.perf_counter()
        examples = _select_examples(self.example_pool, task.task_id, self.shots)
        few_shot_prompt = _format_examples(examples)

        round_feedback: List[str] = []
        round_debug: List[dict[str, Any]] = []
        candidate_errors: List[str] = []
        best_overall: RoutingResult | None = None
        best_overall_metrics: EvalMetrics | None = None
        best_overall_quality: tuple[Any, ...] | None = None
        stop_reason = "max_rounds_reached"
        last_failed_round_drc: int | None = None

        for round_idx in range(1, self.max_rounds + 1):
            round_candidates: List[RoutingResult] = []
            round_metrics: List[EvalMetrics] = []
            round_quality: List[tuple[Any, ...]] = []
            round_candidate_debug: List[dict[str, Any]] = []
            round_success_found = False

            if round_idx > 1 and best_overall is not None and best_overall_metrics is not None:
                seed_validation = _route_validation(task, best_overall.routing_output)
                if (
                    not seed_validation["missing_expected_nets"]
                    and not seed_validation["unexpected_output_nets"]
                ):
                    seed_quality = _verified_candidate_quality(
                        best_overall_metrics,
                        seed_validation,
                    )
                    seed_result = RoutingResult(
                        method=f"{self.method}_round_{round_idx}_seed_best",
                        task_id=task.task_id,
                        routing_output=best_overall.routing_output,
                        runtime=best_overall.runtime,
                        meta={
                            "round": round_idx,
                            "candidate": "seed_best",
                            "seed_from_round": best_overall.meta.get("round"),
                            "seed_from_candidate": best_overall.meta.get("candidate"),
                        },
                    )
                    round_candidates.append(seed_result)
                    round_metrics.append(best_overall_metrics)
                    round_quality.append(seed_quality)
                    round_candidate_debug.append(
                        {
                            "round": round_idx,
                            "candidate": "seed_best",
                            "seed_from_round": best_overall.meta.get("round"),
                            "seed_from_candidate": best_overall.meta.get("candidate"),
                            "routing_chars": len(best_overall.routing_output),
                            "runtime": best_overall.runtime,
                            "validation": seed_validation,
                            "metrics": _metrics_summary(best_overall_metrics),
                            "quality": list(seed_quality),
                            "self_evolution_seed": True,
                        }
                    )

            main_jobs = [
                {
                    "candidate_idx": candidate_idx,
                    "prior": _build_round_prior(
                        task=task,
                        few_shot_prompt=few_shot_prompt,
                        round_feedback=round_feedback,
                        round_idx=round_idx,
                        max_rounds=self.max_rounds,
                        candidate_idx=candidate_idx,
                        samples=self.samples,
                    ),
                }
                for candidate_idx in range(1, self.samples + 1)
            ]
            for item in self._run_parallel_main_candidate_jobs(task, main_jobs, round_idx):
                if item.get("error"):
                    message = f"round_{round_idx}.candidate_{item.get('candidate_idx')}: {item.get('error')}"
                    candidate_errors.append(message)
                    round_candidate_debug.append(
                        {
                            "round": round_idx,
                            "candidate": item.get("candidate_idx"),
                            "error": item.get("error"),
                        }
                    )
                    continue

                result = item["result"]
                metrics = item["metrics"]
                validation = item["validation"]
                quality = item["quality"]
                round_candidates.append(result)
                round_metrics.append(metrics)
                round_quality.append(quality)
                round_candidate_debug.append(
                    {
                        "round": round_idx,
                        "candidate": item.get("candidate_idx"),
                        "routing_chars": len(result.routing_output),
                        "runtime": result.runtime,
                        "validation": validation,
                        "metrics": _metrics_summary(metrics),
                        "quality": list(quality),
                    }
                )

                if best_overall_quality is None or quality > best_overall_quality:
                    best_overall = result
                    best_overall_metrics = metrics
                    best_overall_quality = quality
                if metrics.success:
                    round_success_found = True

                if round_success_found:
                    break

            if (
                not round_success_found
                and self.repair_samples > 0
                and round_candidates
                and round_quality
            ):
                source_idx = max(range(len(round_quality)), key=lambda idx: round_quality[idx])
                source_result = round_candidates[source_idx]
                source_metrics = round_metrics[source_idx]
                source_candidate = source_result.meta.get("candidate")
                repair_candidates = self._generate_llm_repair_candidates(
                    task=task,
                    routing_output=source_result.routing_output,
                    metrics=source_metrics,
                    round_idx=round_idx,
                    candidate_idx=source_candidate,
                )
                for repair_idx, repair_payload in enumerate(repair_candidates, start=1):
                    repair_routing = (
                        repair_payload.get("routing_output")
                        if isinstance(repair_payload, dict)
                        else repair_payload
                    )
                    if not isinstance(repair_routing, str) or not repair_routing.strip():
                        continue
                    repair_start = time.perf_counter()
                    repair_metrics = self._evaluate_with_drc(
                        repair_routing,
                        task,
                        method=(
                            f"{self.method}_round_{round_idx}_candidate_{source_candidate}"
                            f"_repair_{repair_idx}"
                        ),
                        runtime=0.0,
                    )
                    repair_runtime = source_result.runtime + time.perf_counter() - repair_start
                    repair_validation = _route_validation(task, repair_routing)
                    repair_validation["repair_contract_ok"] = True
                    repair_validation["self_evolution_repair"] = True
                    repair_validation["repair_strategy"] = "target_net_decomposed"
                    if isinstance(repair_payload, dict):
                        repair_validation["active_repair_nets"] = repair_payload.get(
                            "active_repair_nets",
                            [],
                        )
                        repair_validation["active_issue_count_before"] = repair_payload.get(
                            "active_issue_count_before"
                        )
                        repair_validation["active_issue_count_after"] = repair_payload.get(
                            "active_issue_count_after"
                        )
                        repair_validation["local_repair_improved"] = repair_payload.get(
                            "local_repair_improved"
                        )
                        repair_validation["total_repair_improved"] = repair_payload.get(
                            "total_repair_improved"
                        )
                    repair_quality = _verified_candidate_quality(
                        repair_metrics,
                        repair_validation,
                    )
                    repair_result = RoutingResult(
                        method=(
                            f"{self.method}_round_{round_idx}_candidate_{source_candidate}"
                            f"_repair_{repair_idx}"
                        ),
                        task_id=task.task_id,
                        routing_output=repair_routing,
                        runtime=repair_runtime,
                        meta={
                            "round": round_idx,
                            "candidate": f"{source_candidate}.repair_{repair_idx}",
                            "repair_source_candidate": source_candidate,
                            "repair_strategy": "target_net_decomposed",
                            "repair_payload": repair_payload if isinstance(repair_payload, dict) else {},
                        },
                    )
                    round_candidates.append(repair_result)
                    round_metrics.append(repair_metrics)
                    round_quality.append(repair_quality)
                    round_candidate_debug.append(
                        {
                            "round": round_idx,
                            "candidate": f"{source_candidate}.repair_{repair_idx}",
                            "repair_source_candidate": source_candidate,
                            "routing_chars": len(repair_routing),
                            "runtime": repair_runtime,
                            "validation": repair_validation,
                            "metrics": _metrics_summary(repair_metrics),
                            "quality": list(repair_quality),
                            "repair": "llm_self_evolution_repair",
                            "repair_strategy": "target_net_decomposed",
                            "repair_payload": repair_payload if isinstance(repair_payload, dict) else {},
                        }
                    )

                    if best_overall_quality is None or repair_quality > best_overall_quality:
                        best_overall = repair_result
                        best_overall_metrics = repair_metrics
                        best_overall_quality = repair_quality
                    if repair_metrics.success:
                        round_success_found = True
                        break

            if not round_candidates:
                round_debug.append(
                    {
                        "round": round_idx,
                        "candidate_debug": round_candidate_debug,
                        "round_error": "all_candidates_failed",
                    }
                )
                continue

            best_round_idx = max(range(len(round_quality)), key=lambda idx: round_quality[idx])
            best_round = round_candidates[best_round_idx]
            best_round_metrics = round_metrics[best_round_idx]
            best_round_quality = round_quality[best_round_idx]
            feedback = ""

            if best_round_metrics.success:
                stop_reason = "drc_passed"
                round_debug.append(
                    {
                        "round": round_idx,
                        "best_candidate": best_round.meta.get("candidate"),
                        "best_quality": list(best_round_quality),
                        "best_metrics": _metrics_summary(best_round_metrics),
                        "candidate_debug": round_candidate_debug,
                        "feedback": feedback,
                    }
                )
                break

            last_failed_round_drc = best_round_metrics.drc_violation

            if round_idx < self.max_rounds:
                feedback = self._generate_failure_feedback(
                    task=task,
                    routing_output=best_round.routing_output,
                    metrics=best_round_metrics,
                    round_idx=round_idx,
                )
                if feedback:
                    round_feedback.append(feedback)

            round_debug.append(
                {
                    "round": round_idx,
                    "best_candidate": best_round.meta.get("candidate"),
                    "best_quality": list(best_round_quality),
                    "best_metrics": _metrics_summary(best_round_metrics),
                    "candidate_debug": round_candidate_debug,
                    "feedback": feedback,
                }
            )

        if best_overall is None or best_overall_metrics is None:
            raise RuntimeError(
                "All VSEA reroute candidates failed: "
                + " | ".join(candidate_errors)
            )

        final_result = RoutingResult(
            method=self.method,
            task_id=task.task_id,
            routing_output=best_overall.routing_output,
            runtime=time.perf_counter() - start_time,
            meta={
                "best_round": best_overall.meta.get("round"),
                "best_candidate": best_overall.meta.get("candidate"),
                "best_candidate_runtime": best_overall.runtime,
                "best_metrics": _metrics_summary(best_overall_metrics),
                "few_shot_examples": [
                    {"task_id": item["task_id"]}
                    for item in examples
                ],
                "samples": self.samples,
                "shots": self.shots,
                "max_rounds": self.max_rounds,
                "repair_samples": self.repair_samples,
                "repair_retries": self.repair_retries,
                "rounds_completed": len(round_debug),
                "stop_reason": stop_reason,
                "round_feedback_count": len(round_feedback),
                "round_debug": _sanitize_round_debug(round_debug),
                "candidate_errors": candidate_errors,
            },
        )
        return final_result

    def _thread_evaluator(self) -> DRCEvaluator:
        if not self._drc_path or _env_int("REROUTE_THREAD_LOCAL_DRC", 0) <= 0:
            return self.evaluator
        evaluator = getattr(self._thread_local, "evaluator", None)
        if evaluator is None:
            evaluator = DRCEvaluator(self._drc_path)
            self._thread_local.evaluator = evaluator
        return evaluator

    def _evaluate_with_drc(
        self,
        routing_output: str,
        task: RoutingTask,
        method: str,
        runtime: float,
        output_path: str = "",
    ) -> EvalMetrics:
        return self._thread_evaluator().evaluate_with_drc(
            routing_output=routing_output,
            task=task,
            method=method,
            runtime=runtime,
            output_path=output_path,
        )

    def _run_parallel_main_candidate_jobs(
        self,
        task: RoutingTask,
        jobs: List[dict[str, Any]],
        round_idx: int,
    ) -> List[dict[str, Any]]:
        max_workers = max(
            1,
            min(
                len(jobs),
                _env_int("REROUTE_MAIN_PARALLELISM", self.samples),
            ),
        )
        if max_workers == 1:
            return [
                self._run_single_main_candidate_job(task, job, round_idx)
                for job in jobs
            ]
        results: List[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self._run_single_main_candidate_job, task, job, round_idx)
                for job in jobs
            ]
            for future in as_completed(futures):
                results.append(future.result())
        return sorted(results, key=lambda item: item.get("candidate_idx", 0))

    def _run_single_main_candidate_job(
        self,
        task: RoutingTask,
        job: dict[str, Any],
        round_idx: int,
    ) -> dict[str, Any]:
        candidate_idx = job["candidate_idx"]
        cand_start = time.perf_counter()
        try:
            occupancy_debug: dict[str, Any] = {}
            if (
                self.occupancy_routing
                and len(_extract_expected_connections(task.task_prompt)) > 1
            ):
                routing, occupancy_debug = self._generate_occupancy_aware_routing(
                    task=task,
                    prior=job["prior"],
                    round_idx=round_idx,
                    candidate_idx=candidate_idx,
                )
            else:
                routing = self.pipeline.run_llm_cot_plan_routing(
                    task,
                    experience_prompt=job["prior"],
                    call_name=(
                        f"vsea_reroute.round_{round_idx}."
                        f"candidate_{candidate_idx}.{task.task_id}"
                    ),
                )
                occupancy_debug = {}
            routing = _normalize_repair_syntax(routing)
            routing = _insert_missing_layer_transition_vias(routing)
            runtime = time.perf_counter() - cand_start
            metrics = self._evaluate_with_drc(
                routing,
                task,
                method=f"{self.method}_round_{round_idx}_candidate_{candidate_idx}",
                runtime=runtime,
            )
            validation = _route_validation(task, routing)
            quality = _verified_candidate_quality(metrics, validation)
            result = RoutingResult(
                method=f"{self.method}_round_{round_idx}_candidate_{candidate_idx}",
                task_id=task.task_id,
                routing_output=routing,
                runtime=runtime,
                meta={
                    "round": round_idx,
                    "candidate": candidate_idx,
                    "occupancy_aware": bool(occupancy_debug),
                    "occupancy_debug": occupancy_debug,
                },
            )
            return {
                "candidate_idx": candidate_idx,
                "result": result,
                "metrics": metrics,
                "validation": validation,
                "quality": quality,
            }
        except Exception as exc:
            return {
                "candidate_idx": candidate_idx,
                "error": str(exc),
            }

    def _generate_occupancy_aware_routing(
        self,
        task: RoutingTask,
        prior: str,
        round_idx: int,
        candidate_idx: int,
    ) -> tuple[str, dict[str, Any]]:
        connections = _extract_expected_connections(task.task_prompt)
        plan = self._generate_global_occupancy_plan(
            task=task,
            connections=connections,
            prior=prior,
            round_idx=round_idx,
            candidate_idx=candidate_idx,
        )
        ordered_connections = _order_connections_from_plan(plan, connections)
        routed_parts: List[str] = []
        step_debug: List[dict[str, Any]] = []
        routed_output = ""

        for step_idx, conn in enumerate(ordered_connections, start=1):
            previous_bad = ""
            previous_violations: List[str] = []
            selected_output = ""
            selected_violations: List[str] = []
            selected_occupied: dict[str, Any] = {}
            for attempt_idx in range(1, self.occupancy_net_retries + 1):
                occupied_map = _build_occupied_map(
                    task=task,
                    connection=conn,
                    routed_output=routed_output,
                    all_connections=connections,
                    remaining_connections=ordered_connections[step_idx:],
                )
                messages = _build_stateful_net_routing_messages(
                    task=task,
                    connection=conn,
                    all_connections=connections,
                    plan=plan,
                    occupied_map=occupied_map,
                    routed_output=routed_output,
                    prior=prior if step_idx == 1 else "",
                    step_idx=step_idx,
                    total_steps=len(ordered_connections),
                    candidate_idx=candidate_idx,
                    attempt_idx=attempt_idx,
                    previous_bad=previous_bad,
                    previous_violations=previous_violations,
                )
                try:
                    raw = self.pipeline.call_llm(
                        messages,
                        call_name=(
                            f"vsea_reroute.occupancy_round_{round_idx}."
                            f"candidate_{candidate_idx}.net_{conn['net']}.attempt_{attempt_idx}."
                            f"{task.task_id}"
                        ),
                    )
                except Exception as exc:
                    selected_violations = [f"LLM single-net route call failed: {exc}"]
                    if selected_output.strip():
                        break
                    previous_violations = selected_violations
                    previous_bad = ""
                    continue
                net_output = normalize_routing_response(raw)
                net_output = _normalize_repair_syntax(net_output)
                net_output = _only_objects_for_net(net_output, str(conn["net"]))
                net_output = _insert_missing_layer_transition_vias(net_output)
                violations = _stateful_net_route_violations(
                    task=task,
                    connection=conn,
                    net_output=net_output,
                    routed_output=routed_output,
                    occupied_map=occupied_map,
                )
                selected_output = net_output
                selected_violations = violations
                selected_occupied = occupied_map
                if not violations:
                    break
                previous_bad = net_output
                previous_violations = violations

            if selected_output.strip():
                routed_parts.extend(_split_kicad_routing_objects(selected_output))
                routed_output = "\n".join(routed_parts).strip()
            step_debug.append(
                {
                    "step": step_idx,
                    "net": conn.get("net"),
                    "net_name": conn.get("net_name"),
                    "attempts": attempt_idx,
                    "accepted_without_local_violations": not selected_violations,
                    "local_violations": selected_violations[:6],
                    "occupied_summary": _occupied_map_debug_summary(selected_occupied),
                    "output_chars": len(selected_output),
                }
            )

        return (
            _insert_missing_layer_transition_vias(routed_output),
            {
                "mode": "global_plan_then_stateful_per_net",
                "plan": plan,
                "ordered_nets": [str(conn.get("net")) for conn in ordered_connections],
                "steps": step_debug,
            },
        )

    def _generate_global_occupancy_plan(
        self,
        task: RoutingTask,
        connections: List[dict[str, Any]],
        prior: str,
        round_idx: int,
        candidate_idx: int,
    ) -> dict[str, Any]:
        overview = _build_global_occupancy_overview(task, connections)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 PCB 多网络布线的全局规划智能体。"
                    "你的任务不是输出 KiCad，而是给逐根布线 agent 制定 net 顺序、层分配和通道避让计划。"
                    "必须考虑：先布线的目标 net 会成为后续目标 net 的新增障碍。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"任务 ID：{task.task_id}\n"
                    f"缺失走线描述：\n{task.task_prompt}\n\n"
                    f"可用信号层：{json.dumps(_available_signal_layers(task.context_kicad), ensure_ascii=False)}\n"
                    f"目标连接：\n{json.dumps(connections, ensure_ascii=False, indent=2)}\n\n"
                    f"局部障碍概览：\n{json.dumps(overview, ensure_ascii=False, indent=2)[:9000]}\n\n"
                    f"少量参考样例/规则先验，只作策略参考，禁止照抄坐标：\n{prior[:3500]}\n\n"
                    "请只输出 JSON，不要 Markdown。格式：\n"
                    "{\n"
                    '  "net_order": ["24", "99", ...],\n'
                    '  "net_plans": {\n'
                    '    "24": {"primary_layer": "Top或内层", "inner_layer": "SIGxx", '
                    '"corridor": "上/下/左/右侧绕行", "avoid": ["..."], "reason": "..."}\n'
                    "  },\n"
                    '  "global_strategy": "先短线/边界线，长斜线最后；每根线生成后成为 occupied obstacle"\n'
                    "}\n"
                    "规划目标：减少多根目标线之间的同层交叉。"
                ),
            },
        ]
        try:
            raw = self.pipeline.call_llm(
                messages,
                call_name=(
                    f"vsea_reroute.occupancy_plan_round_{round_idx}."
                    f"candidate_{candidate_idx}.{task.task_id}"
                ),
            )
            plan = _parse_json_object(raw)
        except Exception as exc:
            plan = {"planning_error": str(exc)}
        if not isinstance(plan, dict):
            plan = {}
        plan.setdefault("net_order", _default_occupancy_net_order(connections))
        plan.setdefault("net_plans", {})
        plan.setdefault(
            "global_strategy",
            "逐根布线；已完成目标 net 作为后续 occupied obstacle；长线和斜线优先使用内层绕行。",
        )
        return plan

    def _generate_failure_feedback(
        self,
        task: RoutingTask,
        routing_output: str,
        metrics: EvalMetrics,
        round_idx: int,
    ) -> str:
        report = _compact_drc_report(metrics)
        validation = _route_validation(task, routing_output)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是资深 PCB DRC 诊断工程师。请根据 DRC 报告提炼失败原因，"
                    "并给出下一轮布线应遵守的具体策略。只能修改当前任务的目标 net，"
                    "不能建议重布或删除板上已有的非目标 net。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"任务 ID：{task.task_id}\n"
                    f"缺失走线描述：\n{task.task_prompt}\n\n"
                    f"上一轮最优候选布线：\n{routing_output[:3000]}\n\n"
                    f"目标 net / 输出 net 结构化校验：\n"
                    f"{json.dumps(validation, ensure_ascii=False, indent=2)[:3000]}\n\n"
                    f"DRC / 评测报告摘要：\n"
                    f"{json.dumps(report, ensure_ascii=False, indent=2)[:5000]}\n\n"
                    "请严格基于上面的结构化校验和 DRC issue 明细输出两部分：\n"
                    "1. 失败原因：用 2-5 条说明，必须引用具体 rule、net、pad/坐标/segment 信息；"
                    "如果 missing_expected_nets 非空，必须把缺失 net 作为最高优先级错误；"
                    "如果 unexpected_output_nets 非空，必须指出输出了错误 net；"
                    "如果 HR_DRC_SEGMENT_CROSSING 涉及目标 net 和已有 net，"
                    "必须表述为目标 net 穿过已有走线，而不是要求修改已有 net。\n"
                    "2. 下一轮策略：用 2-5 条给出可执行修改；必须明确下一轮应该输出哪些 net、"
                    "每个 net 的起终点坐标、需要避开的 crossing/未连接问题。\n"
                    "若目标 net 在 Top 层直连穿过已有走线，优先建议在靠近起终点处放 via，"
                    "切到可用内层分段绕行，再回到 Top 层连接焊盘；不要再输出单条斜线直连。\n"
                    "若 issue 显示 BGA pad 未逃逸、fanout 不足、未连接或 escape 不完整，"
                    "优先把对应 pad/component/layer/坐标处的短引出、过孔、fanout 或连通性修复作为下一轮主策略，"
                    "不要泛化成 crossing 绕障。\n"
                    "若 DRC 报告没有给 segment endpoint / crossing point，就明确说明信息缺失，"
                    "不要编造不存在的 endpoints。\n"
                    "禁止编造 DRC 报告中没有的对象；不要输出 KiCad 代码。"
                ),
            },
        ]
        try:
            return self.pipeline.call_llm(
                messages,
                call_name=f"vsea_reroute.feedback_round_{round_idx}.{task.task_id}",
            ).strip()
        except Exception as exc:
            return (
                "失败原因：DRC 未通过，自动反馈 LLM 调用失败。"
                f"错误：{exc}\n"
                "下一轮策略：减少跨越障碍区域，缩短路径，避免不必要 via，"
                "优先保持同层连续曼哈顿路径。"
            )

    def _generate_llm_repair_candidates(
        self,
        task: RoutingTask,
        routing_output: str,
        metrics: EvalMetrics,
        round_idx: int,
        candidate_idx: Any,
    ) -> List[dict[str, Any]]:
        candidates: List[dict[str, Any]] = []
        current_output = routing_output
        current_metrics = metrics
        failed_repair_memory: List[dict[str, Any]] = []

        for wave_idx in range(1, self.repair_retries + 1):
            report = _compact_drc_report(current_metrics)
            validation = _route_validation(task, current_output)
            full_contract = _build_llm_repair_contract(task, current_output, current_metrics)
            full_contract["target_repair_attempts"] = _repair_attempt_stats(
                failed_repair_memory
            )
            repair_jobs = []
            scheduled_active_nets: set[str] = set()
            for repair_idx in range(1, self.repair_samples + 1):
                repair_contract = _activate_repair_target(full_contract, repair_idx)
                if not repair_contract.get("crossing_targets"):
                    continue
                active_set = {str(net) for net in repair_contract.get("active_repair_nets") or []}
                if active_set and active_set <= scheduled_active_nets:
                    continue
                scheduled_active_nets.update(active_set)
                repair_contract["previous_failed_repairs"] = _relevant_failed_repair_memory(
                    failed_repair_memory,
                    repair_contract,
                    limit=3,
                )
                skill_cards = self.skill_bank.retrieve(
                    task=task,
                    repair_contract=repair_contract,
                    report=report,
                    positive_k=1,
                    negative_k=1,
                )
                repair_jobs.append(
                    {
                        "repair_idx": repair_idx,
                        "wave_idx": wave_idx,
                        "base_output": current_output,
                        "base_metrics": current_metrics,
                        "validation": validation,
                        "report": report,
                        "repair_contract": repair_contract,
                        "active_issue_count_before": _active_crossing_issue_count(
                            report,
                            repair_contract,
                        ),
                        "skill_cards": skill_cards,
                    }
                )
            if not repair_jobs:
                break

            probe_items = self._run_parallel_repair_wave_jobs(
                task=task,
                jobs=repair_jobs,
                round_idx=round_idx,
                candidate_idx=candidate_idx,
                wave_idx=wave_idx,
            )
            accepted_this_wave: List[dict[str, Any]] = []
            for item in sorted(probe_items, key=lambda value: value.get("repair_idx", 0)):
                repaired = item.get("routing_output") or ""
                repair_contract = item["repair_contract"]
                violations = item.get("violations") or []
                if violations:
                    failed_repair_memory.append(
                        _failed_repair_memory_item(
                            repaired,
                            repair_contract,
                            violations=violations,
                            active_issue_count_before=item.get("active_issue_count_before"),
                        )
                    )
                    continue
                probe_metrics = item["probe_metrics"]
                probe_report = _compact_drc_report(probe_metrics)
                active_issue_count_after = item["active_issue_count_after"]
                local_improved = (
                    active_issue_count_after < item["active_issue_count_before"]
                )
                total_improved = (
                    probe_metrics.drc_violation < item["base_metrics"].drc_violation
                )
                payload = {
                    "routing_output": repaired,
                    "repair_contract": _repair_contract_debug_summary(repair_contract),
                    "active_repair_nets": repair_contract.get("active_repair_nets") or [],
                    "active_issue_count_before": item["active_issue_count_before"],
                    "active_issue_count_after": active_issue_count_after,
                    "local_repair_improved": local_improved,
                    "total_repair_improved": total_improved,
                    "parallel_repair_wave": wave_idx,
                    "probe_metrics": _metrics_summary(probe_metrics),
                    "repair_idx": item.get("repair_idx"),
                    "retry_idx": wave_idx,
                }
                candidates.append(payload)
                accepted_this_wave.append(
                    {
                        "payload": payload,
                        "metrics": probe_metrics,
                    }
                )
                if not (probe_metrics.success or local_improved or total_improved):
                    failed_repair_memory.append(
                        _failed_repair_memory_item(
                            repaired,
                            repair_contract,
                            violations=[],
                            probe_report=probe_report,
                            metrics=probe_metrics,
                            active_issue_count_before=item.get("active_issue_count_before"),
                            active_issue_count_after=active_issue_count_after,
                        )
                    )
            improved = [
                item
                for item in accepted_this_wave
                if item["metrics"].success
                or item["payload"].get("total_repair_improved")
                or item["payload"].get("local_repair_improved")
            ]
            if not improved:
                break
            best_item = max(
                improved,
                key=lambda item: _parallel_repair_acceptance_quality(
                    item["metrics"],
                    item["payload"],
                ),
            )
            current_output = best_item["payload"]["routing_output"]
            current_metrics = best_item["metrics"]
            if current_metrics.success:
                break
        return candidates

    def _run_parallel_repair_wave_jobs(
        self,
        task: RoutingTask,
        jobs: List[dict[str, Any]],
        round_idx: int,
        candidate_idx: Any,
        wave_idx: int,
    ) -> List[dict[str, Any]]:
        repair_workers = max(
            1,
            min(
                len(jobs),
                _env_int("REROUTE_REPAIR_PARALLELISM", 3),
            ),
        )
        probe_workers = max(
            1,
            min(
                len(jobs),
                _env_int("REROUTE_DRC_PARALLELISM", 3),
            ),
        )
        results: List[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=repair_workers) as repair_executor:
            with ThreadPoolExecutor(max_workers=probe_workers) as probe_executor:
                repair_futures = {
                    repair_executor.submit(
                        self._run_single_repair_job,
                        task,
                        job,
                        round_idx,
                        candidate_idx,
                    ): job
                    for job in jobs
                }
                probe_futures = {}
                for repair_future in as_completed(repair_futures):
                    job = repair_futures[repair_future]
                    try:
                        item = repair_future.result()
                    except Exception as exc:
                        item = {
                            **job,
                            "routing_output": "",
                            "llm_error": f"LLM repair call failed: {exc}",
                        }
                    probe_futures[
                        probe_executor.submit(
                            self._probe_single_repair_result,
                            task,
                            item,
                            round_idx,
                            candidate_idx,
                            wave_idx,
                        )
                    ] = item
                for probe_future in as_completed(probe_futures):
                    results.append(probe_future.result())
        return results

    def _run_parallel_repair_probe_jobs(
        self,
        task: RoutingTask,
        llm_results: List[dict[str, Any]],
        round_idx: int,
        candidate_idx: Any,
        wave_idx: int,
    ) -> List[dict[str, Any]]:
        max_workers = max(
            1,
            min(
                len(llm_results),
                _env_int("REROUTE_DRC_PARALLELISM", 3),
            ),
        )
        if max_workers == 1:
            return [
                self._probe_single_repair_result(
                    task,
                    item,
                    round_idx,
                    candidate_idx,
                    wave_idx,
                )
                for item in llm_results
            ]
        results: List[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    self._probe_single_repair_result,
                    task,
                    item,
                    round_idx,
                    candidate_idx,
                    wave_idx,
                )
                for item in llm_results
            ]
            for future in as_completed(futures):
                results.append(future.result())
        return results

    def _probe_single_repair_result(
        self,
        task: RoutingTask,
        item: dict[str, Any],
        round_idx: int,
        candidate_idx: Any,
        wave_idx: int,
    ) -> dict[str, Any]:
        repaired = item.get("routing_output") or ""
        repair_contract = item["repair_contract"]
        if not re.search(r"\(\s*(segment|via)\b", repaired, flags=re.IGNORECASE):
            return {
                **item,
                "violations": ["没有输出任何 KiCad segment/via 对象。"],
            }
        violations = _repair_contract_violations(task, repaired, repair_contract)
        if violations:
            return {
                **item,
                "violations": violations,
            }
        probe_metrics = self._evaluate_with_drc(
            repaired,
            task,
            method=(
                f"{self.method}_repair_probe_round_{round_idx}."
                f"candidate_{candidate_idx}.repair_{item.get('repair_idx')}."
                f"wave_{wave_idx}"
            ),
            runtime=0.0,
        )
        probe_report = _compact_drc_report(probe_metrics)
        return {
            **item,
            "probe_metrics": probe_metrics,
            "active_issue_count_after": _active_crossing_issue_count(
                probe_report,
                repair_contract,
            ),
        }

    def _run_parallel_repair_jobs(
        self,
        task: RoutingTask,
        jobs: List[dict[str, Any]],
        round_idx: int,
        candidate_idx: Any,
    ) -> List[dict[str, Any]]:
        max_workers = max(1, min(len(jobs), _env_int("REROUTE_REPAIR_PARALLELISM", 3)))
        if max_workers == 1:
            return [
                self._run_single_repair_job(task, job, round_idx, candidate_idx)
                for job in jobs
            ]
        results: List[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self._run_single_repair_job, task, job, round_idx, candidate_idx)
                for job in jobs
            ]
            for future in as_completed(futures):
                results.append(future.result())
        return results

    def _run_single_repair_job(
        self,
        task: RoutingTask,
        job: dict[str, Any],
        round_idx: int,
        candidate_idx: Any,
    ) -> dict[str, Any]:
        repair_idx = job["repair_idx"]
        wave_idx = job["wave_idx"]
        messages = _build_llm_repair_messages(
            task=task,
            routing_output=job["base_output"],
            validation=job["validation"],
            report=job["report"],
            repair_contract=job["repair_contract"],
            repair_idx=repair_idx,
            repair_samples=self.repair_samples,
            previous_bad="",
            previous_violations=[],
            skill_cards=job["skill_cards"],
        )
        try:
            raw = self.pipeline.call_llm(
                messages,
                call_name=(
                    f"vsea_reroute.repair_round_{round_idx}."
                    f"candidate_{candidate_idx}.repair_{repair_idx}."
                    f"wave_{wave_idx}.{task.task_id}"
                ),
            )
            repaired = normalize_routing_response(raw)
            repaired = _normalize_repair_syntax(repaired)
            repaired = _merge_missing_expected_nets_from_base(
                task=task,
                base_output=job["base_output"],
                repaired_output=repaired,
            )
            repaired = _insert_missing_layer_transition_vias(repaired)
            error = ""
        except Exception as exc:
            repaired = ""
            error = f"LLM repair call failed: {exc}"
        return {
            **job,
            "routing_output": repaired,
            "llm_error": error,
        }


def _build_round_prior(
    task: RoutingTask,
    few_shot_prompt: str,
    round_feedback: List[str],
    round_idx: int,
    max_rounds: int,
    candidate_idx: int,
    samples: int,
) -> str:
    parts: List[str] = []
    if few_shot_prompt:
        parts.append(few_shot_prompt)
    validation = _route_validation(task, "")
    hard_constraints = [
        "当前任务硬约束：",
        "1. 只能输出当前任务缺失详情中的目标 net，不能输出其它 net。",
        "2. 每个目标 net 都必须输出完整连接，不能漏 net。",
        "3. 起点和终点必须来自当前任务缺失详情，不要使用参考样例中的坐标。",
        "4. 如果 Top 层起终点直线会穿过已有走线，必须拆成多段并使用 via/内层绕行；不要输出单条斜线直连。",
        "可用信号层：",
        json.dumps(_available_signal_layers(task.context_kicad), ensure_ascii=False),
        "目标 net 和端点：",
        json.dumps(validation["expected_connections"], ensure_ascii=False, indent=2),
    ]
    parts.append("\n".join(hard_constraints))
    if round_feedback:
        feedback_lines = []
        for idx, item in enumerate(round_feedback, start=1):
            feedback_lines.append(f"第 {idx} 轮 DRC 失败反馈：\n{item}")
        parts.append(
            "Self-verification 反馈：下面是前几轮最优候选经过 DRC 检查后的失败原因和改进策略。"
            "下一轮必须显式规避这些问题。"
            "如果反馈指出缺少目标 net 或输出了错误 net，下一轮必须优先修复 net id 和端点连接。\n\n"
            + "\n\n".join(feedback_lines)
        )
    parts.append(
        "Self-consistency + self-verification 采样要求："
        f"当前是第 {round_idx}/{max_rounds} 轮验证，第 {candidate_idx}/{samples} 条独立候选。"
        "请结合参考样例和已有 DRC 失败反馈重新规划；"
        "不要机械复述示例，不要复制示例中的 net id、坐标或层名；"
        "必须以当前任务缺失详情中的 net id、起点和终点为准；"
        "不得输出当前任务之外的 net；"
        "每个缺失网络都必须至少有一条从起点连向终点方向的完整路径；"
        "优先短路径、少 via、避免同层交叉和障碍区域；"
        "遇到同层 crossing 风险时，使用 Top 短引出、via、内层分段、via、Top 短接回焊盘的结构；"
        "最终只输出合法 KiCad 布线对象。"
    )
    return "\n\n".join(parts)


def _metrics_summary(metrics: EvalMetrics) -> dict[str, Any]:
    return {
        "score": metrics.score,
        "drc_violation": metrics.drc_violation,
        "drc_backend_score": metrics.drc_backend_score,
        "success": metrics.success,
        "path_length": metrics.path_length,
        "via_count": metrics.via_count,
        "status": metrics.status,
    }


def sanitize_router_meta(meta: dict[str, Any]) -> dict[str, Any]:
    if os.getenv("REROUTE_ENABLE_DEBUG") == "1":
        return meta
    return {
        "best_round": meta.get("best_round"),
        "best_candidate": meta.get("best_candidate"),
        "best_candidate_runtime": meta.get("best_candidate_runtime"),
        "best_metrics": meta.get("best_metrics"),
        "samples": meta.get("samples"),
        "shots": meta.get("shots"),
        "max_rounds": meta.get("max_rounds"),
        "repair_samples": meta.get("repair_samples"),
        "repair_retries": meta.get("repair_retries"),
        "rounds_completed": meta.get("rounds_completed"),
        "stop_reason": meta.get("stop_reason"),
        "round_feedback_count": meta.get("round_feedback_count"),
        "candidate_error_count": len(meta.get("candidate_errors") or []),
        "round_debug": _sanitize_round_debug(meta.get("round_debug") or []),
    }


def _sanitize_round_debug(round_debug: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in round_debug:
        candidates = item.get("candidate_debug") or []
        sanitized.append(
            {
                "round": item.get("round"),
                "best_candidate": item.get("best_candidate"),
                "best_quality": item.get("best_quality"),
                "best_metrics": item.get("best_metrics"),
                "round_error": item.get("round_error", ""),
                "candidate_count": len(candidates),
                "feedback_chars": len(item.get("feedback") or ""),
                "candidates": [
                    {
                        "candidate": candidate.get("candidate"),
                        "runtime": candidate.get("runtime"),
                        "metrics": candidate.get("metrics"),
                        "routing_chars": candidate.get("routing_chars"),
                        "error": candidate.get("error", ""),
                        "repair": candidate.get("repair", ""),
                    }
                    for candidate in candidates
                ],
            }
        )
    return sanitized


def _verified_candidate_quality(
    metrics: EvalMetrics,
    validation: dict[str, Any],
) -> tuple[int, int, int, int, int, int, int, float, int, int, float]:
    missing_count = len(validation.get("missing_expected_nets", []))
    unexpected_count = len(validation.get("unexpected_output_nets", []))
    expected_count = len(validation.get("expected_nets", []))
    output_count = len(validation.get("output_nets", []))
    net_valid = int(missing_count == 0 and unexpected_count == 0 and output_count == expected_count)
    repair_contract_ok = int(bool(validation.get("repair_contract_ok")))
    local_repair_improved = int(bool(validation.get("local_repair_improved")))
    active_delta = 0
    before = validation.get("active_issue_count_before")
    after = validation.get("active_issue_count_after")
    if isinstance(before, int) and isinstance(after, int):
        active_delta = before - after
    return (
        net_valid,
        -missing_count,
        -unexpected_count,
        int(metrics.success),
        -metrics.drc_violation,
        local_repair_improved,
        active_delta,
        metrics.drc_backend_score,
        repair_contract_ok,
        -metrics.via_count,
        -metrics.path_length,
    )


def _parallel_repair_acceptance_quality(
    metrics: EvalMetrics,
    payload: dict[str, Any],
) -> tuple[int, int, int, int, float, int, float]:
    active_delta = 0
    before = payload.get("active_issue_count_before")
    after = payload.get("active_issue_count_after")
    if isinstance(before, int) and isinstance(after, int):
        active_delta = before - after
    return (
        int(metrics.success),
        -metrics.drc_violation,
        int(bool(payload.get("total_repair_improved"))),
        int(bool(payload.get("local_repair_improved"))),
        active_delta,
        -metrics.via_count,
        -metrics.path_length,
    )


def _compact_drc_report(metrics: EvalMetrics) -> dict[str, Any]:
    detail = metrics.detail or {}
    drc_detail = detail.get("drc_detail", {}) or {}
    drc_inner = drc_detail.get("detail", {}) or {}
    fill_detail = detail.get("fill_detail", {}) or {}
    return {
        "metrics": _metrics_summary(metrics),
        "pipeline_final_score": detail.get("pipeline_final_score"),
        "semantic_score": detail.get("s1"),
        "drc_score": detail.get("s2"),
        "fill_success": fill_detail.get("success"),
        "fill_reason": (fill_detail.get("detail") or {}).get("reason"),
        "fill_error": fill_detail.get("error_message"),
        "drc_success": drc_detail.get("success"),
        "drc_error": drc_detail.get("error_message"),
        "hard_issue_count": drc_inner.get("hard_issue_count"),
        "hard_rule_counts": drc_inner.get("hard_rule_counts"),
        "issues": _trim_issues(drc_inner.get("issues", []), limit=40),
        "message_zh": drc_inner.get("message_zh"),
        "issue_kind_summary": _issue_kind_summary(drc_inner.get("issues", [])),
        "hard_penalty": drc_inner.get("hard_penalty"),
        "drc_pass": drc_inner.get("pass"),
    }


_DRC_RULE_PRIOR_CACHE: dict[str, Any] | None = None


def _load_drc_rule_prior() -> dict[str, Any]:
    global _DRC_RULE_PRIOR_CACHE
    if _DRC_RULE_PRIOR_CACHE is not None:
        return _DRC_RULE_PRIOR_CACHE

    prior_path = os.getenv("REROUTE_DRC_RULE_PRIOR")
    path = Path(prior_path) if prior_path else Path(__file__).with_name("drc_rule_prior.json")
    try:
        _DRC_RULE_PRIOR_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _DRC_RULE_PRIOR_CACHE = {}
    return _DRC_RULE_PRIOR_CACHE


def _compact_rule_prior_items(items: Any, limit: int = 3) -> List[str]:
    if not isinstance(items, list):
        return []
    compact: List[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            compact.append(text)
        if len(compact) >= limit:
            break
    return compact


def _build_llm_repair_messages(
    task: RoutingTask,
    routing_output: str,
    validation: dict[str, Any],
    report: dict[str, Any],
    repair_contract: dict[str, Any],
    repair_idx: int,
    repair_samples: int,
    previous_bad: str,
    previous_violations: List[str],
    skill_cards: List[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    concise_report = {
        "metrics": report.get("metrics"),
        "hard_rule_counts": report.get("hard_rule_counts"),
        "issue_kind_summary": report.get("issue_kind_summary"),
        "message_zh": report.get("message_zh"),
        "issues": (report.get("issues") or [])[:6],
    }
    contract_text = _format_repair_contract_text(repair_contract, repair_idx=repair_idx)
    adaptive_guidance = _build_issue_adaptive_guidance(report)
    crossing_nets = [
        str(item.get("net"))
        for item in repair_contract.get("crossing_targets") or []
        if item.get("net") is not None
    ]
    active_repair_nets = [str(net) for net in (repair_contract.get("active_repair_nets") or [])]
    via_requirement = ""
    if crossing_nets:
        via_requirement = (
            "硬性文本要求：最终答案中本次 active crossing target net 必须至少出现两行 "
            f"`(via ... (net N))`，其中 N 属于 {crossing_nets}。"
            "非 active nets 不要在本候选里额外加 via。\n"
        )
    retry_feedback = ""
    if previous_violations:
        retry_feedback = (
            "\n\n上一版 repair 候选没有通过自检，禁止重复它。\n"
            f"上一版输出：\n{previous_bad[:2500]}\n"
            "违反的硬约束：\n"
            + json.dumps(previous_violations, ensure_ascii=False, indent=2)
            + "\n请纠正这些问题，尤其不要再输出被禁止的 Top 直连。"
        )
    skill_card_text = _format_skill_cards(skill_cards or [])
    return [
        {
            "role": "system",
            "content": (
                "你是一个具备 DRC 工具反馈的 PCB self-evolution 布线智能体。"
                "你要像 PCB 布线 reviewer 一样先理解障碍物，再生成一版新的完整 KiCad 修复输出。"
                "可以内部推理，但最终必须给出 <answer>，不要留空答案。"
                "DRC 报告是验证反馈，不能修改已有非目标网络。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"任务 ID：{task.task_id}\n"
                f"缺失走线描述：\n{task.task_prompt}\n\n"
                f"可用信号层：{json.dumps(_available_signal_layers(task.context_kicad), ensure_ascii=False)}\n\n"
                f"上一版失败候选，禁止机械复读：\n{routing_output[:2500]}\n\n"
                f"DRC / 评测报告摘要：\n"
                f"{json.dumps(concise_report, ensure_ascii=False, indent=2)[:2500]}\n\n"
                "障碍物和修复合约：\n"
                f"{contract_text}\n"
                "规则先验来自 DRC verifier 的 hard-rule 判定逻辑；它只解释为什么失败和如何规避，"
                "不是可照抄的坐标生成器。\n"
                f"{adaptive_guidance}"
                f"{skill_card_text}"
                f"{retry_feedback}\n\n"
                f"{via_requirement}"
                "请执行一次 self-evolution 修复。你需要先理解："
                "如果 issue 明确是 crossing/clearance，直线失败通常是因为它穿过了 obstacle_segments，不是因为端点错；"
                "如果 issue 明确是 pad 未逃逸 / fanout 不足 / 未连接，就优先补目标 pad 的短引出、过孔和有效连接，"
                "不要把所有失败都机械当成交叉绕障。"
                "当 DRC 没有提供 segment endpoints 时，不要编造不存在的几何细节。"
                "然后直接输出修复后的 KiCad：\n"
                f"0a. 本次是单目标 repair，active_repair_nets={active_repair_nets}；"
                "只重写这些 active net 的绕障路径。其它 expected net 必须保留当前 base output 的路径，"
                "不要在本候选里顺手重布。\n"
                "0. 如果检索到了负向 skill card，必须优先避免其中的 bad_patterns、"
                "failed_rules、long_top_nets 和 repair_prompt_hint 指出的失败路线；"
                "不要把负向 card 当作可照抄的成功路径。\n"
                "1. 如果 HR_DRC_SEGMENT_CROSSING 涉及目标 net 和已有 net，"
                "必须理解为目标 net 穿过已有走线，只能改目标 net。\n"
                "2. 新输出必须包含所有 expected_nets，禁止输出 unexpected net。\n"
                "对于没有出现在 crossing_targets 中的 expected net，保持它自己的最短连接即可，"
                "不要把不同 net 的端点互相连接，也不要为它创建零长度 segment。\n"
                "3. 对 repair contract 中 active crossing_targets，禁止再次输出 forbidden_top_segments 列出的同层失败段；"
                "把 obstacle_segments 当成不可移动障碍物：目标线不能在同一 Top 层穿过这些障碍，"
                "如果障碍在 Top，需要在进入 crossing window 前切到内层或绕开该区域；"
                "如果障碍在内层，需要移动内层 bend/路径，不能继续穿过该内层障碍；"
                "repair contract 的 local inner obstacle layer 是从当前 PCB 上下文检索出的内层占用图，"
                "选择内层时必须优先选 recommended inner-layer order 靠前且 estimated_same_layer_obstacle_hits=0 的层；"
                "如果 scaffold 的内层 segment 会穿过 listed inner obstacle，必须换层或把 BEND 移到障碍物外侧，"
                "禁止把 Top 直线简单平移到被占用的内层走廊；"
                "Previous failed repair probes 是同题 verifier 探针失败记忆，必须明确避开其层、走廊和 bad route；"
                "内部规划时必须显式使用 repair contract 中的 obstacle_segments、forbidden_top_segments、"
                "obstacle_window/detour_window 来选择 VIA_A/BEND/VIA_B；"
                "VIA_A、BEND、VIA_B 应尽量位于 detour_window 外或沿窗口外侧绕行；"
                "active net 必须包含至少两个该 net 的 via，并至少包含一段非 Top/Bottom 信号层上的 segment。"
                "Top 层只能保留靠近焊盘的短 stub，主体绕行必须发生在内层；"
                "via 不能放在 DRC 报告的 crossing 点上。"
                "via 点和中间点由你根据 DRC 坐标、端点和可用层自行选择。\n"
                "4. 每个 segment/via 都必须端点连续、net id 正确、层名来自可用信号层。"
                "线宽优先沿用上下文常用细线宽 0.116840 或失败候选线宽。\n"
                f"5. 这是第 {repair_idx}/{repair_samples} 个独立修复候选，"
                "请和其它候选采用不同的绕行层或中间点假设。\n\n"
                "对 crossing target 的拓扑骨架如下，所有 VIA_A/VIA_B/BEND 坐标都必须替换成数字；"
                "遇到密集障碍时优先使用 BEND_A+BEND_B 的矩形绕行，而不是单 BEND 斜穿：\n"
                "(segment (start target_start_x target_start_y) (end VIA_A_X VIA_A_Y) ... (layer Top) (net target_net))\n"
                "(via (at VIA_A_X VIA_A_Y) ... (net target_net))\n"
                "(segment (start VIA_A_X VIA_A_Y) (end BEND_A_X BEND_A_Y) ... (layer 可用内层) (net target_net))\n"
                "(segment (start BEND_A_X BEND_A_Y) (end BEND_B_X BEND_B_Y) ... (layer 同一内层) (net target_net))\n"
                "(segment (start BEND_B_X BEND_B_Y) (end VIA_B_X VIA_B_Y) ... (layer 同一内层) (net target_net))\n"
                "(via (at VIA_B_X VIA_B_Y) ... (net target_net))\n"
                "(segment (start VIA_B_X VIA_B_Y) (end target_end_x target_end_y) ... (layer Top) (net target_net))\n"
                "VIA_A 应靠近 target_start 且位于 obstacle_window 外；"
                "VIA_B 应靠近 target_end 且位于 obstacle_window 外；"
                "BEND_A/BEND_B 用来让内层路径沿 obstacle_window 外侧绕行，而不是一条长直线穿过去。\n\n"
                "带 via 绕障的格式示例，示例坐标和 net 999 只是格式，禁止照抄：\n"
                "<answer>\n"
                "(segment (start 10.000000 10.000000) (end 11.000000 9.500000) (width 0.116840) (layer Top) (net 999))\n"
                "(via (at 11.000000 9.500000) (size 0.457200) (drill 0.203200) (layers Top Bottom) (net 999))\n"
                "(segment (start 11.000000 9.500000) (end 11.000000 14.000000) (width 0.116840) (layer ART03) (net 999))\n"
                "(segment (start 11.000000 14.000000) (end 8.500000 16.000000) (width 0.116840) (layer ART03) (net 999))\n"
                "(via (at 8.500000 16.000000) (size 0.457200) (drill 0.203200) (layers Top Bottom) (net 999))\n"
                "(segment (start 8.500000 16.000000) (end 8.000000 16.200000) (width 0.116840) (layer Top) (net 999))\n"
                "</answer>\n\n"
                "硬性输出格式：\n"
                "<answer>\n"
                "(segment (start ... ...) (end ... ...) (width ...) (layer ...) (net ...))\n"
                "(via (at ... ...) (size 0.457200) (drill 0.203200) (layers Top Bottom) (net ...))\n"
                "</answer>\n"
                "<answer> 内只允许 KiCad 的 (segment ...) 和 (via ...) 对象，"
                "不要 Markdown，不要自然语言。"
            ),
        },
    ]


def _format_skill_cards(skill_cards: List[dict[str, Any]]) -> str:
    if not skill_cards:
        return ""
    lines = [
        "\n检索到的 skill cards（仅用于本次 repair 参考，不要照抄坐标）：",
        "优先遵守负向 card：先找 bad_patterns/avoid/failed_rules，禁止重复其中的失败路线；"
        "再参考正向 card 的 route_pattern/prompt_hint。",
    ]
    for idx, card in enumerate(skill_cards, start=1):
        label = "正向" if card.get("polarity") == "positive" else "负向"
        text = card.get("card_text") or card.get("card") or ""
        lines.append(
            f"[{idx}] {label} card | rules={json.dumps(card.get('hard_rule_counts') or {}, ensure_ascii=False)}\n"
            f"{str(text)[:2200]}"
        )
    return "\n".join(lines) + "\n"


def _parse_json_object(text: str) -> dict[str, Any]:
    clean = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    start = clean.find("{")
    end = clean.rfind("}")
    if start >= 0 and end > start:
        clean = clean[start : end + 1]
    return json.loads(clean)


def _connection_length(conn: dict[str, Any]) -> float:
    start = conn.get("start")
    end = conn.get("end")
    if not (_is_point(start) and _is_point(end)):
        return 999.0
    return (
        (float(end[0]) - float(start[0])) ** 2
        + (float(end[1]) - float(start[1])) ** 2
    ) ** 0.5


def _default_occupancy_net_order(connections: List[dict[str, Any]]) -> List[str]:
    ordered = sorted(
        connections,
        key=lambda conn: (
            _connection_length(conn),
            str(conn.get("net")),
        ),
    )
    return [str(conn.get("net")) for conn in ordered if conn.get("net") is not None]


def _order_connections_from_plan(
    plan: dict[str, Any],
    connections: List[dict[str, Any]],
) -> List[dict[str, Any]]:
    by_net = {str(conn.get("net")): conn for conn in connections}
    ordered: List[dict[str, Any]] = []
    seen: set[str] = set()
    raw_order = plan.get("net_order") if isinstance(plan, dict) else []
    if not isinstance(raw_order, list):
        raw_order = []
    for item in raw_order:
        match = re.search(r"\d+", str(item))
        if not match:
            continue
        net = match.group(0)
        if net in by_net and net not in seen:
            ordered.append(by_net[net])
            seen.add(net)
    for net in _default_occupancy_net_order(connections):
        if net not in seen:
            ordered.append(by_net[net])
            seen.add(net)
    return ordered


def _build_global_occupancy_overview(
    task: RoutingTask,
    connections: List[dict[str, Any]],
) -> dict[str, Any]:
    signal_layers = _available_signal_layers(task.context_kicad)
    inner_layers = [
        layer for layer in signal_layers if layer.lower() not in {"top", "bottom"}
    ]
    net_items = []
    for conn in connections:
        target = {
            "net": conn.get("net"),
            "start": conn.get("start"),
            "end": conn.get("end"),
        }
        obstacle_prior = _public_obstacle_prior(
            _inner_layer_obstacle_prior(task.context_kicad, target, inner_layers)
        )
        net_items.append(
            {
                "net": conn.get("net"),
                "net_name": conn.get("net_name"),
                "start": conn.get("start"),
                "end": conn.get("end"),
                "straight_length": round(_connection_length(conn), 3),
                "nearby_context_obstacles": _context_obstacles_for_connection(
                    task,
                    conn,
                    margin=4.0,
                    limit=12,
                ),
                "recommended_inner_layers": obstacle_prior.get("recommended_inner_layers", []),
                "inner_layer_summaries": [
                    {
                        "layer": item.get("layer"),
                        "nearby_obstacle_count": item.get("nearby_obstacle_count"),
                        "direct_chord_hits": item.get("direct_chord_hits"),
                        "risk_score": item.get("risk_score"),
                    }
                    for item in obstacle_prior.get("layer_summaries", [])[:8]
                ],
            }
        )
    return {
        "available_signal_layers": signal_layers,
        "default_short_to_long_order": _default_occupancy_net_order(connections),
        "target_pair_chord_crossings_if_all_top": _target_pair_chord_crossings(connections),
        "nets": net_items,
    }


def _target_pair_chord_crossings(connections: List[dict[str, Any]]) -> List[dict[str, Any]]:
    crossings: List[dict[str, Any]] = []
    for idx, first in enumerate(connections):
        first_start = first.get("start")
        first_end = first.get("end")
        if not (_is_point(first_start) and _is_point(first_end)):
            continue
        first_seg = (
            (float(first_start[0]), float(first_start[1])),
            (float(first_end[0]), float(first_end[1])),
        )
        for second in connections[idx + 1 :]:
            second_start = second.get("start")
            second_end = second.get("end")
            if not (_is_point(second_start) and _is_point(second_end)):
                continue
            second_seg = (
                (float(second_start[0]), float(second_start[1])),
                (float(second_end[0]), float(second_end[1])),
            )
            if _segments_intersect(first_seg[0], first_seg[1], second_seg[0], second_seg[1]):
                crossings.append(
                    {
                        "net_a": first.get("net"),
                        "net_b": second.get("net"),
                        "meaning": (
                            "If both nets are routed as same-layer straight chords, "
                            "they may conflict; assign different layers/corridors."
                        ),
                    }
                )
    return crossings


def _build_occupied_map(
    task: RoutingTask,
    connection: dict[str, Any],
    routed_output: str,
    all_connections: List[dict[str, Any]],
    remaining_connections: List[dict[str, Any]],
) -> dict[str, Any]:
    signal_layers = _available_signal_layers(task.context_kicad)
    inner_layers = [
        layer for layer in signal_layers if layer.lower() not in {"top", "bottom"}
    ]
    target = {
        "net": connection.get("net"),
        "start": connection.get("start"),
        "end": connection.get("end"),
    }
    margin = float(os.getenv("REROUTE_OCCUPANCY_MARGIN", "5.0"))
    bbox = _target_corridor_bbox(target, margin=margin)
    context_obstacles = _context_obstacles_for_connection(
        task,
        connection,
        margin=margin,
        limit=_env_int("REROUTE_OCCUPANCY_CONTEXT_LIMIT", 28),
    )
    routed_obstacles = _routing_obstacles_for_connection(
        routed_output,
        connection,
        bbox=bbox,
        limit=_env_int("REROUTE_OCCUPANCY_ROUTED_LIMIT", 40),
    )
    obstacle_prior = _public_obstacle_prior(
        _inner_layer_obstacle_prior(task.context_kicad, target, inner_layers)
    )
    return {
        "current_net": connection,
        "corridor_bbox": bbox,
        "available_signal_layers": signal_layers,
        "recommended_inner_layers": obstacle_prior.get("recommended_inner_layers", inner_layers),
        "context_obstacles_near_corridor": context_obstacles,
        "already_routed_target_obstacles": routed_obstacles,
        "same_layer_obstacles": context_obstacles + routed_obstacles,
        "remaining_target_reservations": [
            {
                "net": item.get("net"),
                "net_name": item.get("net_name"),
                "start": item.get("start"),
                "end": item.get("end"),
                "straight_length": round(_connection_length(item), 3),
            }
            for item in remaining_connections
        ],
        "all_target_nets": [str(item.get("net")) for item in all_connections],
        "inner_layer_summaries": [
            {
                "layer": item.get("layer"),
                "nearby_obstacle_count": item.get("nearby_obstacle_count"),
                "direct_chord_hits": item.get("direct_chord_hits"),
                "risk_score": item.get("risk_score"),
                "obstacles": (item.get("obstacles") or [])[:8],
            }
            for item in obstacle_prior.get("layer_summaries", [])[:8]
        ],
    }


def _context_obstacles_for_connection(
    task: RoutingTask,
    connection: dict[str, Any],
    margin: float,
    limit: int,
) -> List[dict[str, Any]]:
    target = {
        "net": connection.get("net"),
        "start": connection.get("start"),
        "end": connection.get("end"),
    }
    bbox = _target_corridor_bbox(target, margin=margin)
    net_names = _net_name_map(task.context_kicad)
    target_net = str(connection.get("net"))
    start = connection.get("start")
    end = connection.get("end")
    chord = None
    if _is_point(start) and _is_point(end):
        chord = ((float(start[0]), float(start[1])), (float(end[0]), float(end[1])))
    records: List[dict[str, Any]] = []
    for segment in _extract_kicad_sexpressions(task.context_kicad, "segment"):
        record = _segment_record(segment)
        if not record or record["net"] == target_net:
            continue
        if not _segment_intersects_bbox(record["start"], record["end"], bbox):
            continue
        direct_hit = False
        distance = 999.0
        if chord:
            direct_hit = _segments_intersect(chord[0], chord[1], record["start"], record["end"])
            distance = min(
                _point_to_segment_distance(record["start"], chord[0], chord[1]),
                _point_to_segment_distance(record["end"], chord[0], chord[1]),
            )
        records.append(
            {
                "source": "fixed_context",
                "net": record["net"],
                "net_name": net_names.get(record["net"], f"net_{record['net']}"),
                "layer": record["layer"],
                "start": _round_point(record["start"]),
                "end": _round_point(record["end"]),
                "direct_chord_hit": direct_hit,
                "distance_to_target_chord": round(distance, 3),
            }
        )
    records.sort(
        key=lambda item: (
            not bool(item.get("direct_chord_hit")),
            float(item.get("distance_to_target_chord") or 999.0),
            str(item.get("layer")),
        )
    )
    return records[: max(0, limit)]


def _routing_obstacles_for_connection(
    routed_output: str,
    connection: dict[str, Any],
    bbox: dict[str, float],
    limit: int,
) -> List[dict[str, Any]]:
    target_net = str(connection.get("net"))
    records: List[dict[str, Any]] = []
    for segment in _extract_kicad_sexpressions(routed_output, "segment"):
        record = _segment_record(segment)
        if not record or record["net"] == target_net:
            continue
        if not _segment_intersects_bbox(record["start"], record["end"], bbox):
            continue
        records.append(
            {
                "source": "already_routed_target",
                "net": record["net"],
                "net_name": f"target_net_{record['net']}",
                "layer": record["layer"],
                "start": _round_point(record["start"]),
                "end": _round_point(record["end"]),
                "meaning": "previous target net route is fixed occupancy for the current net",
            }
        )
    return records[: max(0, limit)]


def _build_stateful_net_routing_messages(
    task: RoutingTask,
    connection: dict[str, Any],
    all_connections: List[dict[str, Any]],
    plan: dict[str, Any],
    occupied_map: dict[str, Any],
    routed_output: str,
    prior: str,
    step_idx: int,
    total_steps: int,
    candidate_idx: int,
    attempt_idx: int,
    previous_bad: str,
    previous_violations: List[str],
) -> list[dict[str, str]]:
    net = str(connection.get("net"))
    plan_for_net = (plan.get("net_plans") or {}).get(net, {}) if isinstance(plan, dict) else {}
    retry_text = ""
    if previous_violations:
        retry_text = (
            "\n\n上一版当前 net 的局部 occupied-map 自检失败，禁止重复：\n"
            f"{previous_bad[:2500]}\n"
            "失败点：\n"
            f"{json.dumps(previous_violations[:8], ensure_ascii=False, indent=2)}\n"
            "请只重写当前 net，换层、移动 bend/via 或改走 corridor，直到不再同层穿过 occupied segments。"
        )
    prior_text = f"\n\n参考先验（禁止照抄坐标）：\n{prior[:2500]}\n" if prior else ""
    return [
        {
            "role": "system",
            "content": (
                "你是 occupied-map aware 的 PCB 逐网布线智能体。"
                "每次只布一根目标 net；已经布好的目标 net 和 PCB 上下文中的其它 net 都是固定障碍。"
                "你必须根据 occupied map 决定是否使用 via/内层/矩形绕行。"
                "最终只输出当前 net 的 KiCad segment/via，不要输出自然语言。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"任务 ID：{task.task_id}\n"
                f"当前候选：{candidate_idx}，逐网步骤：{step_idx}/{total_steps}，当前 net 尝试：{attempt_idx}\n\n"
                f"全部目标连接：\n{json.dumps(all_connections, ensure_ascii=False, indent=2)}\n\n"
                f"本次只生成当前 net：\n{json.dumps(connection, ensure_ascii=False, indent=2)}\n\n"
                f"全局规划：\n{json.dumps(plan, ensure_ascii=False, indent=2)[:5000]}\n\n"
                f"当前 net 的计划：\n{json.dumps(plan_for_net, ensure_ascii=False, indent=2)}\n\n"
                f"occupied map（这些都是不可穿越的同层障碍）：\n"
                f"{json.dumps(occupied_map, ensure_ascii=False, indent=2)[:10000]}\n\n"
                f"已经完成的目标 net 输出，后续必须把它们当作新增障碍：\n"
                f"{_format_route_objects_for_prompt(routed_output, max_objects=36)}\n"
                f"{prior_text}"
                f"{retry_text}\n\n"
                "硬约束：\n"
                f"1. 只输出 net {net}，不要输出其它 target net 或 context net。\n"
                "2. segment/via 的 net id 必须完全等于当前 net；端点必须从当前 net 的 start 接到 end。\n"
                "3. 不能让当前 net 的 segment 在同一 layer 穿过 occupied map 里的任何不同 net segment；"
                "如果 Top 直线会穿越，必须用短 Top stub + via + 推荐内层 + bend/矩形绕行 + via + 短 Top stub。\n"
                "4. 如果当前 net 使用 Bottom/SIG/ART/In*.Cu 等非 Top 层，必须至少输出两个当前 net 的 via："
                "一个从 start 附近 Top stub 切入内层，一个在 end 附近切回 Top；"
                "禁止用一条 Bottom/SIG 长直线直接连接两个 Top pad 坐标。\n"
                "5. 对长度超过 5mm 或 occupied map 有 direct_chord_hit 的 net，禁止单 segment 直连；"
                "必须输出多段路径，并显式绕开 listed obstacles。\n"
                "6. 已经完成的目标 net 不能改、不能跨、不能复用它的坐标走同层重叠。\n"
                "7. via 只能用于当前 net，via 两侧必须有连续 segment；层名必须来自 available_signal_layers。\n\n"
                "输出格式：\n"
                "<answer>\n"
                "(segment (start ... ...) (end ... ...) (width 0.116840) (layer ...) (net ...))\n"
                "(via (at ... ...) (size 0.457200) (drill 0.203200) (layers Top Bottom) (net ...))\n"
                "</answer>"
            ),
        },
    ]


def _format_route_objects_for_prompt(routing_output: str, max_objects: int = 24) -> str:
    objects = _split_kicad_routing_objects(routing_output)
    if not objects:
        return "(none yet)"
    shown = objects[-max_objects:]
    prefix = "" if len(objects) <= max_objects else f"... omitted {len(objects) - max_objects} earlier objects ...\n"
    return prefix + "\n".join(shown)


def _split_kicad_routing_objects(routing_output: str) -> List[str]:
    pattern = re.compile(r"\(\s*(?:segment|via)\b", flags=re.IGNORECASE)
    objects: List[str] = []
    for match in pattern.finditer(routing_output):
        start = match.start()
        depth = 0
        for idx in range(start, len(routing_output)):
            char = routing_output[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    objects.append(routing_output[start : idx + 1].strip())
                    break
    return objects


def _only_objects_for_net(routing_output: str, net: str) -> str:
    return "\n".join(
        obj
        for obj in _split_kicad_routing_objects(routing_output)
        if re.search(rf"\(\s*net\s+{re.escape(net)}\s*\)", obj, flags=re.IGNORECASE)
    ).strip()


def _stateful_net_route_violations(
    task: RoutingTask,
    connection: dict[str, Any],
    net_output: str,
    routed_output: str,
    occupied_map: dict[str, Any],
) -> List[str]:
    net = str(connection.get("net"))
    violations: List[str] = []
    if not re.search(r"\(\s*(segment|via)\b", net_output, flags=re.IGNORECASE):
        return [f"net {net} produced no KiCad segment/via objects"]
    output_nets = _extract_output_nets(net_output)
    if output_nets != [net]:
        violations.append(f"current step must output only net {net}, got {output_nets}")
    records = []
    for segment in _extract_kicad_sexpressions(net_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        records.append(record)
        if _points_close(record["start"], record["end"]):
            violations.append(f"net {net} has zero-length segment {record['start']}->{record['end']}")
    if not records:
        violations.append(f"net {net} has no valid segment object")
    via_count = _count_objects_for_net(net_output, "via", net)
    via_points = _via_points_for_net(net_output, net)
    non_top_records = [
        record for record in records if record["layer"].lower() != "top"
    ]
    if non_top_records and via_count < 2:
        violations.append(
            f"net {net} uses non-Top layer(s) but has only {via_count} via(s); "
            "two vias are required to leave and re-enter Top pads"
        )
    if _connection_length(connection) > 5.0 and len(records) <= 1:
        violations.append(
            f"net {net} is a long multi-net route but used only {len(records)} segment(s); "
            "use stubs, vias, and at least one bend to avoid occupied routes"
        )
    start = connection.get("start")
    end = connection.get("end")
    if _is_point(start) and not _route_touches_point(records, (float(start[0]), float(start[1]))):
        violations.append(f"net {net} does not touch required start point {start}")
    if _is_point(end) and not _route_touches_point(records, (float(end[0]), float(end[1]))):
        violations.append(f"net {net} does not touch required end point {end}")
    top_pad_points = []
    if _is_point(start):
        top_pad_points.append((float(start[0]), float(start[1])))
    if _is_point(end):
        top_pad_points.append((float(end[0]), float(end[1])))
    for record in non_top_records:
        for point in top_pad_points:
            if _points_close(record["start"], point, tol=0.08) or _points_close(record["end"], point, tol=0.08):
                violations.append(
                    f"net {net} non-Top segment {record['layer']} touches Top pad coordinate {point}; "
                    "use a Top stub plus a via at the transition instead"
                )
                break
    if non_top_records and via_points:
        inner_endpoints = [
            point
            for record in non_top_records
            for point in (record["start"], record["end"])
        ]
        via_anchored = [
            point
            for point in inner_endpoints
            if any(_points_close(point, via_point, tol=0.08) for via_point in via_points)
        ]
        if len(via_anchored) < 2:
            violations.append(
                f"net {net} inner route has insufficient via-anchored transition endpoints; "
                f"matched {len(via_anchored)}"
            )

    obstacles = occupied_map.get("same_layer_obstacles") or []
    if (
        _connection_length(connection) > 5.0
        and via_count == 0
        and any(bool(item.get("direct_chord_hit")) for item in obstacles)
    ):
        violations.append(
            f"net {net} has direct chord hits in occupied map but used no vias; "
            "choose a different layer/corridor with via transitions"
        )
    for record in records:
        if record["net"] != net:
            continue
        for obstacle in obstacles:
            if str(obstacle.get("net")) == net:
                continue
            if str(obstacle.get("layer") or "").lower() != record["layer"].lower():
                continue
            obs_start = obstacle.get("start")
            obs_end = obstacle.get("end")
            if not (_is_point(obs_start) and _is_point(obs_end)):
                continue
            obstacle_segment = (
                (float(obs_start[0]), float(obs_start[1])),
                (float(obs_end[0]), float(obs_end[1])),
            )
            if _segments_intersect(record["start"], record["end"], obstacle_segment[0], obstacle_segment[1]):
                violations.append(
                    "same-layer occupied crossing: "
                    f"net {net} {record['layer']} {record['start']}->{record['end']} "
                    f"crosses {obstacle.get('source')} net {obstacle.get('net')} "
                    f"{obstacle.get('start')}->{obstacle.get('end')}"
                )
                if len(violations) >= 8:
                    return violations
    routed_nets = set(_extract_output_nets(routed_output))
    if net in routed_nets:
        violations.append(f"net {net} was already routed earlier; do not duplicate it")
    return violations


def _route_touches_point(records: List[dict[str, Any]], point: tuple[float, float]) -> bool:
    return any(
        _points_close(record["start"], point, tol=0.08)
        or _points_close(record["end"], point, tol=0.08)
        for record in records
    )


def _occupied_map_debug_summary(occupied_map: dict[str, Any]) -> dict[str, Any]:
    if not occupied_map:
        return {}
    return {
        "corridor_bbox": occupied_map.get("corridor_bbox"),
        "context_obstacle_count": len(occupied_map.get("context_obstacles_near_corridor") or []),
        "already_routed_obstacle_count": len(occupied_map.get("already_routed_target_obstacles") or []),
        "recommended_inner_layers": occupied_map.get("recommended_inner_layers"),
        "remaining_target_count": len(occupied_map.get("remaining_target_reservations") or []),
    }


def _skill_retrieval_query(
    task: RoutingTask,
    repair_contract: dict[str, Any],
    report: dict[str, Any],
) -> str:
    return "\n".join(
        [
            task.task_id,
            task.task_prompt,
            json.dumps(repair_contract.get("expected_connections") or [], ensure_ascii=False),
            json.dumps(repair_contract.get("active_repair_nets") or [], ensure_ascii=False),
            json.dumps(repair_contract.get("crossing_targets") or [], ensure_ascii=False),
            json.dumps(repair_contract.get("inactive_crossing_targets") or [], ensure_ascii=False),
            json.dumps(report.get("hard_rule_counts") or {}, ensure_ascii=False),
            json.dumps((report.get("issues") or [])[:6], ensure_ascii=False),
        ]
    )


def _retrieval_tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(
        re.findall(
            r"hr_[a-z0-9_]+|net_[a-z0-9_]+|art\d+|top|bottom|"
            r"\d+\.\d+|\d+|[a-z][a-z0-9_+-]{1,}",
            lowered,
        )
    )
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,6}", text))
    return tokens


def _route_structure_summary(routing_output: str) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for net in _extract_output_nets(routing_output):
        summary[net] = {
            "segment_count": 0,
            "layers": {},
            "vias": _via_points_for_net(routing_output, net)[:6],
            "top_segment_lengths": [
                round(length, 3) for length in _top_segment_lengths_for_net(routing_output, net)
            ][:6],
        }
    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        item = summary.setdefault(
            record["net"],
            {"segment_count": 0, "layers": {}, "vias": [], "top_segment_lengths": []},
        )
        item["segment_count"] += 1
        layer_info = item["layers"].setdefault(record["layer"], [])
        if len(layer_info) < 6:
            layer_info.append(
                {
                    "start": [round(record["start"][0], 3), round(record["start"][1], 3)],
                    "end": [round(record["end"][0], 3), round(record["end"][1], 3)],
                }
            )
    return summary


def _build_llm_repair_contract(
    task: RoutingTask,
    routing_output: str,
    metrics: EvalMetrics,
) -> dict[str, Any]:
    connections = _extract_expected_connections(task.task_prompt)
    report = _compact_drc_report(metrics)
    issues = report.get("issues") or []
    signal_layers = _available_signal_layers(task.context_kicad)
    inner_layers = [
        layer for layer in signal_layers if layer.lower() not in {"top", "bottom"}
    ]
    crossing_targets: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if issue.get("rule") != "HR_DRC_SEGMENT_CROSSING":
            continue
        issue_layer = str(issue.get("layer") or "Top")
        text = " ".join(
            str(issue.get(key) or "")
            for key in ("message", "net", "obj1", "obj2")
        )
        extra = issue.get("extra") or {}
        for conn in connections:
            net_name = conn["net_name"]
            if net_name not in text and net_name not in str(extra):
                continue
            item = crossing_targets.setdefault(
                conn["net"],
                {
                    "net": conn["net"],
                    "net_name": net_name,
                    "start": conn["start"],
                    "end": conn["end"],
                    "crossing_points": [],
                    "obstacle_segments": [],
                    "forbidden_top_segments": [],
                    "required_structure": [
                            "do_not_modify_existing_non_target_nets",
                            "no_single_top_direct_segment_between_failed_segment_endpoints",
                            "at_least_two_vias_for_this_target_net",
                            "at_least_one_segment_on_a_non_top_bottom_signal_layer",
                            "top_layer_segments_for_this_target_net_must_be_short_endpoint_stubs",
                            "do_not_place_vias_on_reported_crossing_points",
                        ],
                        "max_top_stub_length": 3.0,
                    },
                )
            item["crossing_points"].append(
                {
                    "x": issue.get("x"),
                    "y": issue.get("y"),
                    "existing_net_pair": issue.get("net"),
                    "message": issue.get("message"),
                }
            )
            for idx in ("1", "2"):
                seg_net = extra.get(f"seg{idx}_net")
                if seg_net and seg_net != net_name:
                    item["obstacle_segments"].append(
                        {
                            "net_name": seg_net,
                            "start": extra.get(f"seg{idx}_start"),
                            "end": extra.get(f"seg{idx}_end"),
                            "layer": issue_layer,
                            "meaning": (
                                "existing non-target obstacle; do not modify it or cross it "
                                f"on {issue_layer}"
                            ),
                        }
                    )
            for idx in ("1", "2"):
                if extra.get(f"seg{idx}_net") == net_name:
                    item["forbidden_top_segments"].append(
                        {
                            "start": extra.get(f"seg{idx}_start"),
                            "end": extra.get(f"seg{idx}_end"),
                            "layer": issue_layer,
                        }
                    )
    for item in crossing_targets.values():
        points = [
            (point.get("x"), point.get("y"))
            for point in item.get("crossing_points", [])
            if isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        ]
        if points:
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            margin = 0.8
            item["obstacle_window"] = {
                "x_min": min(xs),
                "x_max": max(xs),
                "y_min": min(ys),
                "y_max": max(ys),
                "detour_window": {
                    "x_min": round(min(xs) - margin, 3),
                    "x_max": round(max(xs) + margin, 3),
                    "y_min": round(min(ys) - margin, 3),
                    "y_max": round(max(ys) + margin, 3),
                    "meaning": (
                        "plan VIA_A/BEND/VIA_B outside or around this expanded keepout; "
                        "do not run the main target segment through it"
                    ),
                },
                "meaning": (
                    "target net should route around this obstacle window; "
                    "if the window is on Top, leave Top before it and return after it; "
                    "if it is on an inner layer, move the bend/inner path away from it"
                ),
            }
        obstacle_prior_full = _inner_layer_obstacle_prior(task.context_kicad, item, inner_layers)
        obstacle_prior = _public_obstacle_prior(obstacle_prior_full)
        if obstacle_prior:
            item["inner_layer_obstacle_prior"] = obstacle_prior
            recommended_layers = obstacle_prior.get("recommended_inner_layers") or []
            if recommended_layers:
                item["recommended_inner_layers"] = recommended_layers
        hint_layers = item.get("recommended_inner_layers") or inner_layers
        item["waypoint_strategy_hints"] = _rank_waypoint_strategy_hints(
            _waypoint_strategy_hints(item, hint_layers),
            obstacle_prior_full,
        )
        item["required_topology_template"] = [
            "segment: target_start -> via_A on Top; short endpoint stub only",
            "via: via_A for target net",
            "segment: via_A -> bend_A on a preferred inner layer",
            "segment: bend_A -> via_B on the same inner layer; route around obstacle_window",
            "via: via_B for target net",
            "segment: via_B -> target_end on Top; short endpoint stub only",
        ]
    return {
        "expected_connections": connections,
        "available_signal_layers": signal_layers,
        "preferred_inner_layers_for_crossing": inner_layers,
        "crossing_targets": list(crossing_targets.values()),
        "active_repair_nets": [],
        "rule_guidance": _build_rule_guidance(connections, issues),
        "general": [
            "Only output missing target nets from expected_connections.",
            "Do not output existing non-target nets.",
            "If a target net appears in crossing_targets, route that target net with vias and an inner signal layer.",
        ],
    }


def _activate_repair_target(
    repair_contract: dict[str, Any],
    repair_idx: int,
) -> dict[str, Any]:
    """Select one crossing target for this repair candidate.

    This keeps repair LLM work local: fix one target net, preserve the other
    expected nets from the current base, then let verifier feedback decide the
    next target.
    """
    contract = json.loads(json.dumps(repair_contract, ensure_ascii=False))
    ranked = sorted(
        contract.get("crossing_targets") or [],
        key=lambda item: (
            len(item.get("crossing_points") or []),
            len(item.get("obstacle_segments") or []),
            str(item.get("net")),
        ),
        reverse=True,
    )
    if not ranked:
        contract["active_repair_nets"] = []
        contract["inactive_crossing_targets"] = []
        return contract

    index = min(max(repair_idx, 1), len(ranked)) - 1
    if len(ranked) > 1:
        attempts = contract.get("target_repair_attempts") or {}
        for offset in range(len(ranked)):
            candidate_index = (index + offset) % len(ranked)
            candidate = ranked[candidate_index]
            net = str(candidate.get("net"))
            before = len(candidate.get("crossing_points") or [])
            stats = attempts.get(net) or {}
            if (
                int(stats.get("attempts") or 0) > 0
                and int(stats.get("best_after") or before) >= before
            ):
                continue
            index = candidate_index
            break
    active = ranked[index]
    active_net = str(active.get("net"))
    contract["crossing_targets"] = [active]
    contract["active_repair_nets"] = [active_net]
    contract["inactive_crossing_targets"] = [
        {
            "net": item.get("net"),
            "net_name": item.get("net_name"),
            "crossing_count": len(item.get("crossing_points") or []),
        }
        for item in ranked
        if str(item.get("net")) != active_net
    ]
    contract["repair_decomposition"] = {
        "mode": "single_target_net",
        "repair_idx": repair_idx,
        "active_net": active_net,
        "ranked_targets": [
            {
                "net": item.get("net"),
                "net_name": item.get("net_name"),
                "crossing_count": len(item.get("crossing_points") or []),
            }
            for item in ranked
        ],
    }
    return contract


def _repair_contract_debug_summary(repair_contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "repair_decomposition": repair_contract.get("repair_decomposition"),
        "active_repair_nets": repair_contract.get("active_repair_nets") or [],
        "inactive_crossing_targets": repair_contract.get("inactive_crossing_targets") or [],
        "crossing_targets": [
            {
                "net": item.get("net"),
                "net_name": item.get("net_name"),
                "crossing_count": len(item.get("crossing_points") or []),
                "obstacle_count": len(item.get("obstacle_segments") or []),
                "layer": (
                    (item.get("obstacle_segments") or [{}])[0].get("layer")
                    if item.get("obstacle_segments")
                    else None
                ),
                "recommended_inner_layers": item.get("recommended_inner_layers"),
                "inner_layer_obstacle_prior": {
                    "recommended_inner_layers": (
                        (item.get("inner_layer_obstacle_prior") or {}).get(
                            "recommended_inner_layers"
                        )
                    ),
                    "layer_summaries": [
                        {
                            "layer": layer_item.get("layer"),
                            "nearby_obstacle_count": layer_item.get("nearby_obstacle_count"),
                            "direct_chord_hits": layer_item.get("direct_chord_hits"),
                            "risk_score": layer_item.get("risk_score"),
                        }
                        for layer_item in (
                            (item.get("inner_layer_obstacle_prior") or {}).get(
                                "layer_summaries"
                            )
                            or []
                        )
                    ],
                },
            }
            for item in repair_contract.get("crossing_targets") or []
        ],
    }


def _active_crossing_issue_count(
    report: dict[str, Any],
    repair_contract: dict[str, Any],
) -> int:
    active_nets = {str(net) for net in (repair_contract.get("active_repair_nets") or [])}
    if not active_nets:
        return int((report.get("hard_rule_counts") or {}).get("HR_DRC_SEGMENT_CROSSING", 0) or 0)
    expected = {
        str(conn.get("net")): str(conn.get("net_name"))
        for conn in repair_contract.get("expected_connections") or []
    }
    active_names = {expected.get(net, net) for net in active_nets}
    count = 0
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict) or issue.get("rule") != "HR_DRC_SEGMENT_CROSSING":
            continue
        text = json.dumps(issue, ensure_ascii=False)
        if any(name and name in text for name in active_names):
            count += 1
    return count


def _relevant_failed_repair_memory(
    memory: List[dict[str, Any]],
    repair_contract: dict[str, Any],
    limit: int = 3,
) -> List[dict[str, Any]]:
    if not memory or limit <= 0:
        return []
    active = {str(net) for net in repair_contract.get("active_repair_nets") or []}
    selected: List[dict[str, Any]] = []
    for item in reversed(memory):
        item_active = {str(net) for net in item.get("active_repair_nets") or []}
        if active and item_active and not (active & item_active):
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _failed_repair_memory_item(
    routing_output: str,
    repair_contract: dict[str, Any],
    violations: List[str],
    probe_report: dict[str, Any] | None = None,
    metrics: EvalMetrics | None = None,
    active_issue_count_before: int | None = None,
    active_issue_count_after: int | None = None,
) -> dict[str, Any]:
    active_nets = [str(net) for net in repair_contract.get("active_repair_nets") or []]
    route_summary = _route_structure_summary(routing_output)
    layers_by_net = {
        net: sorted((route_summary.get(net) or {}).get("layers", {}).keys())
        for net in active_nets
    }
    failed_rules: List[str] = []
    if probe_report:
        failed_rules = sorted((probe_report.get("hard_rule_counts") or {}).keys())
        if not failed_rules:
            failed_rules = sorted(
                {
                    str(issue.get("rule"))
                    for issue in probe_report.get("issues") or []
                    if isinstance(issue, dict) and issue.get("rule")
                }
            )
    active_segments: dict[str, Any] = {}
    for net in active_nets:
        active_segments[net] = (route_summary.get(net) or {}).get("layers", {})
    return {
        "active_repair_nets": active_nets,
        "drc_violation": metrics.drc_violation if metrics else None,
        "active_issue_count_before": active_issue_count_before,
        "active_issue_count_after": active_issue_count_after,
        "failed_rules": failed_rules[:6],
        "violations": violations[:6],
        "layers_by_net": layers_by_net,
        "active_route_segments": active_segments,
        "avoid_hint": _failed_repair_avoid_hint(active_nets, layers_by_net, violations, probe_report),
    }


def _repair_attempt_stats(memory: List[dict[str, Any]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for item in memory:
        before = item.get("active_issue_count_before")
        after = item.get("active_issue_count_after")
        for net in item.get("active_repair_nets") or []:
            net_id = str(net)
            entry = stats.setdefault(
                net_id,
                {"attempts": 0, "best_after": 10**9, "best_before": 0},
            )
            entry["attempts"] += 1
            if isinstance(before, int):
                entry["best_before"] = max(entry["best_before"], before)
            if isinstance(after, int):
                entry["best_after"] = min(entry["best_after"], after)
    return stats


def _failed_repair_avoid_hint(
    active_nets: List[str],
    layers_by_net: dict[str, List[str]],
    violations: List[str],
    probe_report: dict[str, Any] | None,
) -> str:
    layer_text = ", ".join(
        f"net {net} layers {layers}" for net, layers in layers_by_net.items()
    )
    issue_text = ""
    if probe_report:
        issues = []
        for issue in (probe_report.get("issues") or [])[:4]:
            if not isinstance(issue, dict):
                continue
            issues.append(
                f"{issue.get('rule')} {issue.get('net')} on {issue.get('layer')} "
                f"at ({issue.get('x')},{issue.get('y')})"
            )
        issue_text = "; ".join(issues)
    violation_text = "; ".join(violations[:3])
    return (
        f"avoid repeating active nets {active_nets}; {layer_text}; "
        f"violations={violation_text}; drc_issues={issue_text}"
    )


def _waypoint_strategy_hints(
    target: dict[str, Any],
    inner_layers: List[str],
) -> List[dict[str, Any]]:
    start = target.get("start")
    end = target.get("end")
    if not (_is_point(start) and _is_point(end)):
        return []
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    dx = ex - sx
    dy = ey - sy
    x_dir = 1.0 if dx >= 0 else -1.0
    y_dir = 1.0 if dy >= 0 else -1.0
    layers = inner_layers or ["ART03", "ART04"]
    span_y = abs(dy)
    base_via_a = [round(sx - x_dir * 1.5, 3), round(sy - y_dir * 0.5, 3)]
    base_via_b = [round(ex + x_dir * 0.6, 3), round(ey + y_dir * 0.1, 3)]
    base_bend = [
        base_via_a[0],
        round(sy + y_dir * min(max(span_y * 0.46, 3.0), max(span_y - 1.0, 3.0)), 3),
    ]

    hints: List[dict[str, Any]] = []
    for layer_idx, layer in enumerate(layers[:4], start=1):
        hints.append(
            _waypoint_hint(
                f"layer_{layer_idx}_endpoint_escape",
                layer,
                base_via_a,
                base_bend,
                base_via_b,
            )
        )
        for side_name, sign in (("below", -1.0), ("above", 1.0)):
            window = target.get("obstacle_window") or {}
            if sign > 0 and isinstance(window.get("y_max"), (int, float)):
                detour_y = float(window["y_max"]) + 1.2
            elif sign < 0 and isinstance(window.get("y_min"), (int, float)):
                detour_y = float(window["y_min"]) - 1.2
            else:
                detour_y = sy + sign * 1.8
            rect_via_a = [round(sx, 3), round(sy + sign * 0.7, 3)]
            rect_via_b = [round(ex, 3), round(ey + sign * 0.7, 3)]
            rect_bend_a = [rect_via_a[0], round(detour_y, 3)]
            rect_bend_b = [rect_via_b[0], round(detour_y, 3)]
            hints.append(
                _waypoint_hint(
                    f"layer_{layer_idx}_rectangular_{side_name}_detour",
                    layer,
                    rect_via_a,
                    rect_bend_a,
                    rect_via_b,
                    inner_path_points=[rect_via_a, rect_bend_a, rect_bend_b, rect_via_b],
                )
            )

    window = target.get("obstacle_window") or {}
    if isinstance(window.get("y_max"), (int, float)) and isinstance(window.get("y_min"), (int, float)):
        around_y = (
            float(window["y_max"]) + 1.0
            if y_dir >= 0
            else float(window["y_min"]) - 1.0
        )
    else:
        around_y = sy + y_dir * min(max(span_y * 0.65, 3.5), max(span_y - 0.5, 3.5))
    wide_via_a = [round(sx - x_dir * 2.0, 3), round(sy - y_dir * 0.7, 3)]
    wide_via_b = [round(ex + x_dir * 0.8, 3), round(ey + y_dir * 0.2, 3)]
    for layer_idx, layer in enumerate(layers[:4], start=1):
        hints.append(
            _waypoint_hint(
                f"layer_{layer_idx}_wide_bend_around_obstacle_window",
                layer,
                wide_via_a,
                [wide_via_a[0], round(around_y, 3)],
                wide_via_b,
            )
        )
    if len(hints) > 16:
        hints = hints[:16]
    return hints


def _waypoint_hint(
    name: str,
    layer: str,
    via_a: List[float],
    bend: List[float],
    via_b: List[float],
    inner_path_points: List[List[float]] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "purpose": "obstacle-aware topology hint; LLM should still output valid KiCad",
        "inner_layer": layer,
        "via_A_near_start_outside_obstacle": via_a,
        "bend_on_inner_layer_to_go_around_obstacle": bend,
        "via_B_near_end_outside_obstacle": via_b,
        "inner_path_points": inner_path_points or [via_a, bend, via_b],
    }


def _inner_layer_obstacle_prior(
    context_kicad: str,
    target: dict[str, Any],
    inner_layers: List[str],
) -> dict[str, Any]:
    if not inner_layers:
        return {}
    start = target.get("start")
    end = target.get("end")
    if not (_is_point(start) and _is_point(end)):
        return {}
    margin = float(os.getenv("REROUTE_OBSTACLE_MARGIN", "3.0"))
    bbox = _target_corridor_bbox(target, margin=margin)
    net_names = _net_name_map(context_kicad)
    target_net = str(target.get("net"))
    start_point = (float(start[0]), float(start[1]))
    end_point = (float(end[0]), float(end[1]))
    layer_summaries: List[dict[str, Any]] = []
    layer_order = {layer.lower(): idx for idx, layer in enumerate(inner_layers)}

    records: List[dict[str, Any]] = []
    for segment in _extract_kicad_sexpressions(context_kicad, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        if record["net"] == target_net:
            continue
        if record["layer"].lower() not in layer_order:
            continue
        if not _segment_intersects_bbox(record["start"], record["end"], bbox):
            continue
        records.append(record)

    for layer in inner_layers:
        layer_records = [
            record for record in records if record["layer"].lower() == layer.lower()
        ]
        obstacles: List[dict[str, Any]] = []
        direct_hits = 0
        for record in layer_records:
            hit = _segments_intersect(start_point, end_point, record["start"], record["end"])
            if hit:
                direct_hits += 1
            distance = min(
                _point_to_segment_distance(record["start"], start_point, end_point),
                _point_to_segment_distance(record["end"], start_point, end_point),
            )
            obstacles.append(
                {
                    "net": record["net"],
                    "net_name": net_names.get(record["net"], f"net_{record['net']}"),
                    "start": _round_point(record["start"]),
                    "end": _round_point(record["end"]),
                    "layer": record["layer"],
                    "direct_chord_hit": hit,
                    "distance_to_target_chord": round(distance, 3),
                }
            )
        obstacles.sort(
            key=lambda item: (
                not bool(item.get("direct_chord_hit")),
                float(item.get("distance_to_target_chord") or 999.0),
            )
        )
        risk_score = direct_hits * 5 + len(layer_records)
        layer_summaries.append(
            {
                "layer": layer,
                "nearby_obstacle_count": len(layer_records),
                "direct_chord_hits": direct_hits,
                "risk_score": risk_score,
                "obstacles": obstacles[:12],
                "_ranking_obstacles": obstacles,
            }
        )

    ranked = sorted(
        layer_summaries,
        key=lambda item: (
            int(item.get("risk_score") or 0),
            int(item.get("nearby_obstacle_count") or 0),
            layer_order.get(str(item.get("layer")).lower(), 999),
        ),
    )
    return {
        "corridor_bbox": bbox,
        "recommended_inner_layers": [str(item["layer"]) for item in ranked],
        "layer_summaries": layer_summaries,
    }


def _public_obstacle_prior(obstacle_prior: dict[str, Any] | None) -> dict[str, Any]:
    if not obstacle_prior:
        return {}
    public = dict(obstacle_prior)
    public_summaries: List[dict[str, Any]] = []
    for item in obstacle_prior.get("layer_summaries") or []:
        clean = dict(item)
        clean.pop("_ranking_obstacles", None)
        public_summaries.append(clean)
    public["layer_summaries"] = public_summaries
    return public


def _rank_waypoint_strategy_hints(
    hints: List[dict[str, Any]],
    obstacle_prior: dict[str, Any] | None,
) -> List[dict[str, Any]]:
    if not hints:
        return []
    obstacle_prior = obstacle_prior or {}
    summaries = {
        str(item.get("layer")).lower(): item
        for item in obstacle_prior.get("layer_summaries") or []
    }
    ranked: List[dict[str, Any]] = []
    for hint in hints:
        layer = str(hint.get("inner_layer") or "").lower()
        summary = summaries.get(layer, {})
        obstacles = summary.get("_ranking_obstacles") or summary.get("obstacles") or []
        via_a = hint.get("via_A_near_start_outside_obstacle")
        bend = hint.get("bend_on_inner_layer_to_go_around_obstacle")
        via_b = hint.get("via_B_near_end_outside_obstacle")
        inner_path_points = hint.get("inner_path_points") or [via_a, bend, via_b]
        same_layer_hits = 0
        if all(_is_point(point) for point in inner_path_points):
            numeric_points = [
                (float(point[0]), float(point[1])) for point in inner_path_points
            ]
            candidate_segments = list(zip(numeric_points, numeric_points[1:]))
            for obstacle in obstacles:
                obs_start = obstacle.get("start")
                obs_end = obstacle.get("end")
                if not (_is_point(obs_start) and _is_point(obs_end)):
                    continue
                obstacle_segment = (
                    (float(obs_start[0]), float(obs_start[1])),
                    (float(obs_end[0]), float(obs_end[1])),
                )
                if any(
                    _segments_intersect(seg_start, seg_end, *obstacle_segment)
                    for seg_start, seg_end in candidate_segments
                ):
                    same_layer_hits += 1
        item = dict(hint)
        item["estimated_same_layer_obstacle_hits"] = same_layer_hits
        item["layer_risk_score"] = int(summary.get("risk_score") or 0)
        item["layer_nearby_obstacle_count"] = int(summary.get("nearby_obstacle_count") or 0)
        ranked.append(item)
    ranked.sort(
        key=lambda item: (
            int(item.get("estimated_same_layer_obstacle_hits") or 0),
            int(item.get("layer_risk_score") or 0),
            int(item.get("layer_nearby_obstacle_count") or 0),
        )
    )
    return ranked


def _build_rule_guidance(
    connections: List[dict[str, Any]],
    issues: List[dict[str, Any]],
) -> List[dict[str, Any]]:
    guidance: List[dict[str, Any]] = []
    expected_names = {item["net_name"]: item for item in connections}
    rule_priors = (_load_drc_rule_prior().get("rules") or {})
    for issue in issues[:12]:
        if not isinstance(issue, dict):
            continue
        rule = issue.get("rule")
        prior = rule_priors.get(rule or "", {}) if isinstance(rule_priors, dict) else {}
        text = f"{issue.get('message') or ''} {issue.get('net') or ''}"
        matched = [
            conn
            for name, conn in expected_names.items()
            if name in text or str(conn["net"]) in text
        ]
        action = "Inspect this DRC issue and move only target nets; do not modify existing non-target nets."
        if isinstance(prior, dict) and prior.get("prompt_hint"):
            action = str(prior["prompt_hint"])
        elif rule == "HR_CONNECT_PAD_NOT_ESCAPED":
            action = (
                "The target pad is still not escaped. Ensure the relevant expected net has a segment "
                "touching the exact pad coordinate from expected_connections, then continue toward the other endpoint."
            )
        elif rule == "HR_DRC_SEGMENT_CROSSING":
            action = (
                "Treat the non-target segment as a fixed obstacle. Reroute the target net around it, "
                "using layer change and bend points when same-layer straight routing crosses the obstacle."
            )
        elif rule and any(keyword in rule for keyword in ("CLEARANCE", "SHORT", "COLLISION")):
            action = (
                "Treat the reported coordinate/object as a keepout. Preserve the expected endpoint pads, "
                "then move only the target route away with a bend or layer change until clearance is restored."
            )
        elif rule and any(keyword in rule for keyword in ("UNCONNECTED", "NOT_CONNECTED", "OPEN")):
            action = (
                "The target net is not electrically continuous. Keep the exact expected net id and pad endpoints, "
                "then add continuous segments and required vias so every layer transition is connected."
            )
        elif rule and any(keyword in rule for keyword in ("VIA", "HOLE", "DRILL")):
            action = (
                "The via or drill placement is invalid. Move the target via away from the reported coordinate/object "
                "while keeping it on the same target net and connected to both adjacent segments."
            )
        elif rule:
            action = (
                "Preserve target net ids and endpoints, then move the target segment/via away from the reported "
                "coordinate or object until the hard-rule issue disappears."
            )
        extra = issue.get("extra") if isinstance(issue.get("extra"), dict) else {}
        guidance.append(
            {
                "rule": rule,
                "message": issue.get("message"),
                "layer": issue.get("layer"),
                "x": issue.get("x"),
                "y": issue.get("y"),
                "target_connections": matched,
                "action": action,
                "rule_weight": prior.get("weight") if isinstance(prior, dict) else None,
                "checker": prior.get("checker") if isinstance(prior, dict) else None,
                "verifier_trigger": prior.get("trigger") if isinstance(prior, dict) else None,
                "repair_prior": _compact_rule_prior_items(
                    prior.get("repair_prior") if isinstance(prior, dict) else [],
                    limit=4,
                ),
                "avoid_patterns": _compact_rule_prior_items(
                    prior.get("avoid_patterns") if isinstance(prior, dict) else [],
                    limit=3,
                ),
                "issue_extra": {
                    key: extra.get(key)
                    for key in (
                        "pad_id",
                        "component",
                        "seed_point",
                        "seg1_net",
                        "seg2_net",
                        "seg1_start",
                        "seg1_end",
                        "seg2_start",
                        "seg2_end",
                    )
                    if key in extra
                },
            }
        )
    return guidance


def _format_repair_contract_text(
    repair_contract: dict[str, Any],
    repair_idx: int = 1,
) -> str:
    lines: List[str] = []
    crossing_net_ids = {
        str(item.get("net"))
        for item in repair_contract.get("crossing_targets") or []
        if item.get("net") is not None
    }
    inactive_crossing_net_ids = {
        str(item.get("net"))
        for item in repair_contract.get("inactive_crossing_targets") or []
        if item.get("net") is not None
    }
    active_nets = [str(net) for net in (repair_contract.get("active_repair_nets") or [])]
    if active_nets:
        lines.append(
            "Repair decomposition: single-target mode. "
            f"Only actively reroute net(s) {active_nets} in this candidate; "
            "preserve all other expected nets from the current base output."
        )
    inactive = repair_contract.get("inactive_crossing_targets") or []
    if inactive:
        lines.append(
            "Inactive crossing targets for this candidate: "
            + json.dumps(inactive[:8], ensure_ascii=False, separators=(",", ":"))
        )
    lines.append("Expected nets:")
    for conn in repair_contract.get("expected_connections") or []:
        lines.append(
            f"- net {conn.get('net')} {conn.get('net_name')}: "
            f"{conn.get('start')} -> {conn.get('end')}"
        )
        net_id = str(conn.get("net"))
        if net_id in inactive_crossing_net_ids:
            lines.append(
                "  inactive in this repair candidate: keep its current base route; "
                "do not spend this candidate rewriting it."
            )
        elif net_id not in crossing_net_ids:
            lines.append(
                f"  keep this non-crossing net as one Top segment: "
                f"{conn.get('start')} -> {conn.get('end')} net {conn.get('net')}"
            )
    lines.append(
        "Non-active expected nets: preserve the current base route or their own shortest connection; "
        "do not add vias for them in this candidate unless they are the active repair net."
    )
    inner_layers = repair_contract.get("preferred_inner_layers_for_crossing") or []
    if inner_layers:
        lines.append(f"Preferred inner layers for obstacle avoidance: {inner_layers}")
    rule_guidance = repair_contract.get("rule_guidance") or []
    if rule_guidance:
        lines.append("DRC rule prior and issue-guided actions:")
        for item in rule_guidance[:6]:
            weight = item.get("rule_weight")
            weight_text = f", weight={weight}" if weight is not None else ""
            lines.append(
                f"- {item.get('rule')}{weight_text} on {item.get('layer')} "
                f"at ({item.get('x')}, {item.get('y')}): "
                f"{item.get('action')}"
            )
            if item.get("verifier_trigger"):
                lines.append(f"  verifier trigger: {item.get('verifier_trigger')}")
            if item.get("repair_prior"):
                lines.append("  repair prior: " + "; ".join(item.get("repair_prior") or []))
            if item.get("avoid_patterns"):
                lines.append("  avoid: " + "; ".join(item.get("avoid_patterns") or []))
            if item.get("issue_extra"):
                lines.append(
                    "  issue extra: "
                    + json.dumps(item.get("issue_extra"), ensure_ascii=False, separators=(",", ":"))
                )
    for target in repair_contract.get("crossing_targets") or []:
        net = target.get("net")
        lines.append(f"Crossing target net {net} {target.get('net_name')}:")
        lines.append(f"- endpoints: {target.get('start')} -> {target.get('end')}")
        obstacle_prior = target.get("inner_layer_obstacle_prior") or {}
        recommended_layers = target.get("recommended_inner_layers") or obstacle_prior.get("recommended_inner_layers")
        if recommended_layers:
            lines.append(
                "- recommended inner-layer order from local obstacle map: "
                + json.dumps(recommended_layers, ensure_ascii=False)
            )
        for layer_item in (obstacle_prior.get("layer_summaries") or [])[:6]:
            lines.append(
                "- local inner obstacle layer "
                f"{layer_item.get('layer')}: nearby={layer_item.get('nearby_obstacle_count')}, "
                f"direct_chord_hits={layer_item.get('direct_chord_hits')}, "
                f"risk={layer_item.get('risk_score')}. "
                "Treat listed same-layer segments as fixed keepouts."
            )
            for obstacle in (layer_item.get("obstacles") or [])[:5]:
                lines.append(
                    "  inner obstacle: "
                    f"net {obstacle.get('net')} {obstacle.get('net_name')} "
                    f"{obstacle.get('start')} -> {obstacle.get('end')} "
                    f"on {obstacle.get('layer')}"
                )
        previous_failed = repair_contract.get("previous_failed_repairs") or []
        if previous_failed:
            lines.append("Previous failed repair probes for this task/active net; do not repeat:")
            for failed in previous_failed[:3]:
                lines.append(
                    "- failed route: "
                    f"active={failed.get('active_repair_nets')}, "
                    f"layers={failed.get('layers_by_net')}, "
                    f"drc={failed.get('drc_violation')}, "
                    f"rules={failed.get('failed_rules')}, "
                    f"avoid={failed.get('avoid_hint')}"
                )
        window = target.get("obstacle_window")
        if window:
            lines.append(
                "- obstacle_window: "
                f"x=[{window.get('x_min')}, {window.get('x_max')}], "
                f"y=[{window.get('y_min')}, {window.get('y_max')}]"
            )
            detour = window.get("detour_window") or {}
            if detour:
                lines.append(
                    "- detour_window expanded keepout: "
                    f"x=[{detour.get('x_min')}, {detour.get('x_max')}], "
                    f"y=[{detour.get('y_min')}, {detour.get('y_max')}]; "
                    "place VIA_A/BEND/VIA_B outside this box when possible"
                )
        hints = target.get("waypoint_strategy_hints") or []
        if hints:
            hint = hints[0]
            inner_layer = hint.get("inner_layer") or (repair_contract.get("preferred_inner_layers_for_crossing") or ["ART03"])[0]
            start = target.get("start")
            end = target.get("end")
            via_a = hint.get("via_A_near_start_outside_obstacle")
            bend = hint.get("bend_on_inner_layer_to_go_around_obstacle")
            via_b = hint.get("via_B_near_end_outside_obstacle")
            inner_path_points = hint.get("inner_path_points") or [via_a, bend, via_b]
            lines.append(
                "- selected waypoint strategy for this active net: ranked safest strategy; "
                f"VIA_A near {hint.get('via_A_near_start_outside_obstacle')}, "
                f"BEND near {hint.get('bend_on_inner_layer_to_go_around_obstacle')}, "
                f"VIA_B near {hint.get('via_B_near_end_outside_obstacle')}; "
                f"inner_layer={inner_layer}, estimated_same_layer_obstacle_hits="
                f"{hint.get('estimated_same_layer_obstacle_hits', 0)}; "
                f"inner_path_points={inner_path_points}; "
                "use numeric coordinates like these only after checking the obstacle prior"
            )
            if (
                _is_point(start)
                and _is_point(end)
                and _is_point(via_a)
                and _is_point(via_b)
                and all(_is_point(point) for point in inner_path_points)
            ):
                width = "0.116840"
                inner_segments = "\n".join(
                    f"  (segment (start {_point_text(seg_start)}) (end {_point_text(seg_end)}) "
                    f"(width {width}) (layer {inner_layer}) (net {net}))"
                    for seg_start, seg_end in zip(inner_path_points, inner_path_points[1:])
                )
                lines.append(
                    "- topology scaffold for this net; before copying, check it against the local obstacle map and move BEND/layer if needed:\n"
                    f"  (segment (start {_point_text(start)}) (end {_point_text(via_a)}) (width {width}) (layer Top) (net {net}))\n"
                    f"  (via (at {_point_text(via_a)}) (size 0.457200) (drill 0.203200) (layers Top Bottom) (net {net}))\n"
                    f"{inner_segments}\n"
                    f"  (via (at {_point_text(via_b)}) (size 0.457200) (drill 0.203200) (layers Top Bottom) (net {net}))\n"
                    f"  (segment (start {_point_text(via_b)}) (end {_point_text(end)}) (width {width}) (layer Top) (net {net}))"
                )
        for obstacle in target.get("obstacle_segments") or []:
            lines.append(
                "- obstacle segment: "
                f"{obstacle.get('net_name')} {obstacle.get('start')} -> {obstacle.get('end')} "
                f"on {obstacle.get('layer')} (do not modify or cross on same layer)"
            )
        for forbidden in target.get("forbidden_top_segments") or []:
            lines.append(
                "- forbidden failed segment: "
                f"{forbidden.get('start')} -> {forbidden.get('end')} on {forbidden.get('layer')}"
            )
        lines.append(
            f"- mandatory topology for net {net}: Top short stub from start to VIA_A; "
            f"via for net {net}; inner-layer segment to BEND; inner-layer segment to VIA_B; "
            f"via for net {net}; Top short stub from VIA_B to end."
        )
        lines.append(
            f"- net {net} must have at least two `(via ...)` lines, at least one inner-layer segment, "
            "no Top segment longer than 3.0, and every layer transition endpoint must have a matching via line."
        )
    return "\n".join(lines)


def _repair_contract_violations(
    task: RoutingTask,
    routing_output: str,
    repair_contract: dict[str, Any],
) -> List[str]:
    violations: List[str] = []
    validation = _route_validation(task, routing_output)
    if validation["missing_expected_nets"]:
        violations.append(f"missing expected nets: {validation['missing_expected_nets']}")
    if validation["unexpected_output_nets"]:
        violations.append(f"unexpected output nets: {validation['unexpected_output_nets']}")

    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        sx, sy = record["start"]
        ex, ey = record["end"]
        if ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5 <= 1e-6:
            violations.append(f"zero-length segment is not allowed: {segment}")

    inner_layers = set(repair_contract.get("preferred_inner_layers_for_crossing") or [])
    for target in repair_contract.get("crossing_targets") or []:
        net = str(target.get("net"))
        via_count = _count_objects_for_net(routing_output, "via", net)
        if via_count < 2:
            violations.append(f"net {net} must use at least 2 vias, got {via_count}")
        if inner_layers and not _has_segment_on_layers(routing_output, net, inner_layers):
            violations.append(f"net {net} must include a segment on one of {sorted(inner_layers)}")
        if inner_layers:
            inner_endpoints = _inner_segment_endpoints_for_net(routing_output, net, inner_layers)
            via_points = _via_points_for_net(routing_output, net)
            anchored_endpoints = {
                (round(point[0], 4), round(point[1], 4))
                for point in inner_endpoints
                if any(_points_close(point, via_point, tol=0.05) for via_point in via_points)
            }
            if inner_endpoints and len(anchored_endpoints) < 2:
                violations.append(
                    f"net {net} inner-layer route must have vias at both transition endpoints; "
                    f"matched {len(anchored_endpoints)} via-anchored endpoints"
                )
        max_top_stub_length = float(target.get("max_top_stub_length") or 3.0)
        for length in _top_segment_lengths_for_net(routing_output, net):
            if length > max_top_stub_length:
                violations.append(
                    f"net {net} Top-layer segment length {length:.3f} exceeds "
                    f"short-stub limit {max_top_stub_length:.3f}"
                )
        crossing_points = [
            (float(item["x"]), float(item["y"]))
            for item in target.get("crossing_points") or []
            if isinstance(item, dict)
            and isinstance(item.get("x"), (int, float))
            and isinstance(item.get("y"), (int, float))
        ]
        for via_point in _via_points_for_net(routing_output, net):
            if any(_points_close(via_point, crossing, tol=0.05) for crossing in crossing_points):
                violations.append(f"net {net} via {via_point} is placed on a reported crossing point")
        for forbidden in target.get("forbidden_top_segments") or []:
            start = forbidden.get("start")
            end = forbidden.get("end")
            layer = str(forbidden.get("layer") or "Top")
            if _has_segment_between(routing_output, net, layer, start, end):
                violations.append(
                    f"net {net} repeats forbidden {layer} segment from {start} to {end}"
                )
        obstacle_prior = target.get("inner_layer_obstacle_prior") or {}
        obstacles_by_layer = {
            str(item.get("layer")).lower(): item.get("obstacles") or []
            for item in obstacle_prior.get("layer_summaries") or []
        }
        if obstacles_by_layer:
            for segment in _extract_kicad_sexpressions(routing_output, "segment"):
                record = _segment_record(segment)
                if not record or record["net"] != net:
                    continue
                layer_obstacles = obstacles_by_layer.get(record["layer"].lower()) or []
                for obstacle in layer_obstacles:
                    obs_start = obstacle.get("start")
                    obs_end = obstacle.get("end")
                    if not (_is_point(obs_start) and _is_point(obs_end)):
                        continue
                    if _segments_intersect(
                        record["start"],
                        record["end"],
                        (float(obs_start[0]), float(obs_start[1])),
                        (float(obs_end[0]), float(obs_end[1])),
                    ):
                        violations.append(
                            f"net {net} {record['layer']} segment {record['start']}->{record['end']} "
                            f"crosses local inner obstacle net {obstacle.get('net')} "
                            f"{obstacle.get('start')}->{obstacle.get('end')}"
                        )
    return violations


def _count_objects_for_net(routing_output: str, keyword: str, net: str) -> int:
    count = 0
    for obj in _extract_kicad_sexpressions(routing_output, keyword):
        if re.search(rf"\(\s*net\s+{re.escape(net)}\s*\)", obj, flags=re.IGNORECASE):
            count += 1
    return count


def _normalize_repair_syntax(routing_output: str) -> str:
    return re.sub(r"\(\s*iv\b", "(via", routing_output, flags=re.IGNORECASE)


def _insert_missing_layer_transition_vias(routing_output: str) -> str:
    records: List[dict[str, Any]] = []
    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if record:
            records.append(record)
    if not records:
        return routing_output

    existing_vias: dict[str, List[tuple[float, float]]] = {}
    endpoints: dict[tuple[str, float, float], dict[str, Any]] = {}
    for record in records:
        net = record["net"]
        for point in (record["start"], record["end"]):
            key = (net, round(point[0], 4), round(point[1], 4))
            item = endpoints.setdefault(
                key,
                {"net": net, "point": point, "layers": set()},
            )
            item["layers"].add(record["layer"])
    for net in {record["net"] for record in records}:
        existing_vias[net] = _via_points_for_net(routing_output, net)

    additions: List[str] = []
    for item in endpoints.values():
        layers = {str(layer).lower() for layer in item["layers"]}
        if len(layers) < 2:
            continue
        point = item["point"]
        net = item["net"]
        if any(_points_close(point, via_point, tol=0.05) for via_point in existing_vias.get(net, [])):
            continue
        additions.append(
            f"(via (at {point[0]:.6f} {point[1]:.6f}) "
            f"(size 0.457200) (drill 0.203200) (layers Top Bottom) (net {net}))"
        )
        existing_vias.setdefault(net, []).append(point)
    if not additions:
        return routing_output
    return "\n".join([routing_output.strip(), *additions]).strip()


def _merge_missing_expected_nets_from_base(
    task: RoutingTask,
    base_output: str,
    repaired_output: str,
) -> str:
    validation = _route_validation(task, repaired_output)
    if not validation["missing_expected_nets"]:
        return repaired_output
    additions: List[str] = []
    for net in validation["missing_expected_nets"]:
        additions.extend(_objects_for_net(base_output, net))
    if not additions:
        return repaired_output
    return "\n".join([repaired_output.strip(), *additions]).strip()


def _objects_for_net(routing_output: str, net: str) -> List[str]:
    objects: List[str] = []
    for keyword in ("segment", "via"):
        for obj in _extract_kicad_sexpressions(routing_output, keyword):
            if re.search(rf"\(\s*net\s+{re.escape(net)}\s*\)", obj, flags=re.IGNORECASE):
                objects.append(obj)
    return objects


def _has_segment_on_layers(
    routing_output: str,
    net: str,
    layers: set[str],
) -> bool:
    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        if record["net"] == net and record["layer"] in layers:
            return True
    return False


def _has_segment_between(
    routing_output: str,
    net: str,
    layer: str,
    start: Any,
    end: Any,
) -> bool:
    if not _is_point(start) or not _is_point(end):
        return False
    target_start = (float(start[0]), float(start[1]))
    target_end = (float(end[0]), float(end[1]))
    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        if record["net"] != net or record["layer"].lower() != layer.lower():
            continue
        seg_start = record["start"]
        seg_end = record["end"]
        if (
            _points_close(seg_start, target_start)
            and _points_close(seg_end, target_end)
        ) or (
            _points_close(seg_start, target_end)
            and _points_close(seg_end, target_start)
        ):
            return True
    return False


def _top_segment_lengths_for_net(routing_output: str, net: str) -> List[float]:
    lengths: List[float] = []
    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        if record["net"] != net or record["layer"].lower() != "top":
            continue
        sx, sy = record["start"]
        ex, ey = record["end"]
        lengths.append(((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5)
    return lengths


def _inner_segment_endpoints_for_net(
    routing_output: str,
    net: str,
    layers: set[str],
) -> List[tuple[float, float]]:
    endpoints: List[tuple[float, float]] = []
    for segment in _extract_kicad_sexpressions(routing_output, "segment"):
        record = _segment_record(segment)
        if not record:
            continue
        if record["net"] == net and record["layer"] in layers:
            endpoints.extend([record["start"], record["end"]])
    return endpoints


def _via_points_for_net(routing_output: str, net: str) -> List[tuple[float, float]]:
    points: List[tuple[float, float]] = []
    for via in _extract_kicad_sexpressions(routing_output, "via"):
        if not re.search(rf"\(\s*net\s+{re.escape(net)}\s*\)", via, flags=re.IGNORECASE):
            continue
        match = re.search(
            r"\(\s*at\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)",
            via,
            flags=re.IGNORECASE,
        )
        if match:
            points.append((float(match.group(1)), float(match.group(2))))
    return points


def _extract_kicad_sexpressions(text: str, keyword: str) -> List[str]:
    pattern = re.compile(r"\(\s*" + re.escape(keyword) + r"\b", flags=re.IGNORECASE)
    objects: List[str] = []
    for match in pattern.finditer(text):
        start = match.start()
        depth = 0
        for idx in range(start, len(text)):
            char = text[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    objects.append(text[start : idx + 1])
                    break
    return objects


def _segment_record(segment: str) -> dict[str, Any] | None:
    start = re.search(
        r"\(\s*start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)",
        segment,
        flags=re.IGNORECASE,
    )
    end = re.search(
        r"\(\s*end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)",
        segment,
        flags=re.IGNORECASE,
    )
    layer = re.search(r"\(\s*layer\s+([A-Za-z0-9_.+-]+)\s*\)", segment, flags=re.IGNORECASE)
    net = re.search(r"\(\s*net\s+(\d+)\s*\)", segment, flags=re.IGNORECASE)
    if not (start and end and layer and net):
        return None
    return {
        "start": (float(start.group(1)), float(start.group(2))),
        "end": (float(end.group(1)), float(end.group(2))),
        "layer": layer.group(1),
        "net": net.group(1),
    }


def _is_point(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, (int, float)) for item in value)
    )


def _point_text(value: Any) -> str:
    if not _is_point(value):
        return "0.000000 0.000000"
    return f"{float(value[0]):.6f} {float(value[1]):.6f}"


def _points_close(
    point_a: tuple[float, float],
    point_b: tuple[float, float],
    tol: float = 1e-4,
) -> bool:
    return abs(point_a[0] - point_b[0]) <= tol and abs(point_a[1] - point_b[1]) <= tol


def _round_point(point: tuple[float, float]) -> List[float]:
    return [round(float(point[0]), 6), round(float(point[1]), 6)]


def _net_name_map(context_kicad: str) -> dict[str, str]:
    names: dict[str, str] = {}
    quoted = re.findall(
        r"\(\s*net\s+(\d+)\s+\"([^\"]+)\"\s*\)",
        context_kicad,
        flags=re.IGNORECASE,
    )
    for net, name in quoted:
        names[net] = name
    bare = re.findall(
        r"\(\s*net\s+(\d+)\s+([A-Za-z0-9_.:+\-]+)\s*\)",
        context_kicad,
        flags=re.IGNORECASE,
    )
    for net, name in bare:
        names.setdefault(net, name)
    return names


def _target_corridor_bbox(target: dict[str, Any], margin: float = 3.0) -> dict[str, float]:
    points: List[tuple[float, float]] = []
    for key in ("start", "end"):
        point = target.get(key)
        if _is_point(point):
            points.append((float(point[0]), float(point[1])))
    for crossing in target.get("crossing_points") or []:
        if not isinstance(crossing, dict):
            continue
        x = crossing.get("x")
        y = crossing.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            points.append((float(x), float(y)))
    if not points:
        return {"x_min": 0.0, "x_max": 0.0, "y_min": 0.0, "y_max": 0.0}
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "x_min": round(min(xs) - margin, 3),
        "x_max": round(max(xs) + margin, 3),
        "y_min": round(min(ys) - margin, 3),
        "y_max": round(max(ys) + margin, 3),
    }


def _segment_intersects_bbox(
    start: tuple[float, float],
    end: tuple[float, float],
    bbox: dict[str, float],
) -> bool:
    x_min = float(bbox.get("x_min", 0.0))
    x_max = float(bbox.get("x_max", 0.0))
    y_min = float(bbox.get("y_min", 0.0))
    y_max = float(bbox.get("y_max", 0.0))
    if (
        max(start[0], end[0]) < x_min
        or min(start[0], end[0]) > x_max
        or max(start[1], end[1]) < y_min
        or min(start[1], end[1]) > y_max
    ):
        return False
    if (
        x_min <= start[0] <= x_max
        and y_min <= start[1] <= y_max
        or x_min <= end[0] <= x_max
        and y_min <= end[1] <= y_max
    ):
        return True
    corners = [
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
    ]
    edges = list(zip(corners, corners[1:] + corners[:1]))
    return any(_segments_intersect(start, end, edge_start, edge_end) for edge_start, edge_end in edges)


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
    tol: float = 1e-6,
) -> bool:
    def orientation(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> float:
        return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

    def on_segment(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> bool:
        return (
            min(p[0], r[0]) - tol <= q[0] <= max(p[0], r[0]) + tol
            and min(p[1], r[1]) - tol <= q[1] <= max(p[1], r[1]) + tol
        )

    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    if o1 * o2 < -tol and o3 * o4 < -tol:
        return True
    if abs(o1) <= tol and on_segment(a, c, b):
        return True
    if abs(o2) <= tol and on_segment(a, d, b):
        return True
    if abs(o3) <= tol and on_segment(c, a, d):
        return True
    if abs(o4) <= tol and on_segment(c, b, d):
        return True
    return False


def _point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    px, py = point
    sx, sy = start
    ex, ey = end
    dx = ex - sx
    dy = ey - sy
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return ((px - sx) ** 2 + (py - sy) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / denom))
    proj_x = sx + t * dx
    proj_y = sy + t * dy
    return ((px - proj_x) ** 2 + (py - proj_y) ** 2) ** 0.5


def _available_signal_layers(context_kicad: str) -> List[str]:
    layers: List[str] = []
    for _, name in re.findall(r"\(\s*(\d+)\s+([A-Za-z0-9_.+-]+)\s+signal\s*\)", context_kicad):
        if name not in layers:
            layers.append(name)
    return layers or ["Top", "Bottom"]


def _route_validation(task: RoutingTask, routing_output: str) -> dict[str, Any]:
    expected_nets = _extract_expected_nets(task.task_prompt)
    output_nets = _extract_output_nets(routing_output)
    return {
        "expected_nets": expected_nets,
        "output_nets": output_nets,
        "missing_expected_nets": [net for net in expected_nets if net not in output_nets],
        "unexpected_output_nets": [net for net in output_nets if net not in expected_nets],
        "expected_connections": _extract_expected_connections(task.task_prompt),
    }


def _extract_expected_nets(task_prompt: str) -> List[str]:
    return sorted(set(re.findall(r"网络\s+(\d+)\s*\(", task_prompt)))


def _extract_output_nets(routing_output: str) -> List[str]:
    return sorted(set(re.findall(r"\(\s*net\s+(\d+)\s*\)", routing_output, flags=re.IGNORECASE)))


def _extract_expected_connections(task_prompt: str) -> List[dict[str, Any]]:
    layered_pattern = re.compile(
        r"网络\s+(\d+)\(([^)]+)\).*?"
        r"位于层\s*([A-Za-z0-9_.+-]+)\s*，\s*坐标\s*\(([-+]?\d+(?:\.\d+)?),\s*([-+]?\d+(?:\.\d+)?)\).*?"
        r"位于层\s*([A-Za-z0-9_.+-]+)\s*，\s*坐标\s*\(([-+]?\d+(?:\.\d+)?),\s*([-+]?\d+(?:\.\d+)?)\)",
        flags=re.DOTALL,
    )
    connections: List[dict[str, Any]] = []
    for net, net_name, start_layer, sx, sy, end_layer, ex, ey in layered_pattern.findall(task_prompt):
        connections.append(
            {
                "net": net,
                "net_name": net_name,
                "start": [float(sx), float(sy)],
                "start_layer": start_layer,
                "end": [float(ex), float(ey)],
                "end_layer": end_layer,
            }
        )
    if connections:
        return connections

    pattern = re.compile(
        r"网络\s+(\d+)\(([^)]+)\).*?"
        r"坐标\s*\(([-+]?\d+(?:\.\d+)?),\s*([-+]?\d+(?:\.\d+)?)\).*?"
        r"坐标\s*\(([-+]?\d+(?:\.\d+)?),\s*([-+]?\d+(?:\.\d+)?)\)",
        flags=re.DOTALL,
    )
    for net, net_name, sx, sy, ex, ey in pattern.findall(task_prompt):
        connections.append(
            {
                "net": net,
                "net_name": net_name,
                "start": [float(sx), float(sy)],
                "end": [float(ex), float(ey)],
            }
        )
    return connections


def _trim_issues(issues: Any, limit: int = 12) -> List[dict[str, Any]]:
    if not isinstance(issues, list):
        return []
    trimmed: List[dict[str, Any]] = []
    for item in issues[:limit]:
        if not isinstance(item, dict):
            continue
        trimmed.append(
            {
                "rule": item.get("rule"),
                "rule_name_zh": item.get("rule_name_zh"),
                "severity": item.get("severity"),
                "message": item.get("message"),
                "message_zh": item.get("message_zh"),
                "description": item.get("description"),
                "net": item.get("net"),
                "layer": item.get("layer"),
                "x": item.get("x"),
                "y": item.get("y"),
                "obj1": item.get("obj1"),
                "obj2": item.get("obj2"),
                "component": item.get("component"),
                "pad_id": item.get("pad_id"),
                "suggestion": item.get("suggestion"),
                "suggestion_zh": item.get("suggestion_zh"),
                "location_zh": item.get("location_zh"),
                "extra": item.get("extra"),
            }
        )
    return trimmed


def _issue_text_blob(issue: dict[str, Any]) -> str:
    parts = [
        issue.get("rule"),
        issue.get("rule_name_zh"),
        issue.get("message"),
        issue.get("message_zh"),
        issue.get("description"),
        issue.get("suggestion"),
        issue.get("suggestion_zh"),
        issue.get("location_zh"),
    ]
    return " ".join(str(part).strip() for part in parts if part).lower()


def _issue_kind(issue: dict[str, Any]) -> str:
    text = _issue_text_blob(issue)
    if any(token in text for token in ["escape", "fanout", "未逃逸", "逃逸", "bga pad", "pad not escaped"]):
        return "fanout_escape"
    if any(token in text for token in ["unconnected", "not connected", "open", "未连接", "悬空"]):
        return "connectivity"
    if any(token in text for token in ["short", "短路"]):
        return "short"
    if any(token in text for token in ["clearance", "spacing", "crossing", "segment crossing", "间距", "相交", "碰撞"]):
        return "crossing_clearance"
    if any(token in text for token in ["via", "过孔"]):
        return "via"
    return "generic"


def _issue_kind_summary(issues: Any) -> dict[str, int]:
    if not isinstance(issues, list):
        return {}
    counts: dict[str, int] = {}
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        kind = _issue_kind(issue)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _build_issue_adaptive_guidance(report: dict[str, Any]) -> str:
    summary = report.get("issue_kind_summary") or {}
    if not isinstance(summary, dict) or not summary:
        return ""
    lines = ["当前 DRC issue 类型提示："]
    if summary.get("fanout_escape"):
        lines.append(
            "- 若问题是 pad 未逃逸 / fanout 不足：优先在对应 pad 附近短引出，再就近打 via 或接入有效逃逸路径，不要只在远端改主干。"
        )
    if summary.get("connectivity"):
        lines.append(
            "- 若问题是未连接 / 开路：优先补齐目标 net 的真实连通性，检查起终点是否被完整接上，而不是只做避障形状微调。"
        )
    if summary.get("crossing_clearance"):
        lines.append(
            "- 若问题是 crossing / clearance：把已有走线当障碍物，仅改目标 net，优先使用短 Top stub + via + 内层绕行。"
        )
    if summary.get("short"):
        lines.append(
            "- 若问题是短路：下一轮必须明确隔离冲突对象，不要让目标 net 接触非目标 net 或错误 pad。"
        )
    if summary.get("via"):
        lines.append(
            "- 若问题与 via 相关：检查 via 是否放在有效层、有效 net 上，并避免把 via 放到冲突热点正中心。"
        )
    return "\n".join(lines) + "\n"
