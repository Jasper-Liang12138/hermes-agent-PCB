"""Local explainability report generation for PCB reroute results."""

from __future__ import annotations

import configparser
import importlib
import os
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_CHECKPOINT_RELATIVE_PATH = Path("model") / "best.pt"
DEFAULT_OUTPUT_ROOT = Path(tempfile.gettempdir()) / "hermes_pcb_explain"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _project_config_path() -> Path:
    return _repo_root() / "config.ini"


def _load_project_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    config_path = _project_config_path()
    if config_path.is_file():
        parser.read(config_path, encoding="utf-8")
    return parser


def _config_value(parser: configparser.ConfigParser, section: str, option: str) -> str:
    if not parser.has_section(section):
        return ""
    return parser.get(section, option, fallback="").strip()


def _expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def resolve_checkpoint_path(config: configparser.ConfigParser | None = None) -> Path:
    parser = config if config is not None else _load_project_config()
    configured = (
        os.getenv("PCB_EXPLAIN_CHECKPOINT", "").strip()
        or _config_value(parser, "explain", "checkpoint_path")
        or _config_value(parser, "explain_model", "checkpoint_path")
    )
    if configured:
        return _expand_path(configured)
    return _repo_root() / DEFAULT_CHECKPOINT_RELATIVE_PATH


def resolve_output_root(config: configparser.ConfigParser | None = None) -> Path:
    parser = config if config is not None else _load_project_config()
    configured = (
        os.getenv("PCB_EXPLAIN_OUTPUT_ROOT", "").strip()
        or _config_value(parser, "explain", "output_root")
        or _config_value(parser, "explain_model", "output_root")
    )
    if configured:
        return _expand_path(configured)
    return DEFAULT_OUTPUT_ROOT


def _load_local_infer_module():
    try:
        return importlib.import_module("tools.pcb_explain_classifier.infer_ascend_multiview_classifier")
    except ModuleNotFoundError as exc:
        missing = str(exc).split("'")
        package = missing[1] if len(missing) >= 2 else str(exc)
        raise RuntimeError(
            f"local explain dependency is missing: {package}; install the pcb-explain optional dependencies"
        ) from exc


def generate_explain_report(
    *,
    board_file_path: str,
    reroute_result: dict[str, Any] | None = None,
    check_report: dict[str, Any] | None = None,
    checkpoint_path: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> str:
    """Run the local classifier report pipeline and return frontend-ready text."""
    del reroute_result, check_report  # Reserved for future richer report inputs.

    if not str(board_file_path or "").strip():
        raise RuntimeError("internal board file is unavailable for explain report generation")
    board_path = _expand_path(board_file_path)
    if not board_path.is_file():
        raise RuntimeError("internal board file is unavailable for explain report generation")

    checkpoint = _expand_path(checkpoint_path) if checkpoint_path else resolve_checkpoint_path()
    if not checkpoint.is_file():
        raise RuntimeError(
            "local explain checkpoint is missing; set PCB_EXPLAIN_CHECKPOINT "
            "or place best.pt under model/"
        )

    report_output_root = _expand_path(output_root) if output_root else resolve_output_root()
    infer_module = _load_local_infer_module()
    report = infer_module.infer_file(
        board_path,
        checkpoint,
        output_root=report_output_root,
    )
    report_text = str(report or "").strip()
    if not report_text:
        raise RuntimeError("local explain classifier returned empty report")
    return report_text
