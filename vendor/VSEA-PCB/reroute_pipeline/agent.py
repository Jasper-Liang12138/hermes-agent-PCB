from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from .config import RerouteConfig
from .drc_adapter import AgentHardDRCAdapter
from .fill_adapter import FillAdapter
from .schemas import RerouteInput, RerouteOutput
from .vsea_core.pipeline_wrapper import PCBPipelineWrapper
from .vsea_core.router import VSEARerouteRouter, sanitize_router_meta
from .vsea_core.utils import (
    EvalMetrics,
    RoutingTask,
    count_vias,
    estimate_path_length,
    extract_kicad_objects,
)


@contextmanager
def _patched_env(values: Dict[str, str]) -> Iterator[None]:
    previous: Dict[str, str | None] = {}
    for key, value in values.items():
        previous[key] = os.environ.get(key)
        if value:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _AgentEvaluator:
    """Minimal evaluator used by the VSEA router during candidate selection."""

    def __init__(self, fill_adapter: FillAdapter, drc_adapter: AgentHardDRCAdapter, scratch_dir: Path, target_bga: str):
        self.fill_adapter = fill_adapter
        self.drc_adapter = drc_adapter
        self.ai_pcb_eval_path = fill_adapter.ai_pcb_eval_path
        self.scratch_dir = scratch_dir
        self.target_bga = target_bga
        self._counter = 0

    def evaluate_with_drc(
        self,
        routing_output: str,
        task: RoutingTask,
        method: str,
        runtime: float,
        output_path: str = "",
    ) -> EvalMetrics:
        self._counter += 1
        stem = f"{task.task_id}_{self._counter:04d}"
        filled_path = self.scratch_dir / "probe_filled" / f"{stem}.kicad_pcb"
        report_path = self.scratch_dir / "probe_drc" / f"{stem}.json"
        fill = self.fill_adapter.fill(task, routing_output, filled_path)
        path_length = estimate_path_length(routing_output)
        via_count = count_vias(routing_output)
        if not fill.success:
            return EvalMetrics(
                method=method,
                task_id=task.task_id,
                score=-10.0 - 0.1 * path_length - 2.0 * via_count,
                drc_violation=1,
                path_length=path_length,
                via_count=via_count,
                runtime=runtime,
                success=False,
                drc_backend_score=0.0,
                status="fill_failed",
                output_path=output_path,
                detail={
                    "fill_detail": {
                        "success": False,
                        "filled_board_path": "",
                        "error_message": fill.error,
                        "detail": fill.detail,
                    },
                    "drc_detail": {"success": False, "detail": {"hard_issue_count": 1, "issues": []}},
                    "error_message": fill.error,
                },
            )
        drc = self.drc_adapter.run(fill.completed_kicad_path, report_path, target_bga=self.target_bga)
        drc_violation = max(0, int(drc.hard_issue_count))
        score = -10.0 * drc_violation - 0.1 * path_length - 2.0 * via_count
        return EvalMetrics(
            method=method,
            task_id=task.task_id,
            score=score,
            drc_violation=drc_violation,
            path_length=path_length,
            via_count=via_count,
            runtime=runtime,
            success=drc.success and drc_violation == 0,
            drc_backend_score=1.0 if drc.success else 0.0,
            status="ok" if drc.success else "drc_failed",
            output_path=output_path,
            detail={
                "pipeline_final_score": None,
                "s1": fill.semantic_score,
                "s2": 1.0 if drc.success else 0.0,
                "fill_detail": {
                    "success": True,
                    "filled_board_path": fill.completed_kicad_path,
                    "error_message": "",
                    "detail": fill.detail,
                },
                "drc_detail": {
                    "success": drc.success,
                    "violations": drc_violation,
                    "score": 1.0 if drc.success else 0.0,
                    "error_message": drc.error,
                    "detail": {
                        "drc_backend": "agent_hard",
                        "pass": drc.success,
                        "hard_issue_count": drc_violation,
                        "hard_rule_counts": (drc.report.get("result") or {}).get("rule_counts") or {},
                        "issues": drc.report.get("issues") or [],
                        "message_zh": drc.report.get("message_zh", ""),
                        "routing_metrics": drc.report.get("routing_metrics") or {},
                        "board_info": drc.report.get("board_info") or {},
                        "precheck": drc.report.get("precheck") or {},
                        "tool_path": str(self.drc_adapter.drc_agent_package),
                    },
                },
                "error_message": drc.error,
            },
        )


