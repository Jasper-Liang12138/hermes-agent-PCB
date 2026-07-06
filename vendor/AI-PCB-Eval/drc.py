from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .types import DRCResult, EvalConfig


class DRCScorer:
    """DRC scorer backed by eval/drc_backend/api.py."""

    def __init__(self, config: EvalConfig):
        self.config = config

    def score(self, completed_kicad: str) -> DRCResult:
        if not completed_kicad.strip():
            return DRCResult(
                score=0.0,
                success=False,
                detail={"reason": "empty_completed_kicad"},
                error_message="Completed KiCad content is empty.",
            )

        backend_root = Path(__file__).resolve().parent / "drc_backend"
        if str(backend_root) not in sys.path:
            sys.path.insert(0, str(backend_root))

        try:
            from api import evaluate_drc_score
        except Exception as exc:
            return DRCResult(
                score=0.0,
                success=False,
                detail={"reason": "drc_backend_import_failed", "backend_root": str(backend_root)},
                error_message=str(exc),
            )

        with tempfile.TemporaryDirectory(prefix="pcb_eval_") as temp_dir:
            board_path = Path(temp_dir) / "board.kicad_pcb"
            board_path.write_text(completed_kicad, encoding="utf-8")

            try:
                result = evaluate_drc_score(str(board_path), check_mode="hard")
            except Exception as exc:
                return DRCResult(
                    score=0.0,
                    success=False,
                    detail={"reason": "drc_backend_failed"},
                    error_message=str(exc),
                )

        ok = bool(result.get("ok"))
        score_100 = float(result.get("score", 0.0) or 0.0)
        details = result.get("details", {}) or {}
        artifacts = result.get("artifacts", {}) or {}
        error = result.get("error")

        return DRCResult(
            score=max(0.0, min(1.0, score_100 / 100.0)),
            success=ok,
            violations=int(details.get("hard_issue_count", 0) or 0),
            warnings=0,
            raw_output="",
            detail={
                "drc_backend": "eval.drc_backend.api.evaluate_drc_score",
                "score_name": result.get("score_name"),
                "pass": result.get("pass"),
                "hard_penalty": details.get("hard_penalty", 0.0),
                "hard_issue_count": details.get("hard_issue_count", 0),
                "hard_rule_counts": details.get("hard_rule_counts", {}),
                "timing": artifacts.get("timing", {}),
            },
            error_message="" if ok else str(error or "DRC backend returned failure."),
        )
