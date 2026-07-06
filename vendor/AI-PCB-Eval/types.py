from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SampleInput:
    sample_id: str
    context_kicad: str
    label: str
    prediction_raw: str
    prompt: str = ""
    hole_spec: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalConfig:
    alpha: float = 0.5
    require_kicad_code: bool = True
    extraction_mode: str = "auto"
    fill_placeholder: str = "<<<MISSING_KICAD_CODE>>>"
    drc_command: Optional[str] = None
    drc_timeout_sec: int = 120
    semantic_kicad_weight: float = 0.85
    semantic_text_weight: float = 0.15


@dataclass
class SemanticScore:
    score: float
    has_kicad_code: bool
    prediction_code: str
    extracted_blocks: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FillResult:
    success: bool
    completed_kicad: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DRCResult:
    score: float
    success: bool
    violations: int = 0
    warnings: int = 0
    raw_output: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineResult:
    sample_id: str
    s1: float
    s2: float
    final_score: float
    status: str
    prediction_code: str = ""
    has_kicad_code: bool = False
    semantic_detail: Dict[str, Any] = field(default_factory=dict)
    fill_detail: Dict[str, Any] = field(default_factory=dict)
    drc_detail: Dict[str, Any] = field(default_factory=dict)
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
