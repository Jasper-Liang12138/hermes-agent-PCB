from __future__ import annotations

from pathlib import Path

from reroute_pipeline.langgraph_node import vsea_reroute_node


def run_stub(context_path: str, prompt_path: str) -> dict:
    state = {
        "task_id": "manual",
        "context_kicad": Path(context_path).read_text(encoding="utf-8"),
        "routing_task_prompt": Path(prompt_path).read_text(encoding="utf-8"),
        "output_dir": "outputs/reroute_pipeline/manual",
    }
    return vsea_reroute_node(state)

