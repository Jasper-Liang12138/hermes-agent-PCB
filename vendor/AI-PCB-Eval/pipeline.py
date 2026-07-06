from __future__ import annotations

from .drc import DRCScorer
from .fill import KicadCodeFiller
from .semantic import KiCadSemanticScorer
from .types import DRCResult, EvalConfig, PipelineResult, SampleInput


class PCBEvalPipeline:
    """Three-stage PCB eval pipeline: semantic, fill, DRC, then aggregate."""

    def __init__(self, config: EvalConfig | None = None):
        self.config = config or EvalConfig()
        self.semantic_scorer = KiCadSemanticScorer(self.config)
        self.filler = KicadCodeFiller(self.config)
        self.drc_scorer = DRCScorer(self.config)

    def evaluate(self, sample: SampleInput) -> PipelineResult:
        semantic = self.semantic_scorer.score(sample)
        if self.config.require_kicad_code and not semantic.has_kicad_code:
            fill_result = self.filler.fill(sample, semantic.prediction_code)
            drc_result = DRCResult(
                score=0.0,
                success=False,
                detail={"reason": "skipped_due_to_missing_kicad_code"},
                error_message="Skipped DRC because prediction does not contain KiCad code.",
            )
            s2 = 0.0
            status = "no_kicad_code"
            error_message = "Prediction does not contain KiCad code."
        else:
            fill_result = self.filler.fill(sample, semantic.prediction_code)

            status = "ok"
            error_message = ""
            if not fill_result.success:
                drc_result = DRCResult(
                    score=0.0,
                    success=False,
                    detail={"reason": "skipped_due_to_fill_failure"},
                    error_message="Skipped DRC because fill stage failed.",
                )
                s2 = 0.0
                status = fill_result.detail.get("reason", "fill_failed")
                error_message = fill_result.error_message
            else:
                drc_result = self.drc_scorer.score(fill_result.completed_kicad)
                s2 = drc_result.score if drc_result.success else 0.0
                if not drc_result.success:
                    status = drc_result.detail.get("reason", "drc_failed")
                    error_message = drc_result.error_message

        final_score = (self.config.alpha * semantic.score) + ((1.0 - self.config.alpha) * s2)
        return PipelineResult(
            sample_id=sample.sample_id,
            s1=semantic.score,
            s2=s2,
            final_score=final_score,
            status=status,
            prediction_code=semantic.prediction_code,
            has_kicad_code=semantic.has_kicad_code,
            semantic_detail=semantic.detail,
            fill_detail={
                "success": fill_result.success,
                "detail": fill_result.detail,
                "error_message": fill_result.error_message,
            },
            drc_detail=drc_result.to_dict(),
            error_message=error_message,
        )
