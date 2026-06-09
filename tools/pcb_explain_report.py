"""Local explainability report generation for PCB reroute results."""

from __future__ import annotations

import configparser
import importlib
import os
import subprocess
import sys
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
        parser.read(config_path, encoding="utf-8-sig")
    return parser


def _config_value(parser: configparser.ConfigParser, section: str, option: str) -> str:
    if not parser.has_section(section):
        return ""
    return parser.get(section, option, fallback="").strip()


def _expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def _resolve_existing_path(value: str | os.PathLike[str]) -> Path:
    raw = _expand_path(value)
    if raw.is_absolute():
        return raw
    for base in (Path.cwd(), _repo_root().parent, _repo_root()):
        candidate = (base / raw).resolve()
        if candidate.exists():
            return candidate
    return (_repo_root().parent / raw).resolve()


def resolve_checkpoint_path(config: configparser.ConfigParser | None = None) -> Path:
    parser = config if config is not None else _load_project_config()
    configured = (
        os.getenv("PCB_EXPLAIN_CHECKPOINT", "").strip()
        or _config_value(parser, "explain", "checkpoint_path")
        or _config_value(parser, "explain_model", "checkpoint_path")
    )
    if configured:
        return _resolve_existing_path(configured)
    return _resolve_existing_path(DEFAULT_CHECKPOINT_RELATIVE_PATH)


def resolve_output_root(config: configparser.ConfigParser | None = None) -> Path:
    parser = config if config is not None else _load_project_config()
    configured = (
        os.getenv("PCB_EXPLAIN_OUTPUT_ROOT", "").strip()
        or _config_value(parser, "explain", "output_root")
        or _config_value(parser, "explain_model", "output_root")
    )
    if configured:
        return _resolve_existing_path(configured)
    return DEFAULT_OUTPUT_ROOT


def resolve_python_executable(config: configparser.ConfigParser | None = None) -> Path | None:
    parser = config if config is not None else _load_project_config()
    configured = (
        os.getenv("PCB_EXPLAIN_PYTHON", "").strip()
        or _config_value(parser, "explain", "python")
        or _config_value(parser, "explain", "python_executable")
        or _config_value(parser, "explain_model", "python")
        or _config_value(parser, "explain_model", "python_executable")
    )
    candidates: list[Path] = []
    if configured:
        candidates.append(_resolve_existing_path(configured))
    exe_name = "python.exe" if sys.platform == "win32" else "python"
    candidates.extend(
        [
            _repo_root() / "python_runtime" / exe_name,
            _repo_root().parent / "python_runtime" / exe_name,
            _repo_root() / "python_runtime" / "bin" / "python",
            _repo_root().parent / "python_runtime" / "bin" / "python",
        ]
    )
    current = Path(sys.executable).resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved != current:
            return resolved
    return None


def _load_local_infer_module():
    try:
        return importlib.import_module("tools.pcb_explain_classifier.infer_ascend_multiview_classifier")
    except ModuleNotFoundError as exc:
        missing = str(exc).split("'")
        package = missing[1] if len(missing) >= 2 else str(exc)
        raise RuntimeError(
            f"local explain dependency is missing: {package}; install the pcb-explain optional dependencies"
        ) from exc


def _run_external_infer(
    *,
    python_executable: Path,
    board_path: Path,
    checkpoint: Path,
    output_root: Path,
) -> str:
    package_dir = _repo_root() / "tools" / "pcb_explain_classifier"
    if not (package_dir / "infer_ascend_multiview_classifier.py").is_file():
        raise RuntimeError("local explain classifier script is missing")
    env = os.environ.copy()
    pythonpath_items = [str(_repo_root())]
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_items.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_items)
    proc = subprocess.run(
        [
            str(python_executable),
            "-m",
            "tools.pcb_explain_classifier.infer_ascend_multiview_classifier",
            str(board_path),
            str(checkpoint),
            "--output-root",
            str(output_root),
        ],
        cwd=str(_repo_root()),
        env=env,
        text=True,
        capture_output=True,
        timeout=float(os.getenv("PCB_EXPLAIN_LOCAL_TIMEOUT", "300")),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if "ModuleNotFoundError" in detail and "No module named" in detail:
            try:
                missing = detail.split("No module named", 1)[1].splitlines()[0].strip().strip("'\"")
            except Exception:
                missing = detail
            raise RuntimeError(f"python_runtime explain dependency is missing: {missing}") from None
        raise RuntimeError(f"python_runtime explain failed: {detail[:800]}")
    report_text = str(proc.stdout or "").strip()
    if not report_text:
        raise RuntimeError("python_runtime explain classifier returned empty report")
    return report_text


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
    external_python = resolve_python_executable()
    if external_python:
        return _run_external_infer(
            python_executable=external_python,
            board_path=board_path,
            checkpoint=checkpoint,
            output_root=report_output_root,
        )

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
