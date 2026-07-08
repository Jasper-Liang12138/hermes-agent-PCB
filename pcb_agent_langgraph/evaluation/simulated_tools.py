from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


# ====== 功能：模拟 EDA 前端工具响应，用于 live-eval。 ======
class SimulatedFrontend:
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, sample: dict[str, Any], output_dir: str | Path) -> None:
        self.sample = sample
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[dict[str, Any]] = []

    # ====== 功能：向模拟或真实前端发送工具调用。 ======
    async def send_tool_call(self, session_id: str, call_id: str, arguments: dict[str, Any], timeout: float) -> Any:
        await asyncio.sleep(0)
        tool_name = str(arguments.pop("__tool_name__", ""))
        self.calls.append({"id": call_id, "name": tool_name, "arguments": dict(arguments), "timeout": timeout})
        if tool_name == "getProjectData":
            return self._project_data(session_id)
        if tool_name == "deleteTracesForRerouting":
            return self._delete_traces(session_id)
        if tool_name == "importLines":
            return self._import_lines(arguments)
        return {"status": "failed", "reason": f"simulated frontend does not implement {tool_name}"}

    # ====== 功能：生成模拟前端项目数据。 ======
    def _project_data(self, session_id: str) -> dict[str, Any]:
        board_data = str(self.sample.get("board_data") or "COMPONENT U1 BGA pins=256 NET DDR_DQ0 DDR_DQ1 DDR_CLK")
        board_path = self.output_dir / f"{session_id}_board.txt"
        board_path.write_text(board_data, encoding="utf-8")
        return {
            "status": "ok",
            "source": "simulated_getProjectData",
            "boardData": board_data,
            "relative_path": board_path.name,
            "absolute_path": str(board_path),
            "components": [{"refdes": "U1", "package": "BGA", "pinCount": 256}],
        }

    # ====== 功能：生成模拟拆线结果。 ======
    def _delete_traces(self, session_id: str) -> dict[str, Any]:
        board_path = self.output_dir / f"{session_id}_reroute_input.kicad_pcb"
        board_path.write_text(
            """
            (kicad_pcb
              (version 20240108)
              (layers (0 "F.Cu" signal))
              (net 1 DDR_DQ0)
              (net 2 DDR_DQ1)
              (segment (start 1 1) (end 2 1) (width 0.1524) (layer F.Cu) (net 1))
            )
            """,
            encoding="utf-8",
        )
        return {
            "status": "ok",
            "source": "simulated_deleteTracesForRerouting",
            "deletedTraceCount": int(self.sample.get("deleted_trace_count", 4)),
            "projectData": {"absolute_path": str(board_path), "boardData": board_path.read_text(encoding="utf-8")},
            "selectedNets": self.sample.get("selected_nets") or ["DDR_DQ0", "DDR_DQ1"],
            "missing_routes": [{"net_name": net} for net in (self.sample.get("selected_nets") or ["DDR_DQ0", "DDR_DQ1"])],
        }

    # ====== 功能：生成模拟导入布线结果。 ======
    def _import_lines(self, arguments: dict[str, Any]) -> dict[str, Any]:
        file_path = str(arguments.get("filePath") or "")
        return {
            "status": "ok",
            "source": "simulated_importLines",
            "importLinesFilePath": file_path,
            "importedLineCount": int(self.sample.get("imported_line_count", 12)),
        }


