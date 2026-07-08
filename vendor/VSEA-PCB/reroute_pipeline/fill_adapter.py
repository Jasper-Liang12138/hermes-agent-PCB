from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from .vsea_core.utils import RoutingTask, import_ai_pcb_eval


@dataclass
class FillOutput:
    success: bool
    completed_kicad: str = ""
    completed_kicad_path: str = ""
    semantic_score: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


class FillAdapter:
    def __init__(self, ai_pcb_eval_path: str | Path):
        self.ai_pcb_eval_path = Path(ai_pcb_eval_path).resolve()
        import_ai_pcb_eval(self.ai_pcb_eval_path)
        from eval.pipeline import PCBEvalPipeline
        from eval.types import EvalConfig

        self.pipeline = PCBEvalPipeline(EvalConfig())

    def fill(
        self,
        task: RoutingTask,
        routing_patch: str,
        output_path: str | Path,
    ) -> FillOutput:
        from eval.types import SampleInput

        sample = SampleInput(
            sample_id=task.task_id,
            context_kicad=task.context_kicad,
            label=task.label_code,
            prediction_raw=routing_patch,
            prompt=task.task_prompt,
            meta=task.meta,
        )
        semantic = self.pipeline.semantic_scorer.score(sample)
        if self.pipeline.config.require_kicad_code and not semantic.has_kicad_code:
            return FillOutput(
                success=False,
                semantic_score=semantic.score,
                detail={"semantic_detail": semantic.detail},
                error="prediction does not contain KiCad segment/via code",
            )
        fill_result = self.pipeline.filler.fill(sample, semantic.prediction_code)
        if not fill_result.success:
            return FillOutput(
                success=False,
                semantic_score=semantic.score,
                detail={"fill_detail": fill_result.detail, "semantic_detail": semantic.detail},
                error=fill_result.error_message or str(fill_result.detail),
            )
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fill_result.completed_kicad, encoding="utf-8")
        return FillOutput(
            success=True,
            completed_kicad=fill_result.completed_kicad,
            completed_kicad_path=str(path),
            semantic_score=semantic.score,
            detail={"fill_detail": fill_result.detail, "semantic_detail": semantic.detail},
        )

