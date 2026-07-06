from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .batch_loader import build_samples_from_lists, load_samples_from_dirs
from .pipeline import PCBEvalPipeline
from .types import EvalConfig


def summarize_results(results: List[dict]) -> dict:
    count = len(results)
    if count == 0:
        return {
            "sample_count": 0,
            "mean_s1": 0.0,
            "mean_s2": 0.0,
            "mean_final_score": 0.0,
            "has_kicad_code_rate": 0.0,
            "fill_success_rate": 0.0,
            "drc_success_rate": 0.0,
        }
    return {
        "sample_count": count,
        "mean_s1": sum(item["s1"] for item in results) / count,
        "mean_s2": sum(item["s2"] for item in results) / count,
        "mean_final_score": sum(item["final_score"] for item in results) / count,
        "has_kicad_code_rate": sum(1 for item in results if item["has_kicad_code"]) / count,
        "fill_success_rate": sum(1 for item in results if item["fill_detail"]["success"]) / count,
        "drc_success_rate": sum(1 for item in results if item["drc_detail"]["success"]) / count,
    }


def evaluate_samples(
    incomplete_kicad_list: Sequence[str],
    prediction_raw_list: Sequence[str],
    label_list: Sequence[str],
    *,
    sample_ids: Sequence[str] | None = None,
    config: EvalConfig | None = None,
) -> Tuple[List[dict], dict]:
    samples = build_samples_from_lists(
        incomplete_kicad_list=incomplete_kicad_list,
        prediction_raw_list=prediction_raw_list,
        label_list=label_list,
        sample_ids=sample_ids,
    )
    pipeline = PCBEvalPipeline(config or EvalConfig())
    results = [pipeline.evaluate(sample).to_dict() for sample in samples]
    return results, summarize_results(results)
