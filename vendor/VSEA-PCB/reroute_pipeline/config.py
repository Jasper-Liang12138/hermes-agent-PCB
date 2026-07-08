from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
DEFAULT_SKILL_BANK_PATH = PACKAGE_ROOT / "assets" / "skill_bank.jsonl"


def _env(name: str, fallback: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else fallback


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class RerouteConfig:
    ai_pcb_eval_path: str
    drc_agent_package: str
    llm_api_key: str = ""
    llm_base_url: str = ""
    model: str = "qwen3-32b"
    skill_bank_path: str = ""
    output_dir: str = "outputs/reroute_pipeline"
    timeout_seconds: float = 180.0
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 65536
    target_bga: str = ""
    agent_drc_python: str = ""

    @classmethod
    def from_env(cls) -> "RerouteConfig":
        ai_pcb_eval = _env(
            "REROUTE_AI_PCB_EVAL_PATH",
            _env("AI_PCB_EVAL_PATH", str(REPO_ROOT / "AI-PCB-Eval")),
        )
        drc_package = _env(
            "REROUTE_DRC_AGENT_PACKAGE",
            _env(
                "VSEA_PCB_AGENT_DRC_TOOL",
                str(REPO_ROOT / "external_drc" / "DRC_0623_v2" / "agent_package"),
            ),
        )
        return cls(
            ai_pcb_eval_path=ai_pcb_eval,
            drc_agent_package=drc_package,
            llm_api_key=_env("REROUTE_LLM_API_KEY", _env("LLM_API_KEY")),
            llm_base_url=_env("REROUTE_LLM_BASE_URL", _env("LLM_BASE_URL")),
            model=_env("REROUTE_MODEL", _env("MODEL_ID", "qwen3-32b")),
            skill_bank_path=_env(
                "REROUTE_SKILL_BANK_PATH",
                _env("VSEA_PCB_SKILL_BANK", str(DEFAULT_SKILL_BANK_PATH)),
            ),
            output_dir=_env("REROUTE_OUTPUT_DIR", "outputs/reroute_pipeline"),
            timeout_seconds=_env_float("REROUTE_TIMEOUT_SECONDS", 180.0),
            target_bga=_env("REROUTE_TARGET_BGA", _env("VSEA_PCB_AGENT_DRC_TARGET_BGA")),
            agent_drc_python=_env("REROUTE_AGENT_DRC_PYTHON", _env("VSEA_PCB_AGENT_DRC_PYTHON")),
        )
