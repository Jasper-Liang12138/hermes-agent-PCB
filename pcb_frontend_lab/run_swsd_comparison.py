"""Run a baseline-vs-SWSD PCB frontend lab experiment.

The lab runner stays black-box: it only talks to a WebSocket URL. This harness
starts a real WebSocketAdapter with deterministic PCB behavior so the experiment
does not depend on a configured LLM, PCB Builder, or router binary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import sqlite3
import tempfile
import time
import types
from pathlib import Path
from typing import Any

from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter
from hermes_state import SessionDB
from agent.swsd.state_manager import WorkflowStateManager
from pcb_frontend_lab.runner import LabConfig, run_all


BOARD_FIXTURE = """(pcb_data
  (component (name U27) (package BGA256) (pins A1 A2 B1 B2))
  (component (name U23) (package BGA144) (pins A1 A2))
  (component (name U8) (package QFP64))
)"""


FANOUT_PARAMS = {
    "selectedBGA": "U27",
    "routerType": "135",
    "layerAssignments": {"A1": "L2", "A2": "L2", "B1": "L3"},
    "orderLines": [{"pin": "A1", "order": 1}, {"pin": "A2", "order": 2}],
    "constraints": {"LineWidth": 4, "LineSpacing": 3},
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_cases(path: Path) -> None:
    cases = [
        {
            "id": "concept_chat",
            "turns": ["BGA 和 QFP 有什么区别？"],
            "expect": {"no_tool_calls": True, "no_error": True, "absent_body_fields": ["selection", "fanoutParams"]},
        },
        {
            "id": "fanout_selection",
            "turns": ["请对 U27 做 BGA 逃逸布线"],
            "tool_results": {"getProjectData": BOARD_FIXTURE},
            "expect": {"tool_calls": ["getProjectData"], "body_fields": ["selection"], "no_error": True},
        },
        {
            "id": "fanout_route_from_frontend_params",
            "turns": [
                "请对 U27 做 BGA 逃逸布线",
                "已完成逃逸参数配置，确认布线参数\n" + json.dumps({"fanoutParams": FANOUT_PARAMS}, ensure_ascii=False),
            ],
            "tool_results": {
                "getProjectData": BOARD_FIXTURE,
                "importLines": {"success": True, "message": "imported"},
            },
            "expect": {
                "tool_calls": ["getProjectData", "importLines"],
                "body_fields": ["selection", "routingResult"],
                "no_error": True,
            },
        },
        {
            "id": "reroute_frontend_error",
            "turns": ["进入拆线重布，重新绕开框选区域"],
            "expect": {"no_importLines": True, "body_fields": ["rerouteResult", "checkReport", "explanation"], "no_error": True},
        },
        {
            "id": "fanout_then_reroute_switch",
            "turns": ["请对 U27 做 BGA 逃逸布线", "改为拆线重布，不要继续 fanout"],
            "tool_results": {"getProjectData": BOARD_FIXTURE},
            "expect": {
                "tool_calls": ["getProjectData"],
                "body_fields": ["selection", "rerouteResult"],
                "absent_body_fields": ["fanoutParams"],
                "no_error": True,
            },
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")


def _install_deterministic_pcb_behavior(adapter: WebSocketAdapter) -> None:
    async def fake_bga_analysis(self: WebSocketAdapter, session_id: str, bootstrap_context: dict[str, Any]) -> bool:
        fields = {
            "selection": [
                {"label": "U27", "refdes": "U27", "package": "BGA256", "pinCount": 256},
                {"label": "U23", "refdes": "U23", "package": "BGA144", "pinCount": 144},
            ],
            "boardSummary": {"componentCount": 3, "bgaCount": 2},
            "fanoutContext": {"recommendedLineWidth": 4, "recommendedLineSpacing": 3},
        }
        self._remember_board_analysis(session_id, fields)
        self._set_session_mode(session_id, "pcb")
        self._set_flow_state(session_id, "wait_selection")
        await self.send(
            chat_id=session_id,
            content="已识别到 BGA 候选，请选择目标器件。\n\n##PCB_FIELDS##\n"
            + json.dumps(fields, ensure_ascii=False)
            + "\n##PCB_FIELDS_END##",
        )
        return True

    async def fake_fanout_param_step(self: WebSocketAdapter, session_id: str, user_text: str) -> bool:
        params = dict(FANOUT_PARAMS)
        self._remember_fanout_params_from_frontend(session_id, params)
        await self.send(
            chat_id=session_id,
            content="已生成逃逸参数，请确认。\n\n##PCB_FIELDS##\n"
            + json.dumps({"fanoutParams": params}, ensure_ascii=False)
            + "\n##PCB_FIELDS_END##",
        )
        return True

    async def fake_route(self: WebSocketAdapter, session_id: str) -> bool:
        params = dict(self._session_fanout_params.get(session_id) or FANOUT_PARAMS)
        self._set_session_mode(session_id, "pcb")
        self._set_flow_state(session_id, "routing")
        fields = {
            "routingResult": str(Path(tempfile.gettempdir()) / f"pcb_lab_{session_id}_line.out"),
            "importLinesFilePath": str(Path(tempfile.gettempdir()) / f"pcb_lab_{session_id}_line.out"),
            "report": "虚拟布线完成，成功 2 pin，失败 0 pin。",
            "successPins": ["A1", "A2"],
            "failedPins": [],
        }
        import_status = await self._import_fanout_result(session_id, params, fields)
        self._swsd_update(
            session_id,
            "pcb_escape_flow",
            "review",
            self._swsd_escape_payload(session_id, {"routeParams": params, "routeFields": fields, "importStatus": import_status}),
            event_type="checkpoint",
            intent="route_complete",
            checkpoint_label="routing result",
        )
        await self.send(
            chat_id=session_id,
            content="虚拟布线完成。\n\n##PCB_FIELDS##\n"
            + json.dumps(fields, ensure_ascii=False)
            + "\n##PCB_FIELDS_END##",
            metadata={"stream_is_final": True},
        )
        return True

    async def fake_handler(event: Any) -> str:
        text = str(getattr(event, "text", "") or "")
        session_id = getattr(getattr(event, "source", None), "chat_id", "")
        if "拆线" in text or "reroute" in text.lower():
            adapter._swsd_update(
                session_id,
                "pcb_reroute_flow",
                "report",
                {
                    "legacyFlowState": "reroute",
                    "rerouteResult": {"success": False, "reason": "frontend selection required"},
                },
                event_type="checkpoint",
                intent="reroute_result",
                checkpoint_label="reroute frontend error",
            )
            return (
                "前端未提供框选走线，已停止导入。\n\n##PCB_FIELDS##\n"
                + json.dumps(
                    {
                        "rerouteResult": {"success": False, "error": "请先在前端框选需要拆线的走线"},
                        "checkReport": "未执行 DRC：缺少框选输入。",
                        "explanation": "reroute 请求没有继续 fanout，也没有调用 importLines。",
                    },
                    ensure_ascii=False,
                )
                + "\n##PCB_FIELDS_END##"
            )
        return "这是 PCB 概念咨询：BGA 是球栅阵列封装，QFP 是四边引脚扁平封装。"

    adapter._run_direct_bga_analysis = types.MethodType(fake_bga_analysis, adapter)
    adapter._run_direct_fanout_param_step = types.MethodType(fake_fanout_param_step, adapter)
    adapter._run_cached_fanout_route = types.MethodType(fake_route, adapter)
    adapter.set_message_handler(fake_handler)


async def _run_group(name: str, *, swsd_enabled: bool, base_dir: Path, cases_path: Path, timeout: float) -> dict[str, Any]:
    group_dir = base_dir / name
    home_dir = group_dir / "hermes_home"
    home_dir.mkdir(parents=True, exist_ok=True)
    db_path = home_dir / "state.db"
    port = _free_port()
    adapter = WebSocketAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": port,
                "dedicated_ws_thread": False,
                "bootstrap_get_project": True,
                "route_intent_llm_enabled": False,
                "fanout_param_llm_enabled": False,
                "trace_pcb_messages": False,
                "swsd_enabled": swsd_enabled,
                "swsd_persist_checkpoints": swsd_enabled,
            },
        )
    )
    if swsd_enabled:
        db = SessionDB(db_path)
        adapter._swsd_db = db
        adapter._swsd_state = WorkflowStateManager(db, persist=True)
    _install_deterministic_pcb_behavior(adapter)

    await adapter.connect()
    try:
        out_path = group_dir / f"{name}.jsonl"
        results = await run_all(
            LabConfig(
                ws_url=f"ws://127.0.0.1:{port}",
                cases_path=cases_path,
                out_path=out_path,
                timeout=timeout,
                session_prefix=f"lab-{name}",
            )
        )
    finally:
        await adapter.disconnect()

    return {
        "name": name,
        "swsd_enabled": swsd_enabled,
        "report": str(out_path),
        "transcript_dir": str(out_path.parent / f"{out_path.stem}_transcripts"),
        "home": str(home_dir),
        "db_path": str(db_path),
        "results": results,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_lab_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(results),
        "passed": sum(1 for item in results if item.get("passed")),
        "failed": sum(1 for item in results if not item.get("passed")),
        "cases": {
            str(item.get("id")): {
                "passed": bool(item.get("passed")),
                "tool_calls": item.get("actual", {}).get("tool_calls", []),
                "body_fields": item.get("actual", {}).get("body_fields", []),
                "error_count": item.get("actual", {}).get("error_count", 0),
                "failures": item.get("failures", []),
            }
            for item in results
        },
    }


def inspect_swsd_db(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_exists": False, "sessions": 0, "events": 0, "checkpoints": 0, "states_by_workflow": {}}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"workflow_sessions", "workflow_events", "workflow_checkpoints"}.issubset(tables):
            return {"db_exists": True, "sessions": 0, "events": 0, "checkpoints": 0, "states_by_workflow": {}}
        sessions = conn.execute("SELECT COUNT(*) FROM workflow_sessions").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]
        checkpoints = conn.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0]
        state_rows = conn.execute(
            """
            SELECT workflow_id, to_state AS state, COUNT(*) AS count
            FROM workflow_events
            GROUP BY workflow_id, to_state
            ORDER BY workflow_id, to_state
            """
        ).fetchall()
        final_rows = conn.execute(
            "SELECT session_id, workflow_id, current_state FROM workflow_sessions ORDER BY session_id, workflow_id"
        ).fetchall()
    states_by_workflow: dict[str, dict[str, int]] = {}
    for row in state_rows:
        states_by_workflow.setdefault(row["workflow_id"], {})[row["state"]] = int(row["count"])
    return {
        "db_exists": True,
        "sessions": int(sessions),
        "events": int(events),
        "checkpoints": int(checkpoints),
        "states_by_workflow": states_by_workflow,
        "final_states": [dict(row) for row in final_rows],
    }


def compare_protocol(baseline: dict[str, Any], swsd: dict[str, Any]) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    for case_id, base_case in baseline["cases"].items():
        swsd_case = swsd["cases"].get(case_id)
        if not swsd_case:
            diffs.append({"case": case_id, "type": "missing_swsd_case"})
            continue
        for key in ("tool_calls", "body_fields", "error_count"):
            if base_case.get(key) != swsd_case.get(key):
                diffs.append({"case": case_id, "type": f"{key}_diff", "baseline": base_case.get(key), "swsd": swsd_case.get(key)})
    return diffs


def _agent_transcripts_leak_board_text(group: dict[str, Any]) -> bool:
    transcript_dir = Path(group["transcript_dir"])
    if not transcript_dir.exists():
        return False
    needle = "(component (name U27) (package BGA256)"
    for path in transcript_dir.glob("*.json"):
        try:
            frames = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError:
            continue
        if not isinstance(frames, list):
            continue
        for item in frames:
            if not isinstance(item, dict) or item.get("direction") != "agent_to_frontend":
                continue
            if needle in json.dumps(item.get("frame", {}), ensure_ascii=False):
                return True
    return False


def _swsd_db_leaks_board_text(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    needle = "(component (name U27) (package BGA256)"
    with sqlite3.connect(db_path) as conn:
        for table, column in (
            ("workflow_sessions", "state_payload"),
            ("workflow_events", "payload"),
            ("workflow_checkpoints", "payload"),
        ):
            try:
                rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
            except sqlite3.Error:
                continue
            if any(needle in str(row[0] or "") for row in rows):
                return True
    return False


def write_human_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# SWSD PCB Frontend Lab Comparison",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Pass/Fail",
        "",
        "| Case | Baseline | SWSD | Baseline tools | SWSD tools |",
        "| --- | --- | --- | --- | --- |",
    ]
    baseline_cases = summary["baseline"]["cases"]
    swsd_cases = summary["swsd"]["cases"]
    for case_id in baseline_cases:
        b = baseline_cases[case_id]
        s = swsd_cases.get(case_id, {})
        lines.append(
            f"| {case_id} | {'PASS' if b.get('passed') else 'FAIL'} | "
            f"{'PASS' if s.get('passed') else 'FAIL'} | "
            f"{', '.join(b.get('tool_calls') or []) or '-'} | "
            f"{', '.join(s.get('tool_calls') or []) or '-'} |"
        )
    swsd_db = summary["swsd_db"]
    lines.extend(
        [
            "",
            "## SWSD Persistence",
            "",
            f"- workflow_sessions: {swsd_db.get('sessions', 0)}",
            f"- workflow_events: {swsd_db.get('events', 0)}",
            f"- workflow_checkpoints: {swsd_db.get('checkpoints', 0)}",
            f"- observed states: `{json.dumps(swsd_db.get('states_by_workflow', {}), ensure_ascii=False)}`",
            "",
            "## Protocol Diffs",
            "",
        ]
    )
    if summary["protocol_diffs"]:
        for diff in summary["protocol_diffs"]:
            lines.append(f"- {json.dumps(diff, ensure_ascii=False)}")
    else:
        lines.append("- No tool/body/error diffs between baseline and SWSD.")
    lines.extend(
        [
            "",
            "## Board Text Leak Check",
            "",
            f"- baseline agent outbound transcript leak: {summary['leak_check']['baseline_agent_outbound']}",
            f"- SWSD agent outbound transcript leak: {summary['leak_check']['swsd_agent_outbound']}",
            f"- SWSD DB payload leak: {summary['leak_check']['swsd_db']}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_comparison(out_dir: Path, timeout: float = 30.0) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for child_name in ("baseline", "swsd"):
        child = out_dir / child_name
        if child.exists():
            shutil.rmtree(child)
    for file_name in ("cases.jsonl", "summary.json", "summary.md"):
        target = out_dir / file_name
        if target.exists():
            target.unlink()
    cases_path = out_dir / "cases.jsonl"
    _write_cases(cases_path)

    baseline_group = await _run_group("baseline", swsd_enabled=False, base_dir=out_dir, cases_path=cases_path, timeout=timeout)
    swsd_group = await _run_group("swsd", swsd_enabled=True, base_dir=out_dir, cases_path=cases_path, timeout=timeout)

    baseline = summarize_lab_results(_read_jsonl(Path(baseline_group["report"])))
    swsd = summarize_lab_results(_read_jsonl(Path(swsd_group["report"])))
    summary = {
        "cases_path": str(cases_path),
        "baseline": baseline,
        "swsd": swsd,
        "baseline_group": {key: value for key, value in baseline_group.items() if key != "results"},
        "swsd_group": {key: value for key, value in swsd_group.items() if key != "results"},
        "swsd_db": inspect_swsd_db(Path(swsd_group["db_path"])),
        "protocol_diffs": compare_protocol(baseline, swsd),
        "leak_check": {
            "baseline_agent_outbound": _agent_transcripts_leak_board_text(baseline_group),
            "swsd_agent_outbound": _agent_transcripts_leak_board_text(swsd_group),
            "swsd_db": _swsd_db_leaks_board_text(Path(swsd_group["db_path"])),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_human_report(out_dir / "summary.md", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run baseline vs SWSD pcb_frontend_lab comparison.")
    parser.add_argument("--out-dir", type=Path, default=Path("pcb_frontend_lab/reports/swsd_comparison"), help="Experiment output directory")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-turn lab timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = asyncio.run(run_comparison(args.out_dir, timeout=args.timeout))
    print(f"Summary: {args.out_dir / 'summary.md'}")
    print(f"Baseline: {summary['baseline']['passed']}/{summary['baseline']['total']} passed")
    print(f"SWSD: {summary['swsd']['passed']}/{summary['swsd']['total']} passed")
    print(
        "SWSD DB: "
        f"{summary['swsd_db'].get('events', 0)} events, "
        f"{summary['swsd_db'].get('checkpoints', 0)} checkpoints"
    )
    return 1 if summary["baseline"]["failed"] or summary["swsd"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
