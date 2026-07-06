from __future__ import annotations

import unittest

from eval.semantic import KiCadSemanticScorer
from eval.types import EvalConfig, SampleInput


class TestSemanticScoring(unittest.TestCase):
    def test_semantic_score_combines_kicad_and_plain_text(self) -> None:
        scorer = KiCadSemanticScorer(EvalConfig())
        sample = SampleInput(
            sample_id="case-1",
            context_kicad="(kicad_pcb (version 20171130))",
            label=(
                "缺失了 1 条线，下面是补全代码：\n"
                "```kicad\n"
                "(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n"
                "```"
            ),
            prediction_raw=(
                "缺失了 1 条线，现在我来帮你补全：\n"
                "```kicad\n"
                "(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n"
                "```"
            ),
        )

        result = scorer.score(sample)

        self.assertTrue(result.has_kicad_code)
        self.assertGreater(result.score, 0.8)
        self.assertGreater(result.detail["kicad_score"], 0.8)
        self.assertGreater(result.detail["text_score"], 0.3)
        self.assertAlmostEqual(result.detail["semantic_kicad_weight"], 0.85)
        self.assertAlmostEqual(result.detail["semantic_text_weight"], 0.15)

    def test_semantic_score_is_zero_when_kicad_is_required_but_missing(self) -> None:
        scorer = KiCadSemanticScorer(EvalConfig(require_kicad_code=True))
        sample = SampleInput(
            sample_id="case-2",
            context_kicad="(kicad_pcb (version 20171130))",
            label="```kicad\n(segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 1))\n```",
            prediction_raw="我来帮你补全，但这里先不给代码。",
        )

        result = scorer.score(sample)

        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.has_kicad_code)
        self.assertEqual(result.detail["reason"], "missing_kicad_code")


if __name__ == "__main__":
    unittest.main()
