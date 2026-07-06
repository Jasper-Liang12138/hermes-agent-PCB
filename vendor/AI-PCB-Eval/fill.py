from __future__ import annotations

from .patch_kicad_from_raw_standalone import fill_incomplete_board_from_raw_text
from .types import EvalConfig, FillResult, SampleInput


class KicadCodeFiller:
    """Fill incomplete board text using the standalone patch backend."""

    def __init__(self, config: EvalConfig):
        self.config = config

    def fill(self, sample: SampleInput, prediction_code: str) -> FillResult:
        raw_text = prediction_code if prediction_code.strip() else sample.prediction_raw

        try:
            result = fill_incomplete_board_from_raw_text(
                raw_text,
                sample.context_kicad,
                ensure_unique=True,
            )
        except Exception as exc:
            return FillResult(
                success=False,
                detail={"reason": "patch_backend_failed", "fill_backend": "patch_kicad_from_raw_standalone"},
                error_message=str(exc),
            )

        return FillResult(
            success=True,
            completed_kicad=result.filled_pcb_text,
            detail={
                "fill_backend": "patch_kicad_from_raw_standalone",
                "segments_count": result.segments_count,
                "vias_count": result.vias_count,
                "other_lines_count": result.other_lines_count,
            },
        )
