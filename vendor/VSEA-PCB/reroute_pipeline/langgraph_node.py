from __future__ import annotations

from typing import Any, Dict

from .agent import RerouteAgent
from .schemas import RerouteInput


def vsea_reroute_node(state: Dict[str, Any]) -> Dict[str, Any]:
    request = RerouteInput(
        task_id=str(state.get("task_id") or "reroute_task"),
        context_kicad=str(state.get("context_kicad") or ""),
        routing_task_prompt=str(state.get("routing_task_prompt") or ""),
        output_dir=str(state.get("output_dir") or state.get("reroute_output_dir") or "outputs/reroute_pipeline"),
        board_id=str(state.get("board_id") or ""),
        target_bga=str(state.get("target_bga") or ""),
        model=str(state.get("model") or ""),
        max_rounds=int(state.get("max_rounds") or 2),
        samples=int(state.get("samples") or 2),
        repair_samples=int(state.get("repair_samples") or 2),
        repair_retries=int(state.get("repair_retries") or 2),
    )
    result = RerouteAgent.from_env().run(request)
    update = dict(state)
    update["vsea_reroute"] = result.to_dict()
    if result.success:
        update["completed_kicad"] = result.completed_kicad
        update["completed_kicad_path"] = result.completed_kicad_path
        update.pop("error", None)
    else:
        update["error"] = result.error or result.status
    return update