class RerouteAgent:
    def __init__(self, config: RerouteConfig):
        self.config = config

    @classmethod
    def from_env(cls) -> "RerouteAgent":
        return cls(RerouteConfig.from_env())

    def run(self, request: RerouteInput) -> RerouteOutput:
        error = self._validate(request)
        if error:
            return RerouteOutput(
                status="failed",
                task_id=request.task_id,
                routing_patch="",
                completed_kicad="",
                completed_kicad_path="",
                drc_violation=1,
                success=False,
                error=error,
            )

        output_root = Path(request.output_dir or self.config.output_dir)
        raw_patch_path = output_root / "raw_patch" / f"{request.task_id}.kicad_patch"
        completed_path = output_root / "filled_boards" / f"{request.task_id}.kicad_pcb"
        drc_report_path = output_root / "drc_reports" / f"{request.task_id}.json"
        debug_path = output_root / "debug" / f"{request.task_id}.json"
        skill_bank_path = self.config.skill_bank_path or str(output_root / "skill_bank.jsonl")
        for path in (raw_patch_path, completed_path, drc_report_path, debug_path):
            path.parent.mkdir(parents=True, exist_ok=True)

        env = {
            "LLM_API_KEY": self.config.llm_api_key,
            "LLM_BASE_URL": self.config.llm_base_url,
            "LLM_REQUEST_TIMEOUT_SECONDS": str(self.config.timeout_seconds),
        }
        env["REROUTE_SKILL_BANK_PATH"] = skill_bank_path
        env["REROUTE_REPAIR_SAMPLES"] = str(max(0, request.repair_samples))
        env["REROUTE_REPAIR_RETRIES"] = str(max(1, request.repair_retries))

        task = RoutingTask(
            task_id=request.task_id,
            board_id=request.board_id or request.task_id,
            context_kicad=request.context_kicad,
            task_prompt=request.routing_task_prompt,
            label_code="",
            complete_kicad="",
            sample_dir="",
            meta={"source": "reroute_pipeline"},
        )
        fill_adapter = FillAdapter(self.config.ai_pcb_eval_path)
        drc_adapter = AgentHardDRCAdapter(
            self.config.drc_agent_package,
            python_executable=self.config.agent_drc_python,
            timeout_seconds=self.config.timeout_seconds,
        )
        evaluator = _AgentEvaluator(
            fill_adapter=fill_adapter,
            drc_adapter=drc_adapter,
            scratch_dir=output_root / "debug" / "probes",
            target_bga=request.target_bga or self.config.target_bga,
        )

        started = time.perf_counter()
        with _patched_env(env):
            pipeline = PCBPipelineWrapper(
                ai_pcb_eval_path=self.config.ai_pcb_eval_path,
                model=request.model or self.config.model,
                temperature=self.config.temperature,
                top_p=self.config.top_p,
                max_tokens=self.config.max_tokens,
            )
            router = VSEARerouteRouter(
                pipeline=pipeline,
                evaluator=evaluator,
                samples=request.samples,
                shots=0,
                max_rounds=request.max_rounds,
                repair_samples=request.repair_samples,
                repair_retries=request.repair_retries,
                skill_bank_path=skill_bank_path,
            )
            try:
                result = router.route(task)
            except Exception as exc:
                output = RerouteOutput(
                    status="routing_failed",
                    task_id=request.task_id,
                    routing_patch="",
                    completed_kicad="",
                    completed_kicad_path="",
                    drc_violation=1,
                    success=False,
                    debug={"runtime": time.perf_counter() - started},
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                debug_path.write_text(json.dumps(output.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                return output

        routing_patch = extract_kicad_objects(result.routing_output)
        raw_patch_path.write_text(routing_patch, encoding="utf-8")
        final_fill = fill_adapter.fill(task, routing_patch, completed_path)
        if not final_fill.success:
            output = RerouteOutput(
                status="fill_failed",
                task_id=request.task_id,
                routing_patch=routing_patch,
                completed_kicad="",
                completed_kicad_path="",
                drc_violation=1,
                success=False,
                metrics={"runtime": time.perf_counter() - started},
                debug={
                    "router_meta": sanitize_router_meta(result.meta),
                    "fill_detail": final_fill.detail,
                },
                error=final_fill.error,
            )
            debug_path.write_text(json.dumps(output.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return output

        final_drc = drc_adapter.run(
            final_fill.completed_kicad_path,
            drc_report_path,
            target_bga=request.target_bga or self.config.target_bga,
        )
        success = final_drc.success and final_drc.hard_issue_count == 0
        status = "passed" if success else "drc_failed"
        metrics = {
            "runtime": time.perf_counter() - started,
            "path_length": estimate_path_length(routing_patch),
            "via_count": count_vias(routing_patch),
            "semantic_score": final_fill.semantic_score,
            "drc_backend_score": 1.0 if success else 0.0,
        }
        output = RerouteOutput(
            status=status,
            task_id=request.task_id,
            routing_patch=routing_patch,
            completed_kicad=final_fill.completed_kicad if success else "",
            completed_kicad_path=final_fill.completed_kicad_path if success else "",
            drc_violation=final_drc.hard_issue_count,
            success=success,
            metrics=metrics,
            drc_report=final_drc.report,
            debug={
                "router_meta": sanitize_router_meta(result.meta),
                "raw_patch_path": str(raw_patch_path),
                "debug_path": str(debug_path),
            },
            error="" if success else final_drc.error or "DRC hard check failed",
        )
        debug_path.write_text(json.dumps(output.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    @staticmethod
    def _validate(request: RerouteInput) -> str:
        if not request.task_id.strip():
            return "task_id is required"
        if not request.context_kicad.strip():
            return "context_kicad is required"
        if not request.routing_task_prompt.strip():
            return "routing_task_prompt is required; reroute_pipeline does not infer missing nets"
        return ""
