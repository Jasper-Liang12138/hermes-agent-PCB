"""Helpers for KiCad reroute patch fill-in and DRC validation.

This module intentionally imports the eval submodule dynamically.  The
submodule directory is named ``AI-PCB-Eval``, which is not a valid Python
package name for normal dotted imports.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONFIGURED_EVAL_ROOT: Path | None = None


@dataclass
# ====== 功能：描述一次 reroute DRC 校验尝试的结果数据。 ======
class RerouteDrcAttempt:
    iteration: int
    passed: bool
    filled_board_data_file_path: str = ""
    fill_detail: dict[str, Any] = field(default_factory=dict)
    drc_result: dict[str, Any] = field(default_factory=dict)
    failure_summary: str = ""


@dataclass
# ====== 功能：描述 reroute DRC 校验的整体输出数据。 ======
class RerouteDrcValidation:
    passed: bool
    routed_board_data_file_path: str = ""
    original_board_data_file_path: str = ""
    attempts: list[RerouteDrcAttempt] = field(default_factory=list)
    last_failure_summary: str = ""


# ====== 功能：定位当前工具脚本所在项目根目录。 ======
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# ====== 功能：设置 AI-PCB-Eval 工具根目录。 ======
def set_eval_root(path: str | Path) -> None:
    global _CONFIGURED_EVAL_ROOT
    _CONFIGURED_EVAL_ROOT = Path(path)


# ====== 功能：获取当前 DRC 评测根目录。 ======
def _eval_root() -> Path:
    return _CONFIGURED_EVAL_ROOT or (_repo_root() / "vendor" / "AI-PCB-Eval")


# ====== 功能：从文件路径动态加载 Python 模块。 ======
def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ====== 功能：加载 AI-PCB-Eval 需要的辅助模块。 ======
def _load_eval_helpers():
    root = _eval_root()
    if not root.exists():
        raise FileNotFoundError(f"AI-PCB-Eval submodule is missing: {root}")

    fill_module = _load_module(
        "_hermes_ai_pcb_eval_patch",
        root / "patch_kicad_from_raw_standalone.py",
    )

    backend_root = root / "drc_backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    drc_module = _load_module("_hermes_ai_pcb_eval_drc_api", backend_root / "api.py")
    return fill_module.fill_incomplete_board_from_raw_text, drc_module.evaluate_drc_score


# ====== 功能：把样本名转换为安全文件名。 ======
def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned[:80] or "reroute"


# ====== 功能：汇总 DRC 失败原因。 ======
def _summarize_drc_failure(drc_result: dict[str, Any], fill_detail: dict[str, Any] | None = None) -> str:
    if not isinstance(drc_result, dict):
        return "DRC returned an invalid result."
    if drc_result.get("pass") is True:
        return ""

    error = drc_result.get("error")
    parts: list[str] = []
    if error:
        parts.append(f"DRC error: {json.dumps(error, ensure_ascii=False)}")

    details = drc_result.get("details") or {}
    issue_count = details.get("hard_issue_count")
    if issue_count is not None:
        parts.append(f"hard_issue_count={issue_count}")
    rule_counts = details.get("hard_rule_counts") or {}
    if rule_counts:
        parts.append(f"hard_rule_counts={json.dumps(rule_counts, ensure_ascii=False)}")

    issues = (drc_result.get("artifacts") or {}).get("issues") or []
    if issues:
        compact = []
        for issue in issues[:5]:
            if isinstance(issue, dict):
                compact.append(
                    {
                        "rule": issue.get("rule"),
                        "message": issue.get("message") or issue.get("description"),
                        "severity": issue.get("severity"),
                    }
                )
            else:
                compact.append(str(issue))
        parts.append(f"issues={json.dumps(compact, ensure_ascii=False)}")

    if fill_detail:
        parts.append(f"fill_detail={json.dumps(fill_detail, ensure_ascii=False)}")
    return "; ".join(part for part in parts if part) or "DRC failed without detailed issues."


# ====== 功能：把布线 patch 转为 KiCad 并运行 DRC。 ======
def validate_kicad_patch_with_drc(
    *,
    original_board_data: str,
    model_output_text: str,
    output_dir: str | Path | None = None,
    sample_id: str = "reroute",
    iteration: int = 1,
) -> RerouteDrcAttempt:
    """Fill model generated KiCad objects into a board and run hard DRC."""
    fill_incomplete_board_from_raw_text, evaluate_drc_score = _load_eval_helpers()

    try:
        fill_result = fill_incomplete_board_from_raw_text(
            model_output_text,
            original_board_data,
            ensure_unique=True,
        )
        filled_text = fill_result.filled_pcb_text
        fill_detail = {
            "segments_count": fill_result.segments_count,
            "vias_count": fill_result.vias_count,
            "other_lines_count": fill_result.other_lines_count,
        }
    except Exception as exc:
        return RerouteDrcAttempt(
            iteration=iteration,
            passed=False,
            fill_detail={"reason": "fill_failed", "error": str(exc)},
            failure_summary=f"KiCad patch fill failed: {exc}",
        )

    base_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir()) / "hermes_pcb_reroute"
    base_dir.mkdir(parents=True, exist_ok=True)
    output_path = base_dir / f"{_safe_name(sample_id)}_iter{iteration}.kicad_pcb"
    output_path.write_text(filled_text, encoding="utf-8")

    drc_result = evaluate_drc_score(str(output_path), check_mode="hard")
    passed = bool(drc_result.get("ok")) and bool(drc_result.get("pass"))
    return RerouteDrcAttempt(
        iteration=iteration,
        passed=passed,
        filled_board_data_file_path=str(output_path),
        fill_detail=fill_detail,
        drc_result=drc_result,
        failure_summary="" if passed else _summarize_drc_failure(drc_result, fill_detail),
    )

# ====== 功能：对已经生成的完整 KiCad PCB 文件直接运行 DRC。 ======
def validate_kicad_board_with_drc(
    *,
    board_path: str | Path,
    sample_id: str = "reroute",
    iteration: int = 1,
) -> RerouteDrcAttempt:
    """Run hard DRC on a complete routed .kicad_pcb file."""
    _fill_unused, evaluate_drc_score = _load_eval_helpers()
    path = Path(board_path)
    if not path.is_file():
        return RerouteDrcAttempt(
            iteration=iteration,
            passed=False,
            failure_summary=f"routed KiCad board does not exist: {path}",
        )
    drc_result = evaluate_drc_score(str(path), check_mode="hard")
    passed = bool(drc_result.get("ok")) and bool(drc_result.get("pass"))
    return RerouteDrcAttempt(
        iteration=iteration,
        passed=passed,
        filled_board_data_file_path=str(path),
        fill_detail={"reason": "complete_board_drc", "sample_id": sample_id},
        drc_result=drc_result,
        failure_summary="" if passed else _summarize_drc_failure(drc_result, {"reason": "complete_board_drc"}),
    )

