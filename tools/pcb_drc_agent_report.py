"""Best-effort wrapper for the optional vendored PCB DRC agent report tool."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 300.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "session").strip("_") or "session"


def _compact_text(value: str, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def resolve_drc_agent_tool_dir() -> Path:
    configured = os.getenv("PCB_DRC_AGENT_TOOL_DIR", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    return (_repo_root() / "vendor" / "pcb_drc_agent_tool").resolve()


def generate_drc_agent_report(
    *,
    board_file_path: str,
    output_dir: str,
    session_id: str,
    target_bga: str = "",
    check_mode: str = "hard",
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run prod_main.py and return its agent-ready JSON payload.

    The wrapper is intentionally best-effort: missing optional vendor files or
    subprocess failures are returned as structured data instead of raising.
    """

    board_path = Path(os.path.expandvars(os.path.expanduser(str(board_file_path or "")))).resolve()
    report_dir = Path(output_dir or ".").resolve() / "drc_agent_report"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{_safe_name(session_id)}_drc_agent.json"
    log_path = report_dir / f"{_safe_name(session_id)}_drc_agent.log"

    if not board_path.is_file():
        return {"ok": False, "json_path": str(json_path), "error": f"DRC agent input board file is unavailable: {board_file_path}"}

    tool_dir = resolve_drc_agent_tool_dir()
    prod_main = tool_dir / "prod_main.py"
    if not prod_main.is_file():
        return {"ok": False, "json_path": str(json_path), "error": f"DRC agent prod_main.py is missing: {prod_main}"}

    cmd = [
        sys.executable,
        str(prod_main),
        str(board_path),
        "--check-mode",
        str(check_mode or "hard"),
        "--agent-zh-json-out",
        str(json_path),
        "--log-file",
        str(log_path),
    ]
    if str(target_bga or "").strip():
        cmd.extend(["--target-bga", str(target_bga).strip()])

    env = os.environ.copy()
    pythonpath = [str(tool_dir)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(tool_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds if timeout_seconds is not None else float(os.getenv("PCB_DRC_AGENT_REPORT_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))),
        )
    except Exception as exc:
        return {"ok": False, "json_path": str(json_path), "error": f"DRC agent report generation failed: {type(exc).__name__}: {exc}"}

    payload: dict[str, Any] = {}
    if json_path.is_file():
        try:
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {"value": loaded}
        except Exception as exc:
            payload = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}

    if completed.returncode != 0:
        detail = _compact_text(completed.stderr or completed.stdout)
        return {
            "ok": False,
            "json_path": str(json_path),
            "returncode": completed.returncode,
            "payload": payload,
            "error": detail or f"DRC agent exited with code {completed.returncode}",
        }
    if not payload:
        return {"ok": False, "json_path": str(json_path), "returncode": completed.returncode, "error": "DRC agent completed but did not write JSON output."}
    return {"ok": True, "json_path": str(json_path), "returncode": completed.returncode, "payload": payload}
