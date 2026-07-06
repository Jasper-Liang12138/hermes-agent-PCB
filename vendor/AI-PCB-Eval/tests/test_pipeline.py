from __future__ import annotations

import unittest
from unittest.mock import patch

from eval.pipeline import PCBEvalPipeline
from eval.types import DRCResult, EvalConfig, SampleInput


class TestPipeline(unittest.TestCase):
    def test_pipeline_uses_real_fill_and_aggregates_scores(self) -> None:
        pipeline = PCBEvalPipeline(EvalConfig(alpha=0.5))

        def fake_drc_score(completed_kicad: str) -> DRCResult:
            self.assertIn("(segment", completed_kicad)
            return DRCResult(
                score=0.8,
                success=True,
                violations=0,
                detail={"drc_backend": "fake"},
            )

        with patch.object(pipeline.drc_scorer, "score", side_effect=fake_drc_score):
            sample = SampleInput(
                sample_id="pipe-1",
                context_kicad="(kicad_pcb\n  (version 20171130)\n)",
                label="```kicad\n(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n```",
                prediction_raw="```kicad\n(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n```",
            )

            result = pipeline.evaluate(sample)

        self.assertEqual(result.status, "ok")
        self.assertGreater(result.s1, 0.9)
        self.assertAlmostEqual(result.s2, 0.8)
        self.assertAlmostEqual(result.final_score, 0.5 * result.s1 + 0.5 * result.s2)
        self.assertTrue(result.fill_detail["success"])
        self.assertEqual(result.fill_detail["detail"]["segments_count"], 1)

    def test_pipeline_sets_s2_zero_when_fill_fails(self) -> None:
        pipeline = PCBEvalPipeline(EvalConfig(alpha=0.5))
        sample = SampleInput(
            sample_id="pipe-2",
            context_kicad="(kicad_pcb\n  (version 20171130)\n)",
            label="```kicad\n(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n```",
            prediction_raw="这里没有任何 segment 或 via",
        )

        result = pipeline.evaluate(sample)

        self.assertEqual(result.s2, 0.0)
        self.assertIn(result.status, {"no_kicad_code", "patch_backend_failed"})
        self.assertTrue((not result.fill_detail["success"]) or (not result.has_kicad_code))


if __name__ == "__main__":
    unittest.main()
