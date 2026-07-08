from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from pcb_agent_langgraph.models.pcb_model import PCBModel
from pcb_agent_langgraph.planner.intent_entities import normalize_router_type
from pcb_agent_langgraph.tools.reroute_context import board_text_from_payload, build_reroute_context, target_nets_from_context
from pcb_agent_langgraph.utils.config import AppConfig


class RerouteLoopTool:
    # ====== 功能：预留可插拔 reroute loop 接口；当前流程仍由 planner 串联独立工具。
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {
            "status": "failed",
            "tool": "reroute_loop",
            "reason": "reroute_loop plugin is not configured; use default reroute -> drc_check -> help_planner flow",
            "modelAttempts": [],
            "drcHistory": [],
            "fallbackUsed": False,
        }


# ====== 功能：返回第一个非空字符串参数。 ======
def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ====== 功能：封装真实外部 PCB 程序工具调用。 ======
class ExternalProgramTool:
    # 外部程序工具是 LangGraph 与真实 PCB 工具链的边界：本类只准备输入、执行命令、解析结果，不做流程决策。
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, name: str, config: AppConfig) -> None:
        self.name = name
        self.config = config

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
        if self.name == "layer_assign":
            return await self._generate_fanout_params(arguments, context)
        if self.name == "escape_order":
            return await self._escape_order_result(arguments, context)
        if self.name == "fanout_route":
            return await self._run_fanout_route(arguments, context)
        if self.name == "prepare_reroute_inputs":
            return await self._prepare_reroute_inputs(arguments, context)
        if self.name == "reroute":
            return await self._run_reroute(arguments, context)
        if self.name == "compress_reroute_context":
            return await self._compress_reroute_context(arguments, context)
        if self.name == "help_planner":
            return await self._run_help_planner(arguments, context)
        raise RuntimeError(f"Unknown external tool: {self.name}")

    # ====== 功能：生成真实 fanout 工具链所需的输入文件和参数。 ======
    async def _generate_fanout_params(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        project_data = _project_data_text(context)
        if not project_data:
            return {"status": "failed", "tool": self.name, "reason": "missing project data; call getProjectData first", "arguments": arguments}

        selected_bga = _first_text(arguments.get("selectedBGA"), arguments.get("targetBGA"), context.get("selectedBGA"), context.get("targetBGA"))
        if not selected_bga:
            return {"status": "failed", "tool": self.name, "reason": "missing selectedBGA; user must specify target BGA or frontend must return one", "arguments": arguments}

        # router 类型和工艺约束来自用户输入或上一轮缓存，最终写入真实 router 的输入文件。
        router_type = normalize_router_type(_first_text(arguments.get("routerType"), context.get("routerType"))) or "rule_135"
        constraints = _merge_constraints(context.get("constraints"), arguments.get("constraints"))
        work_dir = self._work_dir(context)
        work_dir.mkdir(parents=True, exist_ok=True)
        board_path = work_dir / "board_input.txt"
        target_path = work_dir / "bga_input.txt"
        layer_path = work_dir / "layer_input.txt"
        board_path.write_text(project_data, encoding="utf-8")
        target_path.write_text(selected_bga, encoding="utf-8")

        command = self._layer_assign_command(router_type)
        stdout = stderr = ""
        return_code: int | None = None
        if command:
            command = _resolve_command_program([part.format(board=board_path, target=target_path, output=layer_path, work_dir=work_dir) for part in command], self.config.root)
            completed = await _run_command(command, work_dir, int(context.get("timeout") or self.config.router.rule_timeout_seconds))
            stdout, stderr, return_code = completed["stdout"], completed["stderr"], completed["returncode"]
            if return_code != 0:
                return {"status": "failed", "tool": self.name, "reason": "layer_assign command failed", "command": command, "stdout": stdout, "stderr": stderr, "returncode": return_code, "workDir": str(work_dir)}
        else:
            layer_path.write_text(_default_layer_input(selected_bga, constraints), encoding="utf-8")

        fanout_params = {
            "selectedBGA": selected_bga.upper(),
            "targetBGAs": arguments.get("targetBGAs") or context.get("targetBGAs") or [selected_bga.upper()],
            "routerType": router_type,
            "constraints": constraints,
            "boardInputPath": str(board_path),
            "targetInputPath": str(target_path),
            "layerInputPath": str(layer_path),
            "orderInputPath": str(work_dir / "order_input.txt"),
            "routeOutputPath": str(_route_output_path(work_dir, router_type)),
            "orderLines": _read_lines(layer_path),
        }
        return {"status": "ok", "tool": self.name, "fanoutParams": fanout_params, "workDir": str(work_dir), "stdout": stdout, "stderr": stderr, "returncode": return_code}

    # ====== 功能：生成或调用逃逸顺序工具并更新 fanout 参数。 ======
    async def _escape_order_result(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        fanout_params = _fanout_params(arguments, context)
        if not isinstance(fanout_params, dict) or not fanout_params.get("layerInputPath"):
            return {"status": "failed", "tool": self.name, "reason": "fanoutParams.layerInputPath missing; layer_assign did not complete"}
        work_dir = Path(str(fanout_params.get("layerInputPath"))).parent
        order_path = Path(str(fanout_params.get("orderInputPath") or work_dir / "order_input.txt"))
        board_path = Path(str(fanout_params.get("boardInputPath")))
        layer_path = Path(str(fanout_params.get("layerInputPath")))

        router_type = normalize_router_type(str(fanout_params.get("routerType") or "")) or "rule_135"
        command = self._escape_order_command(router_type)
        stdout = stderr = ""
        return_code: int | None = None
        if command:
            command = _resolve_command_program([part.format(layer=layer_path, board=board_path, output=order_path, work_dir=work_dir) for part in command], self.config.root)
            completed = await _run_command(command, work_dir, int(context.get("timeout") or self.config.router.rule_timeout_seconds))
            stdout, stderr, return_code = completed["stdout"], completed["stderr"], completed["returncode"]
            if return_code != 0:
                return {"status": "failed", "tool": self.name, "reason": "escape_order command failed", "command": command, "stdout": stdout, "stderr": stderr, "returncode": return_code, "workDir": str(work_dir)}
        elif layer_path.exists():
            shutil.copyfile(layer_path, order_path)
        else:
            order_path.write_text(_default_layer_input(str(fanout_params.get("selectedBGA") or "BGA"), fanout_params.get("constraints") or {}), encoding="utf-8")

        fanout_params = dict(fanout_params)
        fanout_params["orderInputPath"] = str(order_path)
        fanout_params["orderLines"] = _read_lines(order_path)
        return {"status": "ok", "tool": self.name, "orderLines": fanout_params["orderLines"], "fanoutParams": fanout_params, "workDir": str(work_dir), "stdout": stdout, "stderr": stderr, "returncode": return_code}

    # ====== 功能：调用真实 fanout router 并解析布线输出。 ======
    async def _run_fanout_route(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        project_data = _project_data_text(context)
        fanout_params = _fanout_params(arguments, context)
        if not project_data:
            return {"status": "failed", "tool": self.name, "reason": "missing project data; call getProjectData first"}
        if not isinstance(fanout_params, dict):
            return {"status": "failed", "tool": self.name, "reason": "missing fanoutParams; layer_assign/escape_order must complete first"}

        router_type = normalize_router_type(fanout_params.get("routerType")) or "rule_135"
        # 真实布线命令必须来自 config.ini 或项目内 router 目录，缺失时明确失败。
        command = self._route_command(router_type)
        if not command:
            return {"status": "failed", "tool": self.name, "reason": f"router command for {router_type} is not configured", "fanoutParams": fanout_params}

        work_dir = Path(str(fanout_params.get("boardInputPath") or self._work_dir(context) / "board_input.txt")).parent
        board_path = Path(str(fanout_params.get("boardInputPath") or work_dir / "board_input.txt"))
        order_path = Path(str(fanout_params.get("orderInputPath") or work_dir / "order_input.txt"))
        output_path = Path(str(fanout_params.get("routeOutputPath") or _route_output_path(work_dir, router_type)))
        command = _resolve_command_program([part.format(board=board_path, order=order_path, output=output_path, work_dir=work_dir) for part in command], self.config.root)
        completed = await _run_command(command, work_dir, int(context.get("timeout") or self.config.router.rule_timeout_seconds))
        if completed["returncode"] != 0:
            return {"status": "failed", "tool": self.name, "reason": "fanout router command failed", "command": command, **completed, "fanoutParams": fanout_params, "workDir": str(work_dir)}
        import_file = _fanout_import_file(work_dir, router_type, output_path)
        if not import_file:
            return {"status": "failed", "tool": self.name, "reason": "fanout router completed but no route output file was found", "command": command, **completed, "workDir": str(work_dir)}
        conversion = _convert_router_output(self.config.root, project_data, work_dir, router_type, import_file)
        routing_result = _first_existing(work_dir / "routing_input.txt", Path(str(conversion.get("routedLayoutTxtFilePath") or "")))
        report_text = _read_fanout_router_report(work_dir)
        report = _router_report(import_file, completed)
        report.update({"text": report_text, "conversion": conversion.get("notes", [])})
        return {
            "status": "ok",
            "tool": self.name,
            "fanoutParams": fanout_params,
            "routingResult": str(routing_result or work_dir / "routing_input.txt"),
            "importLinesFilePath": str(import_file),
            "routedLayoutTxtFilePath": str(routing_result or conversion.get("routedLayoutTxtFilePath") or import_file),
            "routedKicadFilePath": conversion.get("routedKicadFilePath", ""),
            "report": report_text,
            "reportDetail": report,
            "workDir": str(work_dir),
            "command": command,
            **completed,
        }
    # ====== 功能：在 reroute 前对 KiCad 版图执行分块检索压缩。 ======
    async def _prepare_reroute_inputs(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        work_dir = self._work_dir(context) / "reroute_inputs"
        work_dir.mkdir(parents=True, exist_ok=True)
        delete_result = context.get("deleteTracesResult") if isinstance(context.get("deleteTracesResult"), dict) else {}
        source_payload = delete_result.get("projectData") or delete_result.get("project_data") or delete_result.get("boardData") or context.get("projectData") or context.get("board_data")
        board_text, source_path = board_text_from_payload(source_payload)
        if not board_text:
            return {"status": "failed", "tool": self.name, "reason": "missing board data for reroute input preparation"}
        try:
            kicad_path, kicad_text, source_layout_path = _ensure_reroute_kicad_input(self.config.root, board_text, source_path, work_dir)
        except Exception as exc:
            return {
                "status": "failed",
                "tool": self.name,
                "reason": f"格式转换错误：无法把当前版图数据转换为重布线输入格式（{exc}）",
                "sourceLayoutPath": source_path,
                "textPreview": str(board_text or "")[:400],
                "tracebackSummary": _traceback_summary(exc),
            }

        missing_routes = delete_result.get("missing_routes") or delete_result.get("missingRoutes") or context.get("missingRoutes") or []
        if not isinstance(missing_routes, list):
            missing_routes = []
        selected_nets = _nets_from_missing_routes(missing_routes) or list(context.get("selectedNets") or [])
        conversion = _convert_missing_routes_to_kicad(self.config.root, missing_routes, work_dir)
        missing_routes_kicad = conversion.get("missingRoutesKicad") if isinstance(conversion.get("missingRoutesKicad"), list) else []
        route_params = {
            "missingRoutes": missing_routes,
            "missingRoutesKicad": missing_routes_kicad,
            "orderLines": missing_routes_kicad or missing_routes,
            "pcbrouterNets": missing_routes_kicad or missing_routes,
            "selectedNets": selected_nets,
        }
        explicit_csv = arguments.get("localRouteCsvPath") or context.get("explicitLocalRouteCsvPath")
        if explicit_csv:
            route_params["localRouteCsvPath"] = explicit_csv
        csv_path, csv_mode = _prepare_local_route_csv(self.config.root, route_params, kicad_text, work_dir)
        return {
            "status": "ok",
            "tool": self.name,
            "sourceLayoutPath": source_layout_path,
            "kicadBoardPath": str(kicad_path),
            "kicadBoardText": kicad_text,
            "localRouteCsvPath": str(csv_path),
            "missingRoutes": missing_routes,
            "missingRoutesKicad": missing_routes_kicad,
            "selectedNets": selected_nets,
            "geometryConversionStatus": conversion.get("geometryConversionStatus"),
            "geometryConversionNotes": conversion.get("geometryConversionNotes") or [],
            "missingRoutesRawPath": conversion.get("missingRoutesRawPath") or "",
            "missingRoutesKicadPath": conversion.get("missingRoutesKicadPath") or "",
            "csvMode": csv_mode,
            "workDir": str(work_dir),
        }

    # ====== 功能：在 reroute 前对 KiCad 版图执行分块检索压缩。 ======
    async def _compress_reroute_context(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        reroute_input = context.get("rerouteInput") if isinstance(context.get("rerouteInput"), dict) else {}
        delete_result = context.get("deleteTracesResult") if isinstance(context.get("deleteTracesResult"), dict) else {}
        local_context = delete_result.get("localContext") if isinstance(delete_result.get("localContext"), dict) else context.get("localContext") or {}
        board_text = str(reroute_input.get("kicadBoardText") or "")
        board_path = str(reroute_input.get("kicadBoardPath") or "")
        if not board_text:
            board_text, board_path = board_text_from_payload(delete_result)
        if not board_text:
            board_text, board_path = board_text_from_payload(context.get("projectData") or context.get("board_data") or context.get("project_data"))
        if not board_text:
            return {"status": "failed", "tool": self.name, "reason": "missing KiCad board data for reroute context compression"}
        nets = target_nets_from_context(arguments, delete_result, local_context, context)
        selected_trace_ids = delete_result.get("selectedTraceIds") or delete_result.get("selected_trace_ids") or arguments.get("selectedTraceIds") or []
        if not isinstance(selected_trace_ids, list):
            selected_trace_ids = [str(selected_trace_ids)]
        task_description = _first_text(arguments.get("taskDescription"), context.get("user_input"), "前端已框选并删除局部走线，请根据上下文补全同网缺失连接。")
        result = build_reroute_context(
            board_text=board_text,
            task_description=task_description,
            selected_trace_ids=[str(item) for item in selected_trace_ids],
            nets=nets,
            local_context=local_context,
            chunk_chars=int(arguments.get("chunkChars") or 1600),
            overlap_chars=int(arguments.get("overlapChars") or 600),
            retrieve_k=int(arguments.get("retrieveK") or 2),
        )
        result["boardPath"] = board_path
        result["selectedNets"] = nets
        result["selectedTraceIds"] = selected_trace_ids
        return result

    # ====== 功能：执行主模型 reroute，一轮只生成候选重布结果，不在这里兜底 help_planner。 ======
    async def _run_reroute(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        if not self.config.model.base_url or not self.config.model.model:
            return {"status": "unavailable", "tool": self.name, "reason": "reroute model is not configured", "failureStage": "model_config", "failureType": "model_unavailable"}
        reroute_input = context.get("rerouteInput") if isinstance(context.get("rerouteInput"), dict) else {}
        kicad_text = str(reroute_input.get("kicadBoardText") or "")
        kicad_path = str(reroute_input.get("kicadBoardPath") or "")
        if not _is_kicad_board_text(kicad_text):
            return {
                "status": "failed",
                "tool": self.name,
                "reason": "missing KiCad .kicad_pcb board data for reroute",
                "failureStage": "input",
                "failureType": "missing_kicad_board_data",
                "kicadBoardPath": kicad_path,
                "kicadBoardTextPreview": kicad_text[:400],
                "rerouteInputKeys": sorted(reroute_input.keys()),
                "rerouteInputStatus": reroute_input.get("status"),
                "prepareWorkDir": reroute_input.get("workDir"),
            }
        attempt = int(arguments.get("attempt") or 0) or int(context.get("rerouteAttemptCount") or 0) + 1
        work_dir = self._work_dir(context) / "reroute" / f"attempt_{attempt}"
        work_dir.mkdir(parents=True, exist_ok=True)
        prompt_payload = _reroute_model_payload(arguments, context, reroute_input, attempt)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 PCB 局部拆线重布模型。根据 missing routes、压缩版图上下文和 DRC 反馈，"
                    "输出本轮可用于 DRC 回填检查的 KiCad patch 文本。优先返回 JSON；可包含 "
                    "routedText/content/report 字段，不能把解释性文字伪装成可导入布线文件。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
        ]
        try:
            model = PCBModel(self.config.model)
            model_result = await asyncio.to_thread(model.complete, messages)
        except Exception as exc:
            return {"status": "unavailable", "tool": self.name, "reason": f"reroute model call failed: {exc}", "attempt": attempt, "failureStage": "model_call", "failureType": "model_unavailable", "selectedNets": prompt_payload.get("selectedNets") or [], "rawSummary": str(exc), "tracebackSummary": _traceback_summary(exc), "workDir": str(work_dir)}
        parsed_json = _loads_json_object(model_result.content)
        parsed = parsed_json or {"content": model_result.content}
        routed_text = _first_text(parsed.get("routedText"), parsed.get("content"), parsed.get("report"), model_result.content)
        output_path = work_dir / "reroute_output.txt"
        output_path.write_text(routed_text, encoding="utf-8")
        coord_check = _validate_reroute_model_coordinates(routed_text, reroute_input.get("missingRoutesKicad") or [])
        status = "ok" if coord_check.get("passed") else "failed"
        reason = "模型 reroute 已生成候选结果。" if status == "ok" else coord_check.get("reason") or "model output coordinate mismatch"
        return {
            "status": status,
            "tool": self.name,
            "attempt": attempt,
            "modelOutputText": routed_text,
            "modelOutputPath": str(output_path),
            "routedText": routed_text,
            "report": str(parsed.get("report") or reason),
            "modelRaw": parsed,
            "workDir": str(work_dir),
            "elapsedMs": model_result.elapsed_ms,
            "modelOutputChars": len(str(model_result.content or "")),
            "modelParsedJson": parsed_json is not None,
            "kicadBoardPath": kicad_path,
            "selectedNets": prompt_payload.get("selectedNets") or [],
            "rawSummary": str(model_result.content or "")[:1200],
            "coordinateCheck": coord_check,
            "failureType": "model_output_coordinate_mismatch" if status == "failed" else "",
            "reason": reason if status == "failed" else None,
        }

    # ====== 功能：调用兜底局部规则布线器 help_planner。 ======
    async def _run_help_planner(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        if not self.config.reroute_help.enabled:
            return {"status": "failed", "tool": self.name, "reason": "reroute help_planner is disabled in config"}
        reroute_input = context.get("rerouteInput") if isinstance(context.get("rerouteInput"), dict) else {}
        project_data = str(reroute_input.get("kicadBoardText") or "")
        source_board_path = str(reroute_input.get("kicadBoardPath") or "")
        route_params = _help_route_params(arguments, context)
        if reroute_input.get("localRouteCsvPath"):
            route_params["localRouteCsvPath"] = reroute_input.get("localRouteCsvPath")
        if reroute_input.get("selectedNets"):
            route_params["selectedNets"] = reroute_input.get("selectedNets")
        work_dir = self._work_dir(context) / "help_planner"
        pcbrouter_bin = _resolve_path(self.config.root, self.config.router.pcbrouter_bin)
        if not project_data:
            diagnostics = _help_planner_diagnostics(work_dir, pcbrouter_bin, source_board_path)
            return {"status": "failed", "tool": self.name, "reason": "help_planner requires KiCad .kicad_pcb input; rerouteInput is missing", "routeParams": route_params, **diagnostics}
        input_error = _help_planner_input_error(project_data, source_board_path)
        if input_error:
            diagnostics = _help_planner_diagnostics(work_dir, pcbrouter_bin, source_board_path)
            repro_paths = _safe_archive_help_planner_repro_case(
                work_dir=work_dir,
                source_layout_path=str(reroute_input.get("sourceLayoutPath") or context.get("sourceLayoutPath") or source_board_path),
                source_board_path=source_board_path,
                input_board_path=str(diagnostics.get("inputBoardPath") or ""),
                input_csv_path=str(diagnostics.get("inputCsvPath") or ""),
                output_board_path=str(diagnostics.get("outputBoardPath") or ""),
                output_csv_path=str(diagnostics.get("outputCsvPath") or ""),
                import_lines_path="",
                route_params=route_params,
                selected_nets=list(reroute_input.get("selectedNets") or route_params.get("selectedNets") or []),
                missing_routes=list(reroute_input.get("missingRoutes") or context.get("missingRoutes") or []),
                status="failed",
                reason=input_error,
                command="",
            )
            return {"status": "failed", "tool": self.name, "reason": input_error, "routeParams": route_params, **diagnostics, **repro_paths}
        repro_paths: dict[str, Any] = {}
        try:
            module = _load_module("_pcb_agent_langgraph_local_router", self.config.root / "tools" / "pcb_local_router.py")
            old_bin = os.environ.get("PCBROUTER_BIN")
            os.environ["PCBROUTER_BIN"] = str(pcbrouter_bin)
            try:
                # help_planner 是 reroute-DRC 多次失败后的兜底局部规则布线器。
                result = await asyncio.to_thread(
                    module.run_pcbrouter_local_route,
                    project_data=project_data,
                    route_params=route_params,
                    work_dir=work_dir,
                    source_board_path=source_board_path,
                    timeout=self.config.router.pcbrouter_timeout_seconds,
                )
            finally:
                if old_bin is None:
                    os.environ.pop("PCBROUTER_BIN", None)
                else:
                    os.environ["PCBROUTER_BIN"] = old_bin
            payload = _dataclass_to_dict(result)
            routing_path = str(payload.get("routing_result_path") or "")
            output_csv_path = str(payload.get("output_csv_path") or "")
            repro_paths = _safe_archive_help_planner_repro_case(
                work_dir=work_dir,
                source_layout_path=str(reroute_input.get("sourceLayoutPath") or context.get("sourceLayoutPath") or source_board_path),
                source_board_path=source_board_path,
                input_board_path=str(payload.get("input_board_path") or ""),
                input_csv_path=str(payload.get("input_csv_path") or ""),
                output_board_path=routing_path,
                output_csv_path=output_csv_path,
                import_lines_path="",
                route_params=route_params,
                selected_nets=list(reroute_input.get("selectedNets") or route_params.get("selectedNets") or []),
                missing_routes=list(reroute_input.get("missingRoutes") or context.get("missingRoutes") or []),
                status="ok",
                reason="",
                command="",
            )
            import_path = ""
            import_notes: list[str] = []
            if routing_path:
                import_path, import_notes = _write_helper_router_incremental_import_file(
                    original_board_text=project_data,
                    routed_board_path=Path(routing_path),
                    selected_nets=list(reroute_input.get("selectedNets") or route_params.get("selectedNets") or []),
                    work_dir=work_dir,
                )
            if repro_paths and import_path:
                _copy_repro_file(Path(import_path), Path(str(repro_paths.get("reproCaseDir") or "")), "line.out")
                _update_repro_manifest(Path(str(repro_paths.get("reproManifestPath") or "")), "line.out", Path(import_path))
            return {
                "status": "ok",
                "tool": self.name,
                "routingResult": routing_path,
                "routedKicadFilePath": routing_path,
                "routedLayoutTxtFilePath": "",
                "importLinesFilePath": import_path,
                "incrementalImportFilePath": import_path,
                "incrementalImportNotes": import_notes,
                "inputBoardPath": str(payload.get("input_board_path") or ""),
                "inputCsvPath": str(payload.get("input_csv_path") or ""),
                "outputCsvPath": output_csv_path,
                "report": payload.get("report") or "pcbrouter local route completed",
                "detail": payload,
                "seedGeometryInjected": bool(payload.get("seed_geometry_injected")),
                "seedGeometryCount": int(payload.get("seed_geometry_count") or 0),
                "seedGeometryPath": str(payload.get("seed_geometry_path") or ""),
                "workDir": str(work_dir),
                **repro_paths,
            }
        except Exception as exc:
            diagnostics = _help_planner_diagnostics(work_dir, pcbrouter_bin, source_board_path)
            tb = _traceback_summary(exc)
            reason = _help_planner_failure_reason(str(exc), diagnostics)
            failure_type = "local_target_inference_failed" if "cannot infer local target" in reason.lower() else ""
            repro_paths = _safe_archive_help_planner_repro_case(
                work_dir=work_dir,
                source_layout_path=str(reroute_input.get("sourceLayoutPath") or context.get("sourceLayoutPath") or source_board_path),
                source_board_path=source_board_path,
                input_board_path=str(diagnostics.get("inputBoardPath") or ""),
                input_csv_path=str(diagnostics.get("inputCsvPath") or ""),
                output_board_path=str(diagnostics.get("outputBoardPath") or ""),
                output_csv_path=str(diagnostics.get("outputCsvPath") or ""),
                import_lines_path="",
                route_params=route_params,
                selected_nets=list(reroute_input.get("selectedNets") or route_params.get("selectedNets") or []),
                missing_routes=list(reroute_input.get("missingRoutes") or context.get("missingRoutes") or []),
                status="failed",
                reason=reason,
                command=str(diagnostics.get("command") or ""),
            )
            print(f"help_planner_exception traceback_summary={tb}")
            return {"status": "failed", "tool": self.name, "reason": reason, "failureType": failure_type, "tracebackSummary": tb, "routeParams": route_params, **diagnostics, **repro_paths}


    # ====== 功能：根据 router 类型选择真实 layer_assign 命令。 ======
    def _layer_assign_command(self, router_type: str) -> list[str]:
        configured = self.config.router.layer_assign_command
        if configured:
            return _split_command(configured)
        if router_type == "rule_arc":
            router_dir = _resolve_path(self.config.root, self.config.router.rule_arc_dir)
            return [str(router_dir / "layer_assign_cpp.exe"), "-arc", "{board}", "{target}", "--output", "{output}"]
        router_dir = _resolve_path(self.config.root, self.config.router.rule_135_dir)
        return [str(router_dir / "layer_assign_cpp.exe"), "{board}", "{target}", "--output", "{output}"]

    # ====== 功能：根据 router 类型选择真实 escape_order 命令。 ======
    def _escape_order_command(self, router_type: str) -> list[str]:
        configured = self.config.router.escape_order_command
        if configured:
            return _split_command(configured)
        if router_type == "rule_arc":
            router_dir = _resolve_path(self.config.root, self.config.router.rule_arc_dir)
            return [str(router_dir / "escape_order_cpp.exe"), "{layer}", "{board}"]
        router_dir = _resolve_path(self.config.root, self.config.router.rule_135_dir)
        return [str(router_dir / "escape_order_cpp.exe"), "{layer}", "{board}"]
    # ====== 功能：根据 router 类型解析实际命令行。 ======
    def _route_command(self, router_type: str) -> list[str]:
        if router_type == "rule_arc":
            configured = self.config.router.rule_arc_command
            if configured:
                return _split_command(configured)
            router_dir = _resolve_path(self.config.root, self.config.router.rule_arc_dir)
            return [str(router_dir / "arc_main.exe"), "{order}", "{board}", str(router_dir / "constrain.txt")]
        configured = self.config.router.rule_135_command
        if configured:
            return _split_command(configured)
        router_dir = _resolve_path(self.config.root, self.config.router.rule_135_dir)
        return [str(router_dir / "135_main.exe"), "{board}", "{order}"]

    # ====== 功能：计算当前会话的外部工具工作目录。 ======
    def _work_dir(self, context: dict[str, Any]) -> Path:
        session_id = str(context.get("session_id") or "session").strip() or "session"
        return _resolve_path(self.config.root, self.config.router.work_dir) / session_id


# ====== 功能：封装 DRC、BGA 提取和可解释性分析工具调用。 ======
class AnalysisTool:
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, name: str, config: AppConfig) -> None:
        self.name = name
        self.config = config

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
        await asyncio.sleep(0)
        if self.name == "pcb_extra_bga":
            return await self._run_bga_extract_script(arguments, context)
        if self.name == "drc_check":
            return await self._run_drc(arguments, context)
        if self.name == "explainability_report":
            # 启用可解释性模型时优先调用真实模型；未启用时退回 DRC 文本摘要，便于离线/单测运行。
            if self.config.explain_model.enabled:
                return await self._run_explain_model(arguments, context)
            drc_result = context.get("drcResult") or {}
            status = "ok" if isinstance(drc_result, dict) and drc_result else "failed"
            return {"status": status, "report": _build_report(drc_result), "drcResult": drc_result}
        raise RuntimeError(f"Unknown analysis tool: {self.name}")

    # ====== 功能：调用独立脚本提取 BGA 器件列表。 ======
    async def _run_bga_extract_script(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        board_data = str(arguments.get("boardData") or context.get("board_data") or "")
        if not board_data.strip():
            return {"status": "failed", "tool": self.name, "reason": "missing board data for BGA extraction"}
        tool_path = _resolve_path(self.config.root, self.config.bga_extract.tool_path)
        if not tool_path.exists():
            return {"status": "failed", "tool": self.name, "reason": "BGA extract script does not exist", "tool_path": str(tool_path)}
        work_dir = self.config.root / "bga_extract_work" / str(context.get("session_id") or "session")
        work_dir.mkdir(parents=True, exist_ok=True)
        source_path = Path(board_data)
        if source_path.exists() and source_path.is_file():
            input_path = source_path
        else:
            input_path = work_dir / "board_input.txt"
            input_path.write_text(board_data, encoding="utf-8")
        output_path = work_dir / "bga_components.json"
        cache_payload = _load_bga_cache(input_path)
        if cache_payload:
            cache_payload.update({"status": "ok", "tool": self.name, "source": str(input_path), "execution": "cache", "input_path": str(input_path), "output_path": str(output_path)})
            output_path.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return cache_payload
        try:
            module = _load_module("_pcb_agent_langgraph_extract_bga", tool_path)
            components = module.extract_bga_components(input_path)
            payload = {
                "source": str(input_path),
                "match_rule": f"script function extract_bga_components; U components over {getattr(module, 'BGA_PIN_THRESHOLD', 200)} pins are included",
                "count": len(components),
                "components": components,
                "status": "ok",
                "tool": self.name,
                "script_path": str(tool_path),
                "input_path": str(input_path),
                "output_path": str(output_path),
                "execution": "in_process",
            }
            output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return payload
        except Exception as exc:
            in_process_error = str(exc)
        python_exe = _python_executable(self.config)
        command = [str(python_exe), str(tool_path), str(input_path), "-o", str(output_path)]
        completed = await _run_command(command, work_dir, self.config.bga_extract.timeout_seconds)
        if completed["returncode"] != 0:
            fallback_payload = _load_bga_cache(input_path)
            if fallback_payload:
                fallback_payload.update({"status": "ok", "tool": self.name, "source": str(input_path), "execution": "cache_after_script_failure", "input_path": str(input_path), "output_path": str(output_path), "script_error": {"in_process_error": in_process_error, "command": command, "tool_path": str(tool_path), **_command_summary(completed)}})
                output_path.write_text(json.dumps(fallback_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return fallback_payload
            return {"status": "failed", "tool": self.name, "reason": "BGA extract script failed", "in_process_error": in_process_error, "command": command, "tool_path": str(tool_path), "input_path": str(input_path), "output_path": str(output_path), **completed}
        if not output_path.exists():
            return {"status": "failed", "tool": self.name, "reason": "BGA extract script produced no output", "command": command, **completed}
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        payload.update({"status": "ok", "tool": self.name, "source": "script", "script_path": str(tool_path), "input_path": str(input_path), "output_path": str(output_path), "command": command})
        return payload
    # ====== 功能：调用真实 DRC 工具链并整理结果。 ======
    async def _run_drc(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # DRC 不能返回假成功：配置、脚本、评测根目录、输入数据缺一都会显式失败。
        if not self.config.drc.enabled:
            return {"status": "failed", "tool": self.name, "reason": "DRC is disabled or missing [drc] enabled=1 in config.ini"}
        tool_path = _resolve_path(self.config.root, self.config.drc.tool_path)
        eval_root = _resolve_path(self.config.root, self.config.drc.eval_root)
        if not tool_path.exists():
            return {"status": "failed", "tool": self.name, "reason": "DRC tool_path does not exist", "tool_path": str(tool_path)}
        if not eval_root.exists():
            return {"status": "failed", "tool": self.name, "reason": "DRC eval_root does not exist", "eval_root": str(eval_root)}

        try:
            module = _load_module("_pcb_agent_langgraph_drc_tool", tool_path)
            if hasattr(module, "set_eval_root"):
                module.set_eval_root(eval_root)
            reroute_input = context.get("rerouteInput") if isinstance(context.get("rerouteInput"), dict) else {}
            original_board = str(reroute_input.get("kicadBoardText") or "") or _project_data_text(context)
            routed_text = _routed_text(arguments, context)
            routed_board_path = _routed_board_path(arguments, context)
            routed_text_chars = len(routed_text)
            routed_source = _routed_input_source(arguments, context, routed_board_path)
            has_full_board = bool(routed_board_path and routed_board_path.suffix.lower() == ".kicad_pcb" and routed_board_path.exists())
            if has_full_board and hasattr(module, "validate_kicad_board_with_drc"):
                drc_input_mode = "full_board"
                print(f"drc_input mode={drc_input_mode} source={routed_source} routedBoardPath={routed_board_path} routedTextChars={routed_text_chars}")
                attempt = module.validate_kicad_board_with_drc(
                    board_path=routed_board_path,
                    sample_id=str(context.get("session_id") or "session"),
                    iteration=1,
                )
            else:
                drc_input_mode = "patch"
                if not original_board:
                    return {"status": "failed", "tool": self.name, "reason": "missing original board data for DRC", "drcInputMode": drc_input_mode, "drcInputSource": routed_source, "routedBoardPath": str(routed_board_path or ""), "routedTextChars": routed_text_chars, "tool_path": str(tool_path), "eval_root": str(eval_root)}
                if not routed_text:
                    reason = "missing routed output/import lines for DRC"
                    if has_full_board:
                        reason = "DRC full-board validator is unavailable and no routed patch text was provided"
                    return {"status": "failed", "tool": self.name, "reason": reason, "drcInputMode": drc_input_mode, "drcInputSource": routed_source, "routedBoardPath": str(routed_board_path or ""), "routedTextChars": routed_text_chars, "tool_path": str(tool_path), "eval_root": str(eval_root)}
                print(f"drc_input mode={drc_input_mode} source={routed_source} routedBoardPath={routed_board_path or ''} routedTextChars={routed_text_chars}")
                attempt = module.validate_kicad_patch_with_drc(
                    original_board_data=original_board,
                    model_output_text=routed_text,
                    output_dir=_resolve_path(self.config.root, self.config.drc.work_dir) / str(context.get("session_id") or "session"),
                    sample_id=str(context.get("session_id") or "session"),
                    iteration=1,
                )
            payload = _dataclass_to_dict(attempt)
            full_board_passed = bool(payload.get("passed"))
            drc_result = payload.get("drc_result") or {}
            drc_execution_valid = _drc_execution_valid(payload, drc_result)
            scoped = _target_scoped_drc_result(drc_result, context, drc_execution_valid=drc_execution_valid)
            target_scoped_passed = scoped.get("targetScopedPassed") is True
            passed = drc_execution_valid and (full_board_passed or target_scoped_passed)
            errors = [] if passed else [scoped.get("targetFailureSummary") or payload.get("failure_summary") or "DRC failed"]
            result_payload = {
                "status": "ok" if passed else "failed",
                "tool": self.name,
                "passed": passed,
                "fullBoardPassed": full_board_passed,
                "targetScopedPassed": target_scoped_passed,
                "drcExecutionValid": drc_execution_valid,
                "errors": errors,
                "score": 1.0 if passed else 0.0,
                "detail": payload,
                "tool_path": str(tool_path),
                "eval_root": str(eval_root),
                "drc_result": drc_result,
                "drcInputMode": drc_input_mode,
                "drcInputSource": routed_source,
                "routedBoardPath": str(routed_board_path or ""),
                "routedTextChars": routed_text_chars,
                **scoped,
            }
            print(f"drc_result status={result_payload['status']} passed={passed} fullBoardPassed={full_board_passed} targetScopedPassed={target_scoped_passed} drcExecutionValid={drc_execution_valid} mode={drc_input_mode} source={routed_source} failure_summary={payload.get('failure_summary') or result_payload.get('reason') or ''}")
            if not passed and isinstance(context.get("helpPlannerResult"), dict):
                smoke = await _run_explainability_smoke(self.config, context)
                if smoke:
                    result_payload["debug"] = {"explainabilitySmoke": smoke, "explainabilitySmokeReportPath": smoke.get("report_path") or ""}
                    print(f"explainability_smoke status={smoke.get('status')} report_path={smoke.get('report_path', '')} reason={smoke.get('reason', '')}")
            return result_payload
        except Exception as exc:
            return {"status": "failed", "tool": self.name, "reason": str(exc), "tool_path": str(tool_path), "eval_root": str(eval_root)}



    # ====== 功能：调用可解释性模型 runtime 并读取报告。 ======
    async def _run_explain_model(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if not self.config.explain_model.enabled:
            return {"status": "failed", "tool": self.name, "reason": "explain model is disabled"}
        python_exe = _resolve_path(self.config.root, self.config.explain_model.python_executable)
        code_dir = _resolve_path(self.config.root, self.config.explain_model.code_dir)
        checkpoint = _resolve_path(self.config.root, self.config.explain_model.checkpoint_path)
        if not _drc_context_passed(context):
            return {"status": "skipped", "tool": self.name, "reason": "skipped_drc_not_passed", "explainabilityStatus": "skipped_drc_not_passed"}
        board_path = _explain_input_board(arguments, context)
        if not python_exe.exists():
            return {"status": "failed", "tool": self.name, "reason": "explain python executable does not exist", "python": str(python_exe)}
        if not code_dir.exists():
            return {"status": "failed", "tool": self.name, "reason": "explain code_dir does not exist", "code_dir": str(code_dir)}
        if not checkpoint.exists():
            return {"status": "failed", "tool": self.name, "reason": "explain checkpoint does not exist", "checkpoint": str(checkpoint)}
        if not board_path or not board_path.exists() or board_path.suffix.lower() != ".kicad_pcb":
            return {"status": "failed", "tool": self.name, "reason": "explain model requires a .kicad_pcb input file", "input": str(board_path or "")}
        command = [str(python_exe), str(code_dir / "infer_ascend_multiview_classifier.py"), str(board_path), str(checkpoint)]
        try:
            # 可解释性模型在独立 Python runtime 中运行，避免污染主 Agent 环境。
            completed = await _run_command(command, code_dir, self.config.explain_model.timeout_seconds)
            report_path = code_dir / "inference_runs" / board_path.stem / "report.txt"
            prediction_path = code_dir / "inference_runs" / board_path.stem / "prediction.json"
            report_text = report_path.read_text(encoding="utf-8", errors="ignore") if report_path.exists() else completed.get("stdout", "")
            prediction = json.loads(prediction_path.read_text(encoding="utf-8")) if prediction_path.exists() else {}
            if completed["returncode"] != 0:
                return {"status": "failed", "tool": self.name, "reason": "explain model command failed", "command": command, **completed}
            return {
                "status": "ok",
                "tool": self.name,
                "report": report_text,
                "prediction": prediction,
                "report_path": str(report_path),
                "prediction_json_path": str(prediction_path),
                "command": command,
                **completed,
            }
        except Exception as exc:
            return {"status": "failed", "tool": self.name, "reason": str(exc), "command": command}

# ====== 功能：从上下文中提取原始 PCB 文本数据。 ======

# ====== 功能：读取同项目 BGA 脚本缓存，避免运行时缺标准库时阻断 fanout。 ======
def _load_bga_cache(input_path: Path) -> dict[str, Any]:
    candidates = [input_path.with_name("export_bga_components.json"), input_path.with_name("bga_components.json")]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        components = payload.get("components") if isinstance(payload, dict) else payload
        components = _normalize_bga_components(components)
        if isinstance(components, list) and components:
            return {
                "source": str(candidate),
                "match_rule": payload.get("match_rule", "cached export_bga_components.json") if isinstance(payload, dict) else "cached export_bga_components.json",
                "count": len(components),
                "components": components,
                "cache_path": str(candidate),
            }
    return {}


# ====== 功能：截断命令输出，便于前端日志显示失败摘要。 ======

# ====== 功能：统一 BGA 缓存字段，保证前端 selection 能稳定显示 refdes。 ======
def _normalize_bga_components(components: Any) -> list[dict[str, Any]]:
    if not isinstance(components, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in components:
        if not isinstance(item, dict):
            continue
        component = dict(item)
        refdes = _first_text(component.get("refdes"), component.get("ref"), component.get("reference"), component.get("name"), component.get("label"))
        if refdes:
            component.setdefault("refdes", refdes)
            component.setdefault("ref", refdes)
            component.setdefault("reference", refdes)
            component.setdefault("name", refdes)
            component.setdefault("label", refdes)
        normalized.append(component)
    return normalized

def _command_summary(completed: dict[str, Any]) -> dict[str, Any]:
    return {
        "returncode": completed.get("returncode"),
        "stdout": str(completed.get("stdout") or "")[:1600],
        "stderr": str(completed.get("stderr") or "")[:1600],
    }


# ====== 功能：压缩异常 traceback，保留文件名、行号和异常类型给前端诊断。
def _traceback_summary(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines)[-4000:]


# ====== 功能：识别合法 KiCad PCB S-expression，兼容 "(kicad_pcb" 和 "( kicad_pcb"。
def _is_kicad_board_text(text: Any) -> bool:
    return bool(re.match(r"(?is)^\s*\(\s*kicad_pcb\b", str(text or "")))


# ====== 功能：help_planner 失败时优先使用完整 stderr 中的真实原因。
def _help_planner_failure_reason(exc_text: str, diagnostics: dict[str, Any]) -> str:
    stderr = str(diagnostics.get("stderrSummary") or "").strip()
    if "cannot infer local target" in stderr.lower():
        return stderr
    if stderr:
        return f"{exc_text}\nstderr:\n{stderr}"
    return exc_text

# ====== 功能：校验 help_planner 是否拿到了真实 KiCad 输入，而不是 export.txt 路径。
def _help_planner_input_error(project_data: str, source_board_path: str) -> str:
    source = Path(str(source_board_path or "")) if source_board_path else None
    if source and source.is_file() and source.suffix.lower() == ".kicad_pcb":
        return ""
    text = str(project_data or "").strip()
    maybe_path = Path(text) if text and len(text) < 260 else None
    if maybe_path and maybe_path.suffix.lower() == ".kicad_pcb" and maybe_path.is_file():
        return ""
    if maybe_path and maybe_path.suffix.lower() == ".txt":
        return f"help_planner requires KiCad .kicad_pcb input; got PCB Builder/export txt path: {maybe_path}"
    if _is_kicad_board_text(text):
        return ""
    return "help_planner requires KiCad .kicad_pcb input; current projectData is not KiCad board text"

# ====== 功能：汇总 help_planner 失败时最关键的本地诊断路径。 ======
def _help_planner_diagnostics(work_dir: Path, pcbrouter_bin: Path, source_board_path: str) -> dict[str, Any]:
    run_dir = work_dir / "pcbrouter_local_completion"
    input_board = _first_existing(run_dir / "local_route_input.kicad_pcb", run_dir / "export.kicad_pcb", run_dir / "reroute_input.kicad_pcb") or run_dir / "local_route_input.kicad_pcb"
    input_csv = run_dir / "local_route_input.csv"
    output_board = _first_existing(run_dir / "output.kicad_pcb", run_dir / "routed.kicad_pcb", run_dir / "output_routed" / "export.kicad_pcb", run_dir / "export.kicad_pcb") or run_dir / "output.kicad_pcb"
    output_csv = _first_existing(run_dir / "output.csv", run_dir / "local_route_output.csv", run_dir / "result.csv") or run_dir / "output.csv"
    stdout_path = _first_existing(run_dir / "pcbrouter_stdout.log", run_dir / "pcbrouter.stdout.log") or run_dir / "pcbrouter_stdout.log"
    stderr_path = _first_existing(run_dir / "pcbrouter_stderr.log", run_dir / "pcbrouter.stderr.log")
    seed_path = _first_existing(run_dir / "seed_geometry.json")
    files: dict[str, Any] = {}
    for label, path in {"inputBoardPath": input_board, "inputCsvPath": input_csv, "outputBoardPath": output_board, "outputCsvPath": output_csv, "stdoutPath": stdout_path, "stderrPath": stderr_path}.items():
        files[label] = str(path)
        files[label + "Exists"] = path.exists()
        if path.exists():
            try:
                files[label + "Size"] = path.stat().st_size
            except OSError:
                pass
    if stdout_path.exists():
        files["stdoutSummary"] = stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    if stderr_path.exists():
        files["stderrSummary"] = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    return {"workDir": str(work_dir), "pcbrouterBin": str(pcbrouter_bin), "sourceBoardPath": source_board_path, **files}



def _archive_help_planner_repro_case(
    *,
    work_dir: Path,
    source_layout_path: str,
    source_board_path: str,
    input_board_path: str,
    input_csv_path: str,
    output_board_path: str,
    output_csv_path: str,
    import_lines_path: str,
    route_params: dict[str, Any] | Any,
    selected_nets: list[Any] | Any,
    missing_routes: list[Any] | Any,
    status: str,
    reason: str,
    command: str,
) -> dict[str, Any]:
    route_params = route_params if isinstance(route_params, dict) else {}
    selected_nets = selected_nets if isinstance(selected_nets, list) else []
    missing_routes = missing_routes if isinstance(missing_routes, list) else []
    repro_dir = work_dir / "repro_case"
    repro_dir.mkdir(parents=True, exist_ok=True)
    run_dir = work_dir / "pcbrouter_local_completion"
    stdout_path = _first_existing(run_dir / "pcbrouter_stdout.log", run_dir / "pcbrouter.stdout.log")
    stderr_path = _first_existing(run_dir / "pcbrouter_stderr.log", run_dir / "pcbrouter.stderr.log")
    seed_path = _first_existing(run_dir / "seed_geometry.json")

    missing_routes_kicad = route_params.get("missingRoutesKicad") if isinstance(route_params.get("missingRoutesKicad"), list) else []
    csv_header: list[str] = []
    csv_first_data_row: str = ""
    csv_path = Path(str(input_csv_path or ""))
    if csv_path.is_file():
        csv_lines = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
        csv_header = csv_lines[0].split(",") if csv_lines else []
        csv_first_data_row = csv_lines[1] if len(csv_lines) > 1 else ""
    manifest: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "command": command,
        "selectedNets": selected_nets,
        "missingRoutes": missing_routes,
        "missingRoutesKicad": missing_routes_kicad,
        "missingRoutesCount": len(missing_routes),
        "missingRoutesKicadCount": len(missing_routes_kicad),
        "hasRouteGeometry": any(isinstance(route, dict) and isinstance(route.get("start"), dict) and isinstance(route.get("end"), dict) for route in missing_routes_kicad),
        "csvMode": "kicad_geometry" if any(name in {"start_x", "end_x"} for name in csv_header) else "fixed_route_layer" if "route_layer" in csv_header else "net_only",
        "csvHeader": csv_header,
        "csvFirstDataRow": csv_first_data_row,
        "routeParams": route_params,
        "files": {},
    }
    if seed_path and seed_path.is_file():
        try:
            seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
            manifest["seedGeometryInjected"] = bool(seed_payload.get("seedGeometryInjected"))
            manifest["seedGeometryCount"] = int(seed_payload.get("seedGeometryCount") or len(seed_payload.get("seedGeometry") or []))
            manifest["seedGeometry"] = seed_payload.get("seedGeometry") or []
        except Exception as exc:
            manifest["seedGeometryError"] = str(exc)
    returned_paths: dict[str, Any] = {"reproCaseDir": str(repro_dir)}

    raw_routes_path = repro_dir / "missing_routes_raw.json"
    kicad_routes_path = repro_dir / "missing_routes_kicad.json"
    raw_routes_path.write_text(json.dumps(missing_routes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kicad_routes_path.write_text(json.dumps(missing_routes_kicad, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    file_specs = [
        ("sourceLayoutPath", source_layout_path, "deleted_export.txt"),
        ("sourceBoardPath", source_board_path, "source_board.kicad_pcb"),
        ("inputBoardPath", input_board_path, "local_route_input.kicad_pcb"),
        ("inputCsvPath", input_csv_path, "local_route_input.csv"),
        ("missingRoutesRawPath", str(raw_routes_path), "missing_routes_raw.json"),
        ("missingRoutesKicadPath", str(kicad_routes_path), "missing_routes_kicad.json"),
        ("stdoutPath", str(stdout_path or ""), "pcbrouter_stdout.log"),
        ("stderrPath", str(stderr_path or ""), "pcbrouter_stderr.log"),
        ("seedGeometryPath", str(seed_path or ""), "seed_geometry.json"),
        ("outputBoardPath", output_board_path, "output.kicad_pcb"),
        ("outputCsvPath", output_csv_path, "output.csv"),
        ("importLinesFilePath", import_lines_path, "line.out"),
    ]
    for key, raw_path, dest_name in file_specs:
        entry = _copy_repro_file(Path(str(raw_path or "")), repro_dir, dest_name)
        manifest["files"][key] = entry
        if entry.get("exists"):
            returned_paths[key] = entry.get("copiedPath") or entry.get("sourcePath")

    stderr_entry = manifest["files"].get("stderrPath") or {}
    stdout_entry = manifest["files"].get("stdoutPath") or {}
    if stderr_entry.get("copiedPath"):
        manifest["stderrSummary"] = Path(str(stderr_entry["copiedPath"])).read_text(encoding="utf-8", errors="replace")[-2000:]
    if stdout_entry.get("copiedPath"):
        manifest["stdoutSummary"] = Path(str(stdout_entry["copiedPath"])).read_text(encoding="utf-8", errors="replace")[-2000:]

    manifest_path = repro_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    returned_paths["reproManifestPath"] = str(manifest_path)
    return returned_paths


def _safe_archive_help_planner_repro_case(**kwargs: Any) -> dict[str, Any]:
    try:
        return _archive_help_planner_repro_case(**kwargs)
    except Exception as exc:
        return {"reproArchiveError": _traceback_summary(exc)}


def _copy_repro_file(source: Path, repro_dir: Path, dest_name: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"sourcePath": str(source), "exists": False}
    if not str(source) or not source.is_file():
        return entry
    repro_dir.mkdir(parents=True, exist_ok=True)
    target = repro_dir / dest_name
    try:
        shutil.copyfile(source, target)
        entry.update({"exists": True, "copiedPath": str(target), "size": target.stat().st_size})
    except Exception as exc:
        entry.update({"copyError": str(exc)})
    return entry


def _update_repro_manifest(manifest_path: Path, key: str, source: Path) -> None:
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.setdefault("files", {})
        dest_name = "line.out" if key == "importLinesFilePath" else key
        files[key] = _copy_repro_file(source, manifest_path.parent, dest_name)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return
def _ensure_reroute_kicad_input(project_root: Path, board_text: str, source_path: str, work_dir: Path) -> tuple[Path, str, str]:
    source = Path(str(source_path or "")) if source_path else None
    if source and source.is_file() and source.suffix.lower() == ".kicad_pcb":
        target = work_dir / source.name
        shutil.copyfile(source, target)
        return target, target.read_text(encoding="utf-8", errors="ignore"), str(source)
    if _is_kicad_board_text(board_text):
        target = work_dir / "reroute_input.kicad_pcb"
        target.write_text(board_text, encoding="utf-8")
        return target, board_text, str(source or target)

    txt_source = source if source and source.is_file() else work_dir / "reroute_input.txt"
    if not txt_source.exists():
        txt_source.write_text(board_text, encoding="utf-8")
    convert_mod = _load_module("_pcb_agent_langgraph_convert", project_root / "convert.py")
    output_dir = work_dir / "kicad"
    result = convert_mod.convert_one("txt_to_kicad", txt_source, output_dir, None)
    output_path = Path(str(result.get("output") or ""))
    if not output_path.is_file():
        raise RuntimeError(f"txt_to_kicad did not create output for {txt_source}")
    output_text = output_path.read_text(encoding="utf-8", errors="ignore")
    if not _is_kicad_board_text(output_text):
        raise RuntimeError(
            f"txt_to_kicad output is not KiCad .kicad_pcb text; "
            f"outputPath={output_path}; sourcePath={txt_source}; textPreview={output_text[:200]!r}"
        )
    return output_path, output_text, str(txt_source)


def _convert_missing_routes_to_kicad(project_root: Path, missing_routes: list[Any], work_dir: Path) -> dict[str, Any]:
    raw_path = work_dir / "missing_routes_raw.json"
    kicad_path = work_dir / "missing_routes_kicad.json"
    raw_path.write_text(json.dumps(missing_routes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes: list[str] = []
    converted: list[dict[str, Any]] = []
    if not missing_routes:
        kicad_path.write_text("[]\n", encoding="utf-8")
        return {"geometryConversionStatus": "empty", "geometryConversionNotes": ["missingRoutes is empty"], "missingRoutesKicad": [], "missingRoutesRawPath": str(raw_path), "missingRoutesKicadPath": str(kicad_path)}
    try:
        convert_mod = _load_module("_pcb_agent_langgraph_convert_geometry", project_root / "convert.py")
        dbu_to_mm = getattr(convert_mod, "dbu_to_mm")
        layer_txt_to_kicad = getattr(convert_mod, "layer_txt_to_kicad")
        origin_x = int(getattr(convert_mod, "OUTLINE_ONLY_ORIGIN_X", 0))
        origin_y = int(getattr(convert_mod, "OUTLINE_ONLY_ORIGIN_Y", 0))
    except Exception as exc:
        kicad_path.write_text("[]\n", encoding="utf-8")
        return {"geometryConversionStatus": "failed", "geometryConversionNotes": [f"convert.py helpers unavailable: {exc}"], "missingRoutesKicad": [], "missingRoutesRawPath": str(raw_path), "missingRoutesKicadPath": str(kicad_path)}

    for index, route in enumerate(missing_routes):
        if not isinstance(route, dict):
            notes.append(f"route[{index}] is not an object")
            continue
        converted_route = dict(route)
        route_notes: list[str] = []
        for key in ("start", "end"):
            point = route.get(key)
            if not isinstance(point, dict):
                route_notes.append(f"{key} missing")
                continue
            converted_point = dict(point)
            try:
                x_raw = float(point.get("x"))
                y_raw = float(point.get("y"))
            except (TypeError, ValueError):
                route_notes.append(f"{key} coordinate missing or non-numeric")
                converted_point["coordinateConversionStatus"] = "failed"
                converted_route[key] = converted_point
                continue
            converted_point["source_x"] = x_raw
            converted_point["source_y"] = y_raw
            converted_point["sourceCoordinateSystem"] = "pcb_builder_export_dbu"
            converted_point["outlineOnlyOriginApplied"] = True
            converted_point["origin_x"] = origin_x
            converted_point["origin_y"] = origin_y
            converted_point["x"] = round(float(dbu_to_mm(x_raw + origin_x)), 6)
            converted_point["y"] = round(float(dbu_to_mm(y_raw + origin_y)), 6)
            converted_point["coordinateSystem"] = "kicad_mm"
            raw_layer = str(point.get("layer") or route.get("route_layer") or route.get("layer") or "").strip()
            if raw_layer:
                converted_point["source_layer"] = raw_layer
                converted_point["layer"] = str(layer_txt_to_kicad(raw_layer))
            converted_route[key] = converted_point
        raw_route_layer = str(route.get("route_layer") or route.get("layer") or (route.get("start") or {}).get("layer") or (route.get("end") or {}).get("layer") or "").strip()
        if raw_route_layer:
            converted_route["source_route_layer"] = raw_route_layer
            converted_route["route_layer"] = str(layer_txt_to_kicad(raw_route_layer))
        if route_notes:
            converted_route["geometryConversionStatus"] = "partial"
            converted_route["geometryConversionNotes"] = route_notes
            notes.extend(f"route[{index}]: {note}" for note in route_notes)
        else:
            converted_route["geometryConversionStatus"] = "ok"
        converted.append(converted_route)
    status = "ok" if converted and not notes else "partial" if converted else "failed"
    kicad_path.write_text(json.dumps(converted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"geometryConversionStatus": status, "geometryConversionNotes": notes, "missingRoutesKicad": converted, "missingRoutesRawPath": str(raw_path), "missingRoutesKicadPath": str(kicad_path)}

def _nets_from_missing_routes(routes: list[Any]) -> list[str]:
    nets: list[str] = []
    for item in routes:
        if isinstance(item, dict):
            value = item.get("net") or item.get("net_name") or item.get("netName") or item.get("name")
        else:
            value = item
        net = str(value or "").strip()
        if net and net not in nets:
            nets.append(net)
    return nets


def _prepare_local_route_csv(project_root: Path, route_params: dict[str, Any], kicad_text: str, work_dir: Path) -> tuple[Path, str]:
    local_router = _load_module("_pcb_agent_langgraph_local_router_prepare", project_root / "tools" / "pcb_local_router.py")
    csv_path = local_router.write_local_route_csv(route_params=route_params, project_data=kicad_text, work_dir=work_dir)
    lines = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
    header = lines[0].strip().lower() if lines else ""
    if "start_x" in header and "end_x" in header:
        return csv_path, "kicad_geometry"
    return csv_path, "fixed_route_layer" if "route_layer" in header else "net_only"


def _write_helper_router_incremental_import_file(
    *,
    original_board_text: str,
    routed_board_path: Path,
    selected_nets: list[Any],
    work_dir: Path,
) -> tuple[str, list[str]]:
    if not routed_board_path.is_file():
        return "", [f"helper_router_routed_board_missing:{routed_board_path}"]
    routed_board_text = routed_board_path.read_text(encoding="utf-8", errors="ignore")
    original_segments = {_segment_diff_key(segment) for segment in _parse_kicad_segments(original_board_text)}
    selected_net_names = {str(item).strip() for item in selected_nets if str(item).strip()}
    changed_segments: list[dict[str, Any]] = []
    for segment in _parse_kicad_segments(routed_board_text):
        if _segment_diff_key(segment) in original_segments:
            continue
        if selected_net_names and str(segment.get("netName") or "") not in selected_net_names:
            continue
        changed_segments.append(segment)
    if not changed_segments:
        return "", ["helper_router_incremental_import_no_changed_segments"]

    import_dir = work_dir / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    output_path = import_dir / "helper_router_reroute_line.out"
    line_records = [_segment_to_line_out(segment) for segment in changed_segments]
    import_text = "\n".join(record for record in line_records if record) + "\n"
    passed, reason, stats = _validate_line_out_text(import_text)
    if not passed:
        return "", [f"helper_router_incremental_import_invalid:{reason}"]
    output_path.write_text(import_text, encoding="utf-8")
    return str(output_path), [f"generated_helper_router_incremental_line_out:{output_path}", f"lineCount:{stats.get('lineCount', 0)}"]


def _parse_kicad_segments(board_text: str) -> list[dict[str, Any]]:
    net_id_to_name = _kicad_net_id_to_name(board_text)
    segments: list[dict[str, Any]] = []
    for block in _extract_balanced_sexpr_blocks(board_text, "segment"):
        start = re.search(r"\(\s*start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        end = re.search(r"\(\s*end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        width = re.search(r"\(\s*width\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        layer = re.search(r"\(\s*layer\s+\"?([^\s\)\"]+)\"?\s*\)", block, re.IGNORECASE)
        net = re.search(r"\(\s*net\s+(\d+)\s*\)", block, re.IGNORECASE)
        if not (start and end and width and layer and net):
            continue
        net_id = net.group(1).strip()
        segments.append(
            {
                "x1": float(start.group(1)),
                "y1": float(start.group(2)),
                "x2": float(end.group(1)),
                "y2": float(end.group(2)),
                "width": float(width.group(1)),
                "layer": layer.group(1).strip(),
                "netId": net_id,
                "netName": net_id_to_name.get(net_id, net_id).replace("!", "_"),
            }
        )
    return segments


def _extract_balanced_sexpr_blocks(text: str, head: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    blocks: list[str] = []
    pattern = re.compile(rf"\(\s*{re.escape(head)}\b", re.IGNORECASE)
    for match in pattern.finditer(text):
        depth = 0
        in_string = False
        escaped = False
        for pos in range(match.start(), len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "(":
                depth += 1
                continue
            if char == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.start():pos + 1].strip())
                    break
    return blocks


def _kicad_net_id_to_name(board_text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pattern in (
        re.compile(r"\(\s*net\s+(\d+)\s+\"([^\"]+)\"\s*\)", re.IGNORECASE),
        re.compile(r"\(\s*net\s+(\d+)\s+([^\s\)]+)\s*\)", re.IGNORECASE),
    ):
        for match in pattern.finditer(board_text or ""):
            mapping[match.group(1).strip()] = match.group(2).strip()
    return mapping


def _segment_diff_key(segment: dict[str, Any]) -> tuple[Any, ...]:
    x1 = round(float(segment.get("x1") or 0), 6)
    y1 = round(float(segment.get("y1") or 0), 6)
    x2 = round(float(segment.get("x2") or 0), 6)
    y2 = round(float(segment.get("y2") or 0), 6)
    endpoints = tuple(sorted(((x1, y1), (x2, y2))))
    return (endpoints, str(segment.get("layer") or ""), round(float(segment.get("width") or 0), 6), str(segment.get("netName") or segment.get("netId") or ""))


def _segment_to_line_out(segment: dict[str, Any]) -> str:
    return (
        f"{_kicad_layer_to_line_out_layer(str(segment.get('layer') or ''))}!LINE!0!{str(segment.get('netName') or segment.get('netId') or '').replace('!', '_')}!"
        f"{_kicad_mm_to_line_out_coord_text(float(segment.get('x1') or 0), 'x')}!"
        f"{_kicad_mm_to_line_out_coord_text(float(segment.get('y1') or 0), 'y')}!"
        f"{_kicad_mm_to_line_out_coord_text(float(segment.get('x2') or 0), 'x')}!"
        f"{_kicad_mm_to_line_out_coord_text(float(segment.get('y2') or 0), 'y')}!"
        f"{_kicad_mm_to_mil_text(float(segment.get('width') or 0))}"
    )


def _kicad_layer_to_line_out_layer(layer: str) -> str:
    layer = str(layer or "").strip().strip('"')
    aliases = {"F.Cu": "TOP", "B.Cu": "BOTTOM", "Top": "TOP", "Bottom": "BOTTOM", "Conductor/Top": "TOP", "Conductor/Bottom": "BOTTOM"}
    if layer in aliases:
        return aliases[layer]
    if layer.startswith("Conductor/"):
        layer = layer.split("/", 1)[1]
    if layer.lower() == "top":
        return "TOP"
    if layer.lower() == "bottom":
        return "BOTTOM"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", layer).upper()


def _kicad_mm_to_line_out_coord_text(value: float, axis: str) -> str:
    origin = 363386.0 if axis == "x" else 534646.0
    dbu_mm = 0.000254
    local_mil = (float(value) / dbu_mm - origin) / 100.0
    return f"{local_mil:.2f}"


def _kicad_mm_to_mil_text(value: float) -> str:
    return f"{(float(value) / 0.0254):.2f}"


def _validate_line_out_text(text: str) -> tuple[bool, str, dict[str, Any]]:
    stripped = str(text or "").lstrip()
    if not stripped:
        return False, "增量导入文件为空", {"lineCount": 0}
    valid_count = 0
    for raw in stripped.splitlines():
        parts = [part.strip() for part in raw.split("!")]
        if len(parts) == 9 and parts[1].upper() == "LINE" and parts[0] and parts[3]:
            valid_count += 1
    if valid_count <= 0:
        return False, "增量导入文件未包含有效 LINE 记录", {"lineCount": 0}
    return True, "", {"lineCount": valid_count}

def _project_data_text(context: dict[str, Any]) -> str:
    project = context.get("projectData") or context.get("project_data")
    if isinstance(project, str):
        path = Path(project)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore")
        return project
    if isinstance(project, dict):
        for key in ("boardData", "data", "content", "layout", "projectData"):
            value = project.get(key)
            if isinstance(value, str) and value.strip():
                return value
        path_value = project.get("absolute_path") or project.get("filePath")
        if isinstance(path_value, str) and Path(path_value).exists():
            return Path(path_value).read_text(encoding="utf-8", errors="ignore")
    value = context.get("board_data")
    return value if isinstance(value, str) else ""


# ====== 功能：从参数或缓存中提取布线结果文本。 ======
def _routed_text(arguments: dict[str, Any], context: dict[str, Any]) -> str:
    for value in (arguments.get("routedText"), arguments.get("model_output_text"), context.get("routedText")):
        if isinstance(value, str) and value.strip():
            return value
    for item in (context.get("fanout_routeResult"), context.get("rerouteResult"), context.get("importLinesResult")):
        if isinstance(item, dict):
            for key in ("modelOutputText", "routedText", "content", "data"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            for key in ("importLinesFilePath", "routedLayoutTxtFilePath", "routingResult", "filePath"):
                path = item.get(key)
                if isinstance(path, str) and Path(path).exists():
                    return Path(path).read_text(encoding="utf-8", errors="ignore")
    return ""


# ====== 功能：从参数或缓存中提取完整 routed board 文件路径。 ======

# ====== 功能：记录当前 DRC 输入来自哪个缓存对象，便于前端和终端诊断。 ======
def _routed_input_source(arguments: dict[str, Any], context: dict[str, Any], routed_board_path: Path | None) -> str:
    for key in ("routedKicadFilePath", "routedBoardFilePath", "boardPath", "routedText", "model_output_text"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"arguments.{key}"
    for source_key in ("fanout_routeResult", "helpPlannerResult", "rerouteResult", "importLinesResult"):
        item = context.get(source_key)
        if not isinstance(item, dict):
            continue
        for key in ("routedKicadFilePath", "importLinesFilePath", "routedBoardDataFilePath", "routedLayoutKicadFilePath", "boardFilePath"):
            value = str(item.get(key) or "").strip()
            if routed_board_path and value and Path(value) == routed_board_path:
                return source_key
        for key in ("modelOutputText", "routedText", "content", "data"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return source_key
    return "unknown"

def _routed_board_path(arguments: dict[str, Any], context: dict[str, Any]) -> Path | None:
    for value in (arguments.get("routedKicadFilePath"), arguments.get("routedBoardFilePath"), arguments.get("boardPath")):
        if isinstance(value, str) and value.strip() and Path(value).exists():
            return Path(value)
    for item in (context.get("fanout_routeResult"), context.get("helpPlannerResult"), context.get("rerouteResult"), context.get("importLinesResult")):
        if isinstance(item, dict):
            for key in ("routedKicadFilePath", "importLinesFilePath", "routedBoardDataFilePath", "routedLayoutKicadFilePath", "boardFilePath"):
                value = str(item.get(key) or "").strip()
                if value and Path(value).suffix.lower() == ".kicad_pcb" and Path(value).exists():
                    return Path(value)
    return None


# ====== 功能：调用迁移来的 router 输出转换工具生成 routed layout/KiCad 文件。 ======
def _convert_router_output(project_root: Path, original_board_text: str, work_dir: Path, router_type: str, output_file: Path) -> dict[str, Any]:
    try:
        module = _load_module("_pcb_agent_langgraph_router_output", project_root / "tools" / "pcb_router_output.py")
        result = module.convert_router_output_to_layout(
            project_root=project_root,
            original_board_text=original_board_text,
            work_dir=work_dir,
            router_type=router_type,
            import_lines_path=output_file,
        )
        return {
            "routedLayoutTxtFilePath": str(result.routing_input_path),
            "routedKicadFilePath": str(result.routed_kicad_path or ""),
            "wireCount": result.wire_count,
            "notes": result.notes,
        }
    except Exception as exc:
        return {"routedLayoutTxtFilePath": str(output_file), "routedKicadFilePath": "", "wireCount": 0, "notes": [f"router_output_conversion_failed:{exc}"]}



# ====== 功能：从上下文中查找原始板文件路径。 ======
def _source_board_path(context: dict[str, Any]) -> str:
    for item in (context.get("deleteTracesResult"), context.get("projectData"), context.get("project_data")):
        if isinstance(item, dict):
            for key in ("absolute_path", "filePath", "boardFilePath", "originalBoardDataFilePath"):
                value = str(item.get(key) or "").strip()
                if value:
                    return value
    return ""


# ====== 功能：为 help_planner 汇总局部布线参数。 ======
def _help_route_params(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    reroute_input = context.get("rerouteInput") if isinstance(context.get("rerouteInput"), dict) else {}
    for key in ("missingRoutesKicad", "missingRoutes", "geometryConversionStatus", "geometryConversionNotes"):
        value = reroute_input.get(key) or context.get(key)
        if value not in (None, "", [], {}):
            params[key] = value
    for item in (reroute_input, context.get("rerouteResult"), context.get("rerouteContext"), context.get("deleteTracesResult"), {"selectedNets": context.get("selectedNets"), "missingRoutes": context.get("missingRoutes")}, context.get("fanoutParams"), arguments):
        if isinstance(item, dict):
            for key in ("orderLines", "nets", "selectedNets", "localRouteNets", "pcbrouterNets", "pcbrouterCsvPath", "localRouteCsvPath"):
                value = item.get(key)
                if value not in (None, "", [], {}):
                    params[key] = value
    if params.get("missingRoutesKicad"):
        params["orderLines"] = params["missingRoutesKicad"]
        params["pcbrouterNets"] = params["missingRoutesKicad"]
    elif params.get("missingRoutes"):
        params["orderLines"] = params["missingRoutes"]
        params["pcbrouterNets"] = params["missingRoutes"]
    if not any(params.get(key) for key in ("orderLines", "nets", "selectedNets", "localRouteNets", "pcbrouterNets")):
        nets = []
        for item in (context.get("deleteTracesResult"), context.get("rerouteContext"), {"selectedNets": context.get("selectedNets"), "missingRoutes": context.get("missingRoutes")}, context.get("rerouteResult")):
            if isinstance(item, dict):
                raw = item.get("selectedNets") or item.get("missingRoutes") or item.get("missing_routes")
                if isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict):
                            net = str(entry.get("net") or entry.get("net_name") or entry.get("netName") or "").strip()
                        else:
                            net = str(entry or "").strip()
                        if net and net not in nets:
                            nets.append(net)
        if nets:
            params["nets"] = nets
    return params


# ====== 功能：解析可解释性模型需要的 kicad_pcb 输入文件。 ======
def _explain_input_board(arguments: dict[str, Any], context: dict[str, Any]) -> Path | None:
    for value in (arguments.get("input"), arguments.get("filePath"), arguments.get("boardPath")):
        if isinstance(value, str) and value.strip():
            return Path(value)
    drc_result = context.get("drcResult") if isinstance(context.get("drcResult"), dict) else {}
    detail = drc_result.get("detail") if isinstance(drc_result.get("detail"), dict) else {}
    filled_path = detail.get("filled_board_data_file_path") or drc_result.get("filledBoardDataFilePath")
    if isinstance(filled_path, str) and filled_path.strip():
        return Path(filled_path)
    for item in (context.get("helpPlannerResult"), context.get("rerouteResult"), context.get("fanout_routeResult"), context.get("importLinesResult"), context.get("projectData")):
        if isinstance(item, dict):
            for key in ("routedKicadFilePath", "routedLayoutKicadFilePath", "routedBoardDataFilePath", "boardFilePath", "routedLayoutTxtFilePath", "routingResult", "importLinesFilePath", "absolute_path", "filePath"):
                value = str(item.get(key) or "").strip()
                if value and Path(value).suffix.lower() == ".kicad_pcb":
                    return Path(value)
    return None


def _drc_context_passed(context: dict[str, Any]) -> bool:
    drc_result = context.get("drcResult") if isinstance(context.get("drcResult"), dict) else {}
    return drc_result.get("passed") is True and str(drc_result.get("status", "")).lower() not in {"failed", "error"}


async def _run_explainability_smoke(config: AppConfig, context: dict[str, Any]) -> dict[str, Any]:
    if not config.explain_model.enabled:
        return {"status": "skipped", "reason": "explain model is disabled"}
    python_exe = _resolve_path(config.root, config.explain_model.python_executable)
    code_dir = _resolve_path(config.root, config.explain_model.code_dir)
    checkpoint = _resolve_path(config.root, config.explain_model.checkpoint_path)
    smoke_board = _first_existing(
        config.root / "vendor" / "AI-PCB-Eval" / "sample_batch" / "incomplete" / "demo-1.kicad_pcb",
        config.root / "vendor" / "AI-PCB-Eval" / "sample_batch" / "incomplete" / "demo-2.kicad_pcb",
    )
    work_dir = _resolve_path(config.root, config.router.work_dir) / str(context.get("session_id") or "session") / "explainability_smoke"
    work_dir.mkdir(parents=True, exist_ok=True)
    if not python_exe.exists():
        return {"status": "skipped", "reason": "explain python executable does not exist", "python": str(python_exe)}
    if not code_dir.exists():
        return {"status": "skipped", "reason": "explain code_dir does not exist", "code_dir": str(code_dir)}
    if not checkpoint.exists():
        return {"status": "skipped", "reason": "explain checkpoint does not exist", "checkpoint": str(checkpoint)}
    if not smoke_board or not smoke_board.is_file():
        return {"status": "skipped", "reason": "known-good smoke .kicad_pcb case is missing"}
    command = [str(python_exe), str(code_dir / "infer_ascend_multiview_classifier.py"), str(smoke_board), str(checkpoint)]
    try:
        completed = await _run_command(command, code_dir, config.explain_model.timeout_seconds)
        source_report = code_dir / "inference_runs" / smoke_board.stem / "report.txt"
        source_prediction = code_dir / "inference_runs" / smoke_board.stem / "prediction.json"
        report_path = work_dir / "report.txt"
        prediction_path = work_dir / "prediction.json"
        if source_report.is_file():
            shutil.copyfile(source_report, report_path)
        else:
            report_path.write_text(str(completed.get("stdout") or ""), encoding="utf-8")
        if source_prediction.is_file():
            shutil.copyfile(source_prediction, prediction_path)
        return {
            "status": "ok" if completed.get("returncode") == 0 else "failed",
            "reason": "" if completed.get("returncode") == 0 else "explainability smoke command failed",
            "case_path": str(smoke_board),
            "report_path": str(report_path),
            "prediction_json_path": str(prediction_path) if prediction_path.exists() else "",
            "command": command,
            "returncode": completed.get("returncode"),
            "stdout": str(completed.get("stdout") or "")[-1600:],
            "stderr": str(completed.get("stderr") or "")[-1600:],
        }
    except Exception as exc:
        return {"status": "failed", "reason": str(exc), "tracebackSummary": _traceback_summary(exc), "command": command, "case_path": str(smoke_board)}

# ====== 功能：合并用户和上下文中的布线约束。 ======
def _merge_constraints(*items: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            merged.update({key: value for key, value in item.items() if value not in (None, "")})
    return merged


# ====== 功能：从板数据中提取可能的 BGA/器件编号。 ======
def _extract_refdes_candidates(board_data: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in board_data.replace('"', " ").replace("'", " ").replace(",", " ").split():
        upper = token.upper().strip(":;()[]{}")
        if len(upper) < 2 or upper in seen:
            continue
        if (upper[0] in {"U", "B"} and upper[1:].isdigit()) or "BGA" in upper:
            seen.add(upper)
            candidates.append({"refdes": upper, "kind": "bga_candidate"})
    return candidates[:50]


# ====== 功能：从参数或缓存中读取 fanout 参数。 ======
def _fanout_params(arguments: dict[str, Any], context: dict[str, Any]) -> Any:
    return arguments.get("fanoutParams") or context.get("fanoutParams") or context.get("escapeOrderResult", {}).get("fanoutParams") or context.get("layerAssignResult", {}).get("fanoutParams")


# ====== 功能：选择执行项目内 Python 脚本的解释器。 ======
def _python_executable(config: AppConfig) -> Path:
    configured = _resolve_path(config.root, config.explain_model.python_executable)
    if configured.exists():
        return configured
    current = Path(sys.executable)
    if current.exists() and current.suffix.lower() == ".exe" and current.name.lower().startswith("python"):
        return current
    return configured


# ====== 功能：把相对路径解析为项目内绝对路径。 ======
def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


# ====== 功能：按命令行规则拆分外部工具命令。 ======
def _split_command(command: str) -> list[str]:
    if not command.strip():
        return []
    import shlex

    return shlex.split(command, posix=False)


# ====== 功能：把命令行中的可执行文件解析到项目根目录。 ======
def _resolve_command_program(command: list[str], root: Path) -> list[str]:
    if not command:
        return command
    program = Path(command[0])
    if program.is_absolute():
        return command
    candidate = root / program
    if candidate.exists():
        return [str(candidate), *command[1:]]
    return command


# ====== 功能：在线程中执行外部命令并截取输出。 ======
async def _run_command(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    # ====== 功能：同步执行外部命令的内部函数。 ======
    def run() -> dict[str, Any]:
        completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, shell=False)
        return {"returncode": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}

    return await asyncio.to_thread(run)


# ====== 功能：生成默认 layer/order 输入内容。 ======
def _default_layer_input(selected_bga: str, constraints: dict[str, Any]) -> str:
    lines = [selected_bga.upper()]
    for key, value in constraints.items():
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


# ====== 功能：根据 router 类型计算默认输出文件路径。 ======
def _route_output_path(work_dir: Path, router_type: str) -> Path:
    return work_dir / ("ARC_output.txt" if router_type == "rule_arc" else "line.out")


# ====== 功能：读取文本文件中的非空行。 ======
def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.rstrip("\n") for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]



# ====== 功能：解析模型可能返回的 JSON 对象。 ======
def _loads_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:].strip() if text.lower().startswith("json") else text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


# ====== 功能：返回存在的路径对象。 ======
def _existing_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path if path.exists() else None


# ====== 功能：按旧 Hermes 优先级读取本轮 fanout router 报告。 ======
def _read_fanout_router_report(work_dir: Path) -> str:
    for name in ("data.txt", "statistical.out", "statistical.txt", "report.txt", "route_report.txt"):
        path = work_dir / name
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        for encoding in ("utf-8", "gbk", "gb18030"):
            try:
                text = path.read_text(encoding=encoding).strip()
                if text:
                    return text
            except UnicodeDecodeError:
                continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return "布线完成（无详细报告）"


# ====== 功能：按 router family 选择前端 importLines 应导入的原始记录文件。 ======
def _fanout_import_file(work_dir: Path, router_type: str, output_path: Path) -> Path | None:
    normalized = normalize_router_type(router_type) or router_type
    if normalized == "rule_arc":
        return _first_existing(work_dir / "ARC_output.txt", work_dir / "arc_output.txt", output_path)
    return _first_existing(work_dir / "line.out", output_path)


# ====== 功能：构造主模型 reroute 输入，包含上一轮 DRC 反馈。 ======

# ====== 功能：提取主模型输出中的 KiCad segment 坐标。
def _reroute_model_segments(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for block in _extract_balanced_sexpr_blocks(str(text or ""), "segment"):
        start = re.search(r"\(\s*start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        end = re.search(r"\(\s*end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        layer = re.search(r"\(\s*layer\s+\"?([^\s\)\"]+)\"?\s*\)", block, re.IGNORECASE)
        if start and end:
            segments.append({"start": [float(start.group(1)), float(start.group(2))], "end": [float(end.group(1)), float(end.group(2))], "layer": layer.group(1) if layer else ""})
    return segments


# ====== 功能：校验模型输出是否使用了 missingRoutesKicad 的 KiCad 坐标。
def _validate_reroute_model_coordinates(routed_text: str, missing_routes_kicad: list[Any]) -> dict[str, Any]:
    routes = [route for route in missing_routes_kicad if isinstance(route, dict)] if isinstance(missing_routes_kicad, list) else []
    if not routes:
        return {"passed": True, "reason": "no missingRoutesKicad available for coordinate check"}
    segments = _reroute_model_segments(routed_text)
    if not segments:
        return {"passed": True, "reason": "no segment coordinates in model output"}
    expected_points: list[tuple[float, float]] = []
    raw_points: list[tuple[float, float]] = []
    for route in routes:
        for key in ("start", "end"):
            point = route.get(key) if isinstance(route.get(key), dict) else {}
            try:
                expected_points.append((float(point.get("x")), float(point.get("y"))))
                if point.get("source_x") is not None and point.get("source_y") is not None:
                    raw_points.append((float(point.get("source_x")), float(point.get("source_y"))))
            except (TypeError, ValueError):
                continue
    if not expected_points:
        return {"passed": True, "reason": "missingRoutesKicad has no numeric points"}
    output_points = [(float(seg[side][0]), float(seg[side][1])) for seg in segments for side in ("start", "end") if isinstance(seg.get(side), list) and len(seg.get(side)) == 2]
    tolerance_mm = 2.0
    expected_hits = sum(1 for point in output_points if any(math.hypot(point[0] - exp[0], point[1] - exp[1]) <= tolerance_mm for exp in expected_points))
    raw_hits = sum(1 for point in output_points if any(math.hypot(point[0] - raw[0], point[1] - raw[1]) <= tolerance_mm for raw in raw_points))
    passed = expected_hits > 0 and expected_hits >= max(1, len(output_points) // 2) and raw_hits == 0
    return {
        "passed": passed,
        "reason": "model output uses raw PCB Builder/export coordinates instead of missingRoutesKicad KiCad mm coordinates" if raw_hits else "model output coordinates are not near missingRoutesKicad KiCad mm targets",
        "expectedHits": expected_hits,
        "rawHits": raw_hits,
        "outputPointCount": len(output_points),
        "expectedPoints": expected_points[:4],
        "outputPoints": output_points[:4],
    }

def _reroute_model_payload(arguments: dict[str, Any], context: dict[str, Any], reroute_input: dict[str, Any], attempt: int) -> dict[str, Any]:
    delete_result = context.get("deleteTracesResult") if isinstance(context.get("deleteTracesResult"), dict) else {}
    kicad_text = str(reroute_input.get("kicadBoardText") or "")
    missing_routes = reroute_input.get("missingRoutes") or context.get("missingRoutes") or delete_result.get("missing_routes") or []
    missing_routes_kicad = reroute_input.get("missingRoutesKicad") or context.get("missingRoutesKicad") or []
    return {
        "task": "pcb_local_reroute",
        "inputFormat": "kicad_pcb",
        "coordinateContract": "Use only missingRoutesKicad coordinates in KiCad millimeters when producing segment start/end. Do not use raw PCB Builder/export coordinates from missingRoutes.",
        "attempt": attempt,
        "instruction": "根据 missingRoutesKicad 补全局部重布；输出 KiCad segment 时只能使用 KiCad mm 坐标。如有上一轮 DRC/coordinate feedback，请修正后重新输出。",
        "missingRoutes": missing_routes,
        "missingRoutesKicad": missing_routes_kicad,
        "selectedNets": reroute_input.get("selectedNets") or context.get("selectedNets") or [],
        "rerouteContext": context.get("rerouteContext") or {},
        "kicadBoardPreview": kicad_text[:12000],
        "kicadBoardPath": reroute_input.get("kicadBoardPath") or "",
        "previousRerouteResult": context.get("lastRerouteResult") or context.get("rerouteResult") or {},
        "drcFeedbackHistory": context.get("rerouteDrcFeedbackHistory") or [],
        "lastDrcResult": context.get("lastDrcResult") or context.get("drcResult") or {},
        "lastExplainabilityReport": context.get("explainabilityReport") or {},
        "arguments": arguments,
    }


# ====== 功能：将 DRC 和解释结果压缩为下一轮模型可读反馈。 ======
def _reroute_feedback_summary(cache: dict[str, Any]) -> dict[str, Any]:
    drc = cache.get("drcResult") if isinstance(cache.get("drcResult"), dict) else {}
    explain = cache.get("explainabilityReport") if isinstance(cache.get("explainabilityReport"), dict) else {}
    return {
        "drcStatus": drc.get("status"),
        "drcPassed": drc.get("passed") is True,
        "errors": drc.get("errors") or [],
        "failureSummary": (drc.get("detail") or {}).get("failure_summary") if isinstance(drc.get("detail"), dict) else drc.get("reason"),
        "explainStatus": explain.get("status"),
        "explainReason": explain.get("reason") or str(explain.get("report") or "")[:1200],
    }

# ====== 功能：返回第一个存在且非空的文件。 ======
def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            return path
    return None


# ====== 功能：生成 router 输出文件摘要。 ======
def _router_report(output_file: Path, completed: dict[str, Any]) -> dict[str, Any]:
    return {"outputFile": str(output_file), "outputBytes": output_file.stat().st_size, "returncode": completed.get("returncode")}


# ====== 功能：针对局部 reroute 的目标网络计算 DRC 通过状态，同时保留全板残留错误。
def _target_scoped_drc_result(drc_result: dict[str, Any], context: dict[str, Any], *, drc_execution_valid: bool = True) -> dict[str, Any]:
    issues = ((drc_result.get("artifacts") or {}).get("issues") or []) if isinstance(drc_result, dict) else []
    issue_dicts = [_compact_drc_issue(issue) for issue in issues]
    target_nets = _target_nets_from_context(context)
    details = drc_result.get("details") if isinstance(drc_result, dict) and isinstance(drc_result.get("details"), dict) else {}
    full_rule_counts = details.get("hard_rule_counts") or {}
    full_issue_count = int(details.get("hard_issue_count") if details.get("hard_issue_count") is not None else len(issue_dicts))
    if not drc_execution_valid:
        return {
            "targetNets": target_nets,
            "targetScoped": False,
            "targetScopedPassed": False,
            "targetIssueCount": full_issue_count,
            "fullBoardIssueCount": full_issue_count,
            "fullBoardRuleCounts": full_rule_counts,
            "targetDrcIssues": issue_dicts[:50],
            "fullBoardResidualIssues": [],
            "targetFailureSummary": "DRC did not produce a valid checked board; target-scoped pass is not allowed.",
        }
    if not target_nets:
        return {
            "targetNets": [],
            "targetScoped": False,
            "targetIssueCount": full_issue_count,
            "fullBoardIssueCount": full_issue_count,
            "fullBoardRuleCounts": full_rule_counts,
            "targetDrcIssues": issue_dicts[:50],
            "fullBoardResidualIssues": [],
            "targetFailureSummary": "No selected reroute target nets were available for target-scoped DRC.",
        }
    target_keys = {net.lower() for net in target_nets}
    target_issues = [issue for issue in issue_dicts if _issue_mentions_target_net(issue, target_keys)]
    residual_issues = [issue for issue in issue_dicts if not _issue_mentions_target_net(issue, target_keys)]
    target_rule_counts: dict[str, int] = {}
    for issue in target_issues:
        rule = str(issue.get("rule") or "unknown")
        target_rule_counts[rule] = target_rule_counts.get(rule, 0) + 1
    target_passed = bool(drc_result.get("ok", True)) and len(target_issues) == 0
    return {
        "targetNets": target_nets,
        "targetScoped": True,
        "targetScopedPassed": target_passed,
        "targetIssueCount": len(target_issues),
        "targetRuleCounts": target_rule_counts,
        "targetDrcIssues": target_issues[:50],
        "fullBoardIssueCount": full_issue_count,
        "fullBoardRuleCounts": full_rule_counts,
        "fullBoardResidualIssueCount": len(residual_issues),
        "fullBoardResidualIssues": residual_issues[:50],
        "targetFailureSummary": _target_failure_summary(target_issues, target_nets),
    }


def _drc_execution_valid(payload: dict[str, Any], drc_result: dict[str, Any]) -> bool:
    if not isinstance(drc_result, dict) or drc_result.get("ok") is not True:
        return False
    fill_detail = payload.get("fill_detail") if isinstance(payload.get("fill_detail"), dict) else {}
    if fill_detail.get("reason") == "fill_failed":
        return False
    filled_path = str(payload.get("filled_board_data_file_path") or "").strip()
    if not filled_path:
        return False
    return Path(filled_path).exists()

def _target_failure_summary(target_issues: list[dict[str, Any]], target_nets: list[str]) -> str:
    if not target_issues:
        return ""
    counts: dict[str, int] = {}
    for issue in target_issues:
        rule = str(issue.get("rule") or "unknown")
        counts[rule] = counts.get(rule, 0) + 1
    return f"target_nets={target_nets}; target_issue_count={len(target_issues)}; target_rule_counts={json.dumps(counts, ensure_ascii=False)}"


def _compact_drc_issue(issue: Any) -> dict[str, Any]:
    if isinstance(issue, dict):
        message = issue.get("message") or issue.get("description") or ""
        return {"rule": issue.get("rule"), "severity": issue.get("severity"), "message": message}
    return {"rule": "unknown", "severity": "", "message": str(issue)}


def _issue_mentions_target_net(issue: dict[str, Any], target_nets: set[str]) -> bool:
    text = json.dumps(issue, ensure_ascii=False).lower()
    return any(net and net in text for net in target_nets)


def _target_nets_from_context(context: dict[str, Any]) -> list[str]:
    nets: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            nets.append(value.strip())
        elif isinstance(value, dict):
            for key in ("net", "netName", "net_name", "name"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    nets.append(item.strip())
                    break
        elif isinstance(value, list):
            for item in value:
                add(item)

    for source in (context, context.get("rerouteInput"), context.get("deleteTracesResult"), context.get("rerouteContext"), context.get("rerouteResult"), context.get("helpPlannerResult")):
        if not isinstance(source, dict):
            continue
        for key in ("selectedNets", "nets", "localRouteNets", "missingRoutes", "missingRoutesKicad", "pcbrouterNets", "orderLines"):
            add(source.get(key))
        route_params = source.get("routeParams")
        if isinstance(route_params, dict):
            for key in ("selectedNets", "nets", "localRouteNets", "missingRoutes", "missingRoutesKicad", "pcbrouterNets", "orderLines"):
                add(route_params.get(key))
    unique: list[str] = []
    seen: set[str] = set()
    for net in nets:
        key = net.lower()
        if key not in seen:
            seen.add(key)
            unique.append(net)
    return unique

# ====== 功能：从文件路径动态加载 Python 模块。 ======
def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ====== 功能：把 dataclass 或 dict 统一转换为字典。 ======
def _dataclass_to_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return {"value": str(value)}


# ====== 功能：根据输入结果生成摘要报告。 ======
def _build_report(drc_result: Any) -> str:
    if not isinstance(drc_result, dict) or not drc_result:
        return "DRC result is not available; run drc_check first."
    status = drc_result.get("status")
    passed = drc_result.get("passed")
    errors = drc_result.get("errors") or []
    if passed:
        return "DRC passed. No hard-rule violations were reported."
    return f"DRC status={status}; errors={json.dumps(errors, ensure_ascii=False)}"
