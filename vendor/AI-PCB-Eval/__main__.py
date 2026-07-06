from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import summarize_results
from .batch_loader import load_samples_from_dirs
from .io_utils import dump_jsonl
from .pipeline import PCBEvalPipeline
from .types import EvalConfig, ensure_dir


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PCB escape-routing evaluation pipeline")
    parser.add_argument("--incomplete-dir", required=True, help="Directory of incomplete KiCad boards.")
    parser.add_argument("--prediction-dir", required=True, help="Directory of model raw reply files.")
    parser.add_argument("--label-dir", required=True, help="Directory of label files.")
    parser.add_argument("--output-dir", required=True, help="Directory for result files.")
    parser.add_argument("--alpha", type=float, default=0.5, help="Final score weight for s1.")
    parser.add_argument(
        "--allow-non-kicad",
        action="store_true",
        help="Allow prediction text without KiCad code blocks.",
    )
    parser.add_argument(
        "--fill-placeholder",
        default="<<<MISSING_KICAD_CODE>>>",
        help="Placeholder to replace in context_kicad.",
    )
    parser.add_argument(
        "--drc-command",
        default=None,
        help="DRC command template, use {board_path} as the board file placeholder.",
    )
    parser.add_argument(
        "--drc-timeout-sec",
        type=int,
        default=120,
        help="Timeout for the DRC command.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    config = EvalConfig(
        alpha=args.alpha,
        require_kicad_code=not args.allow_non_kicad,
        fill_placeholder=args.fill_placeholder,
        drc_command=args.drc_command,
        drc_timeout_sec=args.drc_timeout_sec,
    )
    pipeline = PCBEvalPipeline(config)
    output_dir = ensure_dir(Path(args.output_dir))
    samples = load_samples_from_dirs(
        Path(args.incomplete_dir),
        Path(args.prediction_dir),
        Path(args.label_dir),
    )

    results = [pipeline.evaluate(sample).to_dict() for sample in samples]
    dump_jsonl(output_dir / "results.jsonl", results)
    summary = summarize_results(results)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
