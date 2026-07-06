from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval import evaluate_samples


class TestPipelineIntegration(unittest.TestCase):
    def test_pipeline_runs_end_to_end_with_real_fill_and_real_drc_on_batch2(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        sample_root = project_root / "eval" / "sample_batch"

        incomplete_kicad_list = [
            (sample_root / "incomplete" / "demo-1.kicad_pcb").read_text(encoding="utf-8"),
            (sample_root / "incomplete" / "demo-2.kicad_pcb").read_text(encoding="utf-8"),
        ]
        prediction_raw_list = [
            (sample_root / "prediction" / "demo-1.txt").read_text(encoding="utf-8"),
            (sample_root / "prediction" / "demo-2.txt").read_text(encoding="utf-8"),
        ]
        label_list = [
            (sample_root / "label" / "demo-1.txt").read_text(encoding="utf-8"),
            (sample_root / "label" / "demo-2.txt").read_text(encoding="utf-8"),
        ]

        temp_root = project_root / "eval" / "tests" / "_tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(temp_root)
        try:
            results, summary = evaluate_samples(
                incomplete_kicad_list,
                prediction_raw_list,
                label_list,
                sample_ids=["demo-1", "demo-2"],
            )
        finally:
            tempfile.tempdir = old_tempdir

        self.assertEqual(len(results), 2)
        self.assertEqual(summary["sample_count"], 2)
        print("-------------------------\nresult:\n")
        print(results)
        print("-------------------------\nsummary:\n")
        print(summary)
        for result in results:
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["fill_detail"]["success"])
            self.assertTrue(result["drc_detail"]["success"])
            self.assertIn("segments_count", result["fill_detail"]["detail"])
            self.assertEqual(
                result["drc_detail"]["detail"]["drc_backend"],
                "eval.drc_backend.api.evaluate_drc_score",
            )


if __name__ == "__main__":
    unittest.main()
