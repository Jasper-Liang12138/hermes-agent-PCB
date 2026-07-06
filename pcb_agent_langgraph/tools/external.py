from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from pcb_agent_langgraph.models.pcb_model import PCBModel
from pcb_agent_langgraph.planner.intent_entities import normalize_router_type
from pcb_agent_langgraph.tools.reroute_context import board_text_from_payload, build_reroute_context, target_nets_from_context
from pcb_agent_langgraph.utils.config import AppConfig


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
    async def _compress_reroute_context(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        delete_result = context.get("deleteTracesResult") if isinstance(context.get("deleteTracesResult"), dict) else {}
        local_context = delete_result.get("localContext") if isinstance(delete_result.get("localContext"), dict) else context.get("localContext") or {}
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
            return {"status": "unavailable", "tool": self.name, "reason": "reroute model is not configured"}
        project_data = _project_data_text(context)
        if not project_data:
            return {"status": "failed", "tool": self.name, "reason": "missing board data for reroute"}
        attempt = int(arguments.get("attempt") or 0) or int(context.get("rerouteAttemptCount") or 0) + 1
        work_dir = self._work_dir(context) / "reroute" / f"attempt_{attempt}"
        work_dir.mkdir(parents=True, exist_ok=True)
        prompt_payload = _reroute_model_payload(arguments, context, project_data, attempt)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 PCB 局部拆线重布模型。根据 missing routes、压缩版图上下文和 DRC 反馈，"
                    "输出本轮可用于 DRC 检查的 reroute 结果。优先返回 JSON；可包含 "
                    "routingResult/importLinesFilePath/routedLayoutTxtFilePath/report/content 字段。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
        ]
        try:
            model = PCBModel(self.config.model)
            model_result = await asyncio.to_thread(model.complete, messages)
        except Exception as exc:
            return {"status": "unavailable", "tool": self.name, "reason": f"reroute model call failed: {exc}", "attempt": attempt}
        parsed = _loads_json_object(model_result.content) or {"content": model_result.content}
        routed_text = _first_text(parsed.get("routedText"), parsed.get("content"), parsed.get("report"), model_result.content)
        output_path = work_dir / "reroute_output.txt"
        output_path.write_text(routed_text, encoding="utf-8")
        import_path = _existing_path(parsed.get("importLinesFilePath")) or _existing_path(parsed.get("routedLayoutTxtFilePath")) or _existing_path(parsed.get("routingResult")) or output_path
        return {
            "status": "ok",
            "tool": self.name,
            "attempt": attempt,
            "routingResult": str(parsed.get("routingResult") or output_path),
            "routedLayoutTxtFilePath": str(parsed.get("routedLayoutTxtFilePath") or import_path),
            "importLinesFilePath": str(parsed.get("importLinesFilePath") or import_path),
            "routedText": routed_text,
            "report": str(parsed.get("report") or "模型 reroute 已生成候选结果。"),
            "modelRaw": parsed,
            "workDir": str(work_dir),
            "elapsedMs": model_result.elapsed_ms,
        }

    # ====== 功能：调用兜底局部规则布线器 help_planner。 ======
    async def _run_help_planner(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        if not self.config.reroute_help.enabled:
            return {"status": "failed", "tool": self.name, "reason": "reroute help_planner is disabled in config"}
        project_data = _project_data_text(context)
        route_params = _help_route_params(arguments, context)
        if not project_data:
            return {"status": "failed", "tool": self.name, "reason": "missing board data for help_planner"}
        work_dir = self._work_dir(context) / "help_planner"
        pcbrouter_bin = _resolve_path(self.config.root, self.config.router.pcbrouter_bin)
        source_board_path = _source_board_path(context)
        input_error = _help_planner_input_error(project_data, source_board_path)
        if input_error:
            diagnostics = _help_planner_diagnostics(work_dir, pcbrouter_bin, source_board_path)
            return {"status": "failed", "tool": self.name, "reason": input_error, "routeParams": route_params, **diagnostics}
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
            return {
                "status": "ok",
                "tool": self.name,
                "routingResult": routing_path,
                "routedLayoutTxtFilePath": routing_path,
                "importLinesFilePath": routing_path,
                "report": payload.get("report") or "pcbrouter local route completed",
                "detail": payload,
                "workDir": str(work_dir),
            }
        except Exception as exc:
            diagnostics = _help_planner_diagnostics(work_dir, pcbrouter_bin, source_board_path)
            return {"status": "failed", "tool": self.name, "reason": str(exc), "routeParams": route_params, **diagnostics}


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

        original_board = _project_data_text(context)
        routed_text = _routed_text(arguments, context)
        if not original_board:
            return {"status": "failed", "tool": self.name, "reason": "missing original board data for DRC"}
        if not routed_text:
            return {"status": "failed", "tool": self.name, "reason": "missing routed output/import lines for DRC"}

        try:
            module = _load_module("_pcb_agent_langgraph_drc_tool", tool_path)
            if hasattr(module, "set_eval_root"):
                module.set_eval_root(eval_root)
            routed_board_path = _routed_board_path(arguments, context)
            if routed_board_path and routed_board_path.suffix.lower() == ".kicad_pcb" and routed_board_path.exists() and hasattr(module, "validate_kicad_board_with_drc"):
                attempt = module.validate_kicad_board_with_drc(
                    board_path=routed_board_path,
                    sample_id=str(context.get("session_id") or "session"),
                    iteration=1,
                )
            else:
                attempt = module.validate_kicad_patch_with_drc(
                    original_board_data=original_board,
                    model_output_text=routed_text,
                    output_dir=_resolve_path(self.config.root, self.config.drc.work_dir) / str(context.get("session_id") or "session"),
                    sample_id=str(context.get("session_id") or "session"),
                    iteration=1,
                )
            payload = _dataclass_to_dict(attempt)
            passed = bool(payload.get("passed"))
            drc_result = payload.get("drc_result") or {}
            return {
                "status": "ok" if passed else "failed",
                "tool": self.name,
                "passed": passed,
                "errors": [] if passed else [payload.get("failure_summary") or "DRC failed"],
                "score": 1.0 if passed else 0.0,
                "detail": payload,
                "tool_path": str(tool_path),
                "eval_root": str(eval_root),
                "drc_result": drc_result,
            }
        except Exception as exc:
            return {"status": "failed", "tool": self.name, "reason": str(exc), "tool_path": str(tool_path), "eval_root": str(eval_root)}



    # ====== 功能：调用可解释性模型 runtime 并读取报告。 ======
    async def _run_explain_model(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if not self.config.explain_model.enabled:
            return {"status": "failed", "tool": self.name, "reason": "explain model is disabled"}
        python_exe = _resolve_path(self.config.root, self.config.explain_model.python_executable)
        code_dir = _resolve_path(self.config.root, self.config.explain_model.code_dir)
        checkpoint = _resolve_path(self.config.root, self.config.explain_model.checkpoint_path)
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
    if re.search(r"(?is)^\s*\(\s*kicad_pcb\b", text):
        return ""
    return "help_planner requires KiCad .kicad_pcb input; current projectData is not KiCad board text"

# ====== 功能：汇总 help_planner 失败时最关键的本地诊断路径。 ======
def _help_planner_diagnostics(work_dir: Path, pcbrouter_bin: Path, source_board_path: str) -> dict[str, Any]:
    run_dir = work_dir / "pcbrouter_local_completion"
    input_board = run_dir / "local_route_input.kicad_pcb"
    input_csv = run_dir / "local_route_input.csv"
    stdout_path = run_dir / "pcbrouter_stdout.log"
    stderr_path = run_dir / "pcbrouter_stderr.log"
    files: dict[str, Any] = {}
    for label, path in {"inputBoardPath": input_board, "inputCsvPath": input_csv, "stdoutPath": stdout_path, "stderrPath": stderr_path}.items():
        files[label] = str(path)
        files[label + "Exists"] = path.exists()
        if path.exists():
            try:
                files[label + "Size"] = path.stat().st_size
            except OSError:
                pass
    if stdout_path.exists():
        files["stdoutSummary"] = stdout_path.read_text(encoding="utf-8", errors="replace")[:1600]
    if stderr_path.exists():
        files["stderrSummary"] = stderr_path.read_text(encoding="utf-8", errors="replace")[:1600]
    return {"workDir": str(work_dir), "pcbrouterBin": str(pcbrouter_bin), "sourceBoardPath": source_board_path, **files}

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
            for key in ("importLinesFilePath", "routedLayoutTxtFilePath", "routingResult", "filePath"):
                path = item.get(key)
                if isinstance(path, str) and Path(path).exists():
                    return Path(path).read_text(encoding="utf-8", errors="ignore")
            for key in ("routedText", "content", "data"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return ""


# ====== 功能：从参数或缓存中提取完整 routed board 文件路径。 ======
def _routed_board_path(arguments: dict[str, Any], context: dict[str, Any]) -> Path | None:
    for value in (arguments.get("routedKicadFilePath"), arguments.get("routedBoardFilePath"), arguments.get("boardPath")):
        if isinstance(value, str) and value.strip() and Path(value).exists():
            return Path(value)
    for item in (context.get("fanout_routeResult"), context.get("rerouteResult"), context.get("helpPlannerResult"), context.get("importLinesResult")):
        if isinstance(item, dict):
            for key in ("routedKicadFilePath", "routedBoardDataFilePath", "routedLayoutKicadFilePath", "boardFilePath"):
                value = str(item.get(key) or "").strip()
                if value and Path(value).exists():
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
    for item in (context.get("rerouteResult"), context.get("rerouteContext"), context.get("deleteTracesResult"), {"selectedNets": context.get("selectedNets"), "missingRoutes": context.get("missingRoutes")}, context.get("fanoutParams"), arguments):
        if isinstance(item, dict):
            for key in ("orderLines", "nets", "selectedNets", "localRouteNets", "pcbrouterNets", "pcbrouterCsvPath", "localRouteCsvPath"):
                value = item.get(key)
                if value not in (None, "", [], {}):
                    params[key] = value
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
    for item in (context.get("helpPlannerResult"), context.get("rerouteResult"), context.get("fanout_routeResult"), context.get("importLinesResult"), context.get("projectData")):
        if isinstance(item, dict):
            for key in ("routedKicadFilePath", "routedLayoutKicadFilePath", "routedBoardDataFilePath", "boardFilePath", "routedLayoutTxtFilePath", "routingResult", "importLinesFilePath", "absolute_path", "filePath"):
                value = str(item.get(key) or "").strip()
                if value and Path(value).suffix.lower() == ".kicad_pcb":
                    return Path(value)
    return None

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
def _reroute_model_payload(arguments: dict[str, Any], context: dict[str, Any], project_data: str, attempt: int) -> dict[str, Any]:
    delete_result = context.get("deleteTracesResult") if isinstance(context.get("deleteTracesResult"), dict) else {}
    missing_routes = context.get("missingRoutes") or delete_result.get("missing_routes") or []
    return {
        "task": "pcb_local_reroute",
        "attempt": attempt,
        "instruction": "根据 missing routes 补全局部重布；如有上一轮 DRC 反馈，请修正后重新输出。",
        "missingRoutes": missing_routes,
        "selectedNets": context.get("selectedNets") or [],
        "rerouteContext": context.get("rerouteContext") or {},
        "projectDataPreview": project_data[:12000],
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
    }# ====== 功能：返回第一个存在且非空的文件。 ======
def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            return path
    return None


# ====== 功能：生成 router 输出文件摘要。 ======
def _router_report(output_file: Path, completed: dict[str, Any]) -> dict[str, Any]:
    return {"outputFile": str(output_file), "outputBytes": output_file.stat().st_size, "returncode": completed.get("returncode")}


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





















