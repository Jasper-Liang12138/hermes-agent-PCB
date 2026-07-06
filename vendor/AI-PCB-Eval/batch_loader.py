from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

from .types import SampleInput


def _collect_files(root: Path) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        key = str(rel.with_suffix("")).replace("\\", "/")
        files[key] = path
    return files


def load_samples_from_dirs(
    incomplete_dir: Path,
    prediction_dir: Path,
    label_dir: Path,
) -> List[SampleInput]:
    incomplete_map = _collect_files(incomplete_dir)
    prediction_map = _collect_files(prediction_dir)
    label_map = _collect_files(label_dir)

    sample_ids = sorted(set(incomplete_map) & set(prediction_map) & set(label_map))
    if not sample_ids:
        raise ValueError("No matched samples found across incomplete, prediction, and label directories.")

    missing_incomplete = sorted((set(prediction_map) & set(label_map)) - set(incomplete_map))
    missing_prediction = sorted((set(incomplete_map) & set(label_map)) - set(prediction_map))
    missing_label = sorted((set(incomplete_map) & set(prediction_map)) - set(label_map))
    if missing_incomplete or missing_prediction or missing_label:
        raise ValueError(
            "Unmatched sample files detected: "
            f"missing_incomplete={missing_incomplete[:5]}, "
            f"missing_prediction={missing_prediction[:5]}, "
            f"missing_label={missing_label[:5]}"
        )

    samples: List[SampleInput] = []
    for sample_id in sample_ids:
        samples.append(
            SampleInput(
                sample_id=sample_id,
                context_kicad=incomplete_map[sample_id].read_text(encoding="utf-8"),
                label=label_map[sample_id].read_text(encoding="utf-8"),
                prediction_raw=prediction_map[sample_id].read_text(encoding="utf-8"),
                prompt="",
                meta={
                    "incomplete_path": str(incomplete_map[sample_id]),
                    "prediction_path": str(prediction_map[sample_id]),
                    "label_path": str(label_map[sample_id]),
                },
            )
        )
    return samples


def build_samples_from_lists(
    incomplete_kicad_list: Sequence[str],
    prediction_raw_list: Sequence[str],
    label_list: Sequence[str],
    sample_ids: Sequence[str] | None = None,
) -> List[SampleInput]:
    count = len(incomplete_kicad_list)
    if len(prediction_raw_list) != count or len(label_list) != count:
        raise ValueError("incomplete_kicad_list, prediction_raw_list, and label_list must have the same length.")

    if sample_ids is None:
        sample_ids = [f"sample_{idx:06d}" for idx in range(count)]
    elif len(sample_ids) != count:
        raise ValueError("sample_ids must have the same length as the input lists.")

    return [
        SampleInput(
            sample_id=sample_ids[idx],
            context_kicad=incomplete_kicad_list[idx],
            prediction_raw=prediction_raw_list[idx],
            label=label_list[idx],
            prompt="",
            meta={},
        )
        for idx in range(count)
    ]
