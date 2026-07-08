from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Literal


RerouteStatus = Literal["passed", "failed", "routing_failed", "fill_failed", "drc_failed"]


@dataclass
class RerouteInput:
    task_id: str
    context_kicad: str
    routing_task_prompt: str
    output_dir: str
    board_id: str = ""
    target_bga: str = ""
    model: str = ""
    max_rounds: int = 2
    samples: int = 2
    repair_samples: int = 2
    repair_retries: int = 2


@dataclass
class RerouteOutput:
    status: RerouteStatus
    task_id: str
    routing_patch: str
    completed_kicad: str
    completed_kicad_path: str
    drc_violation: int
    success: bool
    metrics: Dict[str, Any] = field(default_factory=dict)
    drc_report: Dict[str, Any] = field(default_factory=dict)
    debug: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