# ====== 功能：模拟外部 PCB 工具响应，用于离线评测。 ======
class SimulatedExternalTool:
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, name: str, output_dir: str | Path, sample: dict[str, Any] | None = None) -> None:
        self.name = name
        self.output_dir = Path(output_dir)
        self.sample = sample or {}
        self.call_count = 0
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
        await asyncio.sleep(0)
        self.call_count += 1
        session_id = str(context.get("session_id") or "sample")
        if self.name == "layer_assign":
            return {"status": "ok", "layers": [3, 4, 5, 6], "strategy": "simulated_layer_assignment", "fanoutParams": dict(arguments)}
        if self.name == "escape_order":
            return {"status": "ok", "order": ["DDR_DQ0", "DDR_DQ1", "DDR_CLK"], "strategy": "simulated_escape_order", "fanoutParams": dict(arguments)}
        if self.name == "fanout_route":
            path = self.output_dir / f"{session_id}_fanout_lines.out"
            path.write_text("LINE DDR_DQ0 0 0 10 10\nLINE DDR_DQ1 0 1 10 11\n", encoding="utf-8")
            return {"status": "ok", "routingResult": str(path), "importLinesFilePath": str(path), "successRate": 1.0, "fanoutParams": dict(arguments)}
        if self.name == "prepare_reroute_inputs":
            board_path = self.output_dir / f"{session_id}_reroute_input.kicad_pcb"
            board_text = board_path.read_text(encoding="utf-8") if board_path.exists() else "(kicad_pcb (version 20240108) (net 1 DDR_DQ0))\n"
            if not board_path.exists():
                board_path.write_text(board_text, encoding="utf-8")
            csv_path = self.output_dir / f"{session_id}_local_route_input.csv"
            nets = self.sample.get("selected_nets") or ["DDR_DQ0", "DDR_DQ1"]
            csv_path.write_text("net\n" + "\n".join(str(net) for net in nets) + "\n", encoding="utf-8")
            return {
                "status": "ok",
                "tool": self.name,
                "sourceLayoutPath": str(board_path),
                "kicadBoardPath": str(board_path),
                "kicadBoardText": board_text,
                "localRouteCsvPath": str(csv_path),
                "missingRoutes": [{"net_name": net} for net in nets],
                "selectedNets": list(nets),
                "csvMode": "net_only",
            }
        if self.name == "reroute_loop":
            if self.sample.get("simulate_reroute_loop_failure"):
                return {"status": "failed", "tool": self.name, "reason": "simulated VSEA failure", "source": "vsea_reroute_pipeline"}
            path = self.output_dir / f"{session_id}_vsea_lines.out"
            path.write_text("TOP!LINE!0!DDR_DQ0!2!1!3!1!0.1524\nTOP!LINE!0!DDR_DQ1!2!2!3!2!0.1524\n", encoding="utf-8")
            routed = self.output_dir / f"{session_id}_vsea_completed.kicad_pcb"
            routed.write_text("(kicad_pcb (version 20240108) (net 1 DDR_DQ0))\n", encoding="utf-8")
            return {
                "status": "ok",
                "tool": self.name,
                "source": "vsea_reroute_pipeline",
                "modelOutputText": "(segment (start 2 1) (end 3 1) (layer F.Cu) (net 1))",
                "routedKicadFilePath": str(routed),
                "importLinesFilePath": str(path),
                "drcResult": {"status": "ok", "passed": True, "source": "vsea_reroute_pipeline"},
                "workDir": str(self.output_dir),
            }
        if self.name == "reroute":
            path = self.output_dir / f"{session_id}_reroute_lines.out"
            path.write_text("LINE DDR_DQ0 1 1 11 11\nLINE DDR_DQ1 1 2 11 12\n", encoding="utf-8")
            return {"status": "ok", "rerouteResult": {"status": "drc_passed_import_pending"}, "importLinesFilePath": str(path), "routedLayoutTxtFilePath": str(path)}
        if self.name == "help_planner":
            path = self.output_dir / f"{session_id}_help_planner.kicad_pcb"
            path.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
            return {"status": "ok", "routingResult": str(path), "importLinesFilePath": str(path), "routedLayoutTxtFilePath": str(path), "report": "Simulated help planner completed."}
        if self.name == "drc_check":
            fail_count = int(self.sample.get("simulate_drc_failures", 0) or 0)
            if self.call_count <= fail_count:
                return {"status": "failed", "passed": False, "errors": [f"simulated_drc_failure_{self.call_count}"], "score": 0.0, "source": "simulated_drc"}
            return {"status": "ok", "passed": True, "errors": [], "score": 1.0, "source": "simulated_drc"}
        if self.name == "explainability_report":
            return {"status": "ok", "report": "Simulated DRC report: passed."}
        return {"status": "failed", "reason": f"simulated external tool does not implement {self.name}"}
