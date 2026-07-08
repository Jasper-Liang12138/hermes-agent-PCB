from __future__ import annotations

import argparse
from pathlib import Path

from reroute_pipeline import RerouteAgent, RerouteInput


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one reroute_pipeline task.")
    parser.add_argument("--context", required=True, help="Path to *_incomplete.kicad_pcb")
    parser.add_argument("--prompt", required=True, help="Path to missing-route prompt text")
    parser.add_argument("--task-id", default="manual")
    parser.add_argument("--output-dir", default="outputs/reroute_pipeline/manual")
    parser.add_argument("--target-bga", default="")
    args = parser.parse_args()

    result = RerouteAgent.from_env().run(
        RerouteInput(
            task_id=args.task_id,
            context_kicad=Path(args.context).read_text(encoding="utf-8"),
            routing_task_prompt=Path(args.prompt).read_text(encoding="utf-8"),
            output_dir=args.output_dir,
            target_bga=args.target_bga,
        )
    )
    print(result.to_dict())


if __name__ == "__main__":
    main()

