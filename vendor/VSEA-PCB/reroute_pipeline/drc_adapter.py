from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


@dataclass
class DRCOutput:
    success: bool
    hard_issue_count: int
    report: Dict[str, Any] = field(default_factory=dict)
    report_path: str = ""
    error: str = ""


class AgentHardDRCAdapter:
    def __init__(
        self,
        drc_agent_package: str | Path,
        python_executable: str = "",
        timeout_seconds: float = 180.0,
    ):
        self.drc_agent_package = Path(drc_agent_package).resolve()
        self.python_executable = python_executable or os.getenv("PYTHON", "python")
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        completed_kicad_path: str | Path,
        report_path: str | Path,
        target_bga: str = "",
    ) -> DRCOutput:
        report = Path(report_path)
        report.parent.mkdir(parents=True, exist_ok=True)
        if not self.drc_agent_package.exists():
            return DRCOutput(
                success=False,
                hard_issue_count=1,
                report_path=str(report),
                error=f"DRC agent package not found: {self.drc_agent_package}",
            )
        cmd = [
            self.python_executable,
            "prod_main.py",
            str(completed_kicad_path),
            "--check-mode",
            "hard",
            "--agent-zh-json-out",
            str(report),
        ]
        if target_bga:
            cmd.extend(["--target-bga", target_bga])
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self.drc_agent_package),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            return DRCOutput(
                success=False,
                hard_issue_count=1,
                report_path=str(report),
                error=f"{exc.__class__.__name__}: {exc}",
            )
        if completed.returncode != 0:
            return DRCOutput(
                success=False,
                hard_issue_count=1,
                report_path=str(report),
                report={
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                    "returncode": completed.returncode,
                },
                error=completed.stderr[-1000:] or completed.stdout[-1000:],
            )
        payload = json.loads(report.read_text(encoding="utf-8"))
        result = payload.get("result", {}) or {}
        hard_issue_count = int(result.get("hard_issue_count", 0) or 0)
        return DRCOutput(
            success=hard_issue_count == 0,
            hard_issue_count=hard_issue_count,
            report=payload,
            report_path=str(report),
        )

