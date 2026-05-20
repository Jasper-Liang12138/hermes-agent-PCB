"""BJUT (0518) / RL (bk_routing) router adapter for BGA fanout.

Pipeline (per router README):
  1. layer_assign_cpp  (+ -arc for arc family)
  2. escape_order_cpp
  3. 135_main / arc_main

Also exposes fanout param generation (steps 1-2 only) for WebSocket fanoutParams.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUPPORTED_ROUTER_TYPES = frozenset({"arc", "135", "rl", "rl_arc", "rl_135"})
_ROUTER_TYPE_ALIASES = {
    "arc": "arc",
    "arc_linux": "arc",
    "curve": "arc",
    "弧形": "arc",
    "圆弧": "arc",
    "135": "135",
    "135_linux": "135",
    "router135": "135",
    "135度": "135",
    "折角": "135",
    "rl": "rl",
    "rl_router": "rl",
    "rl_135": "rl_135",
    "rl_arc": "rl_arc",
    "bk_routing": "rl",
}


@dataclass
class RouterRunOutputs:
    routing_result_path: Path
    import_lines_path: Path
    report: str


def normalize_router_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower().replace("-", "_")
    return _ROUTER_TYPE_ALIASES.get(normalized, normalized)


def router_execution_family(router_type: str) -> str:
    """Map public routerType to arc/135 execution family."""
    normalized = normalize_router_type(router_type)
    if normalized in {"135", "rl", "rl_135"}:
        return "135"
    return "arc"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _config_paths() -> list[Path]:
    paths: list[Path] = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        paths.append(Path(bundled) / "config.ini")
    paths.append(_repo_root() / "config.ini")
    return paths


def load_router_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    for path in _config_paths():
        if path.is_file():
            parser.read(path, encoding="utf-8")
            break
    return parser


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value.strip())))


def _config_path(parser: configparser.ConfigParser, section: str, key: str) -> Path | None:
    if parser.has_option(section, key):
        raw = parser.get(section, key, fallback="").strip()
        if raw:
            return _expand_path(raw)
    return None


def _auto_rl_subdir(rl_root: Path, family: str) -> Path | None:
    if not rl_root.is_dir():
        return None
    platform_tag = "windows" if sys.platform == "win32" else "linux"
    preferred = rl_root / f"{family}_{platform_tag}_0519"
    if preferred.is_dir():
        return preferred
    matches = sorted(rl_root.glob(f"{family}_*"))
    return matches[0] if matches else None


def resolve_router_dir(router_type: str, work_dir: Path | None = None) -> Path:
    normalized = normalize_router_type(router_type)
    env_map = {
        "arc": ("ROUTER_ARC_DIR", "ARC_ROUTER_DIR"),
        "135": ("ROUTER_135_DIR", "ROUTER135_DIR"),
        "rl_arc": ("ROUTER_RL_ARC_DIR", "RL_ARC_ROUTER_DIR"),
        "rl_135": ("ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
        "rl": ("ROUTER_RL_DIR", "ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
    }
    for key in env_map.get(normalized, ()):
        value = os.getenv(key, "").strip()
        if value:
            return _expand_path(value)

    parser = load_router_config()
    if parser.has_section("router"):
        key_by_type = {
            "arc": "arc_dir",
            "135": "135_dir",
            "rl_arc": "rl_arc_dir",
            "rl_135": "rl_135_dir",
            "rl": "rl_135_dir",
        }
        config_key = key_by_type.get(normalized)
        if config_key:
            configured = _config_path(parser, "router", config_key)
            if configured:
                return configured
        if normalized.startswith("rl"):
            rl_root = _config_path(parser, "router", "rl_root_dir")
            if rl_root:
                auto = _auto_rl_subdir(rl_root, router_execution_family(normalized))
                if auto:
                    return auto

    if work_dir is not None:
        return work_dir
    return Path(".")


def _find_binary(router_dir: Path, stem: str) -> Path | None:
    candidates = [stem, f"{stem}.exe"]
    if stem.endswith("_cpp"):
        candidates.extend([stem.replace("_cpp", ""), f"{stem.replace('_cpp', '')}.exe"])
    for name in candidates:
        path = router_dir / name
        if path.is_file():
            return path
    return None


def bjut_router_available(router_type: str, work_dir: Path | None = None) -> bool:
    router_dir = resolve_router_dir(router_type, work_dir=work_dir)
    if not router_dir.is_dir():
        return False
    if _find_binary(router_dir, "layer_assign_cpp") is None:
        return False
    if _find_binary(router_dir, "escape_order_cpp") is None:
        return False
    family = router_execution_family(router_type)
    main_stem = "135_main" if family == "135" else "arc_main"
    return _find_binary(router_dir, main_stem) is not None


def _router_binary_args(binary_path: Path, *args: str) -> list[str]:
    header = binary_path.read_bytes()[:4]
    if header == b"\x7fELF":
        return [str(binary_path), *args]
    if header[:2] == b"MZ":
        if sys.platform == "win32":
            return [str(binary_path), *args]
        raise RuntimeError(
            f"{binary_path.name} 是 Windows 可执行文件，当前 {sys.platform} 环境无法直接运行；"
            "请配置 rl_*_linux 目录或在 Windows 上执行布线。"
        )
    return [sys.executable, str(binary_path), *args]


def _run_process(args: list[str], work_dir: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    logger.info("Executing BJUT router step: %s in %s", args, work_dir)
    return subprocess.run(
        args,
        cwd=work_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _require_success(proc: subprocess.CompletedProcess, step: str) -> None:
    if proc.returncode == 0:
        return
    output = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
    raise RuntimeError(f"{step} 执行失败 (exit {proc.returncode}):\n{output[:1000]}")


def write_layout_inputs(work_dir: Path, project_data: str) -> Path:
    layout_path = work_dir / "layout_input.txt"
    layout_path.write_text(project_data, encoding="utf-8")
    legacy = work_dir / "版图信息.txt"
    if legacy.resolve() != layout_path.resolve():
        legacy.write_text(project_data, encoding="utf-8")
    return layout_path


def write_component_input(work_dir: Path, component_refdes: str) -> Path:
    path = work_dir / "component_input.txt"
    path.write_text(f"{component_refdes.strip()}\n", encoding="utf-8")
    return path


def write_order_input(work_dir: Path, order_lines: list[dict[str, Any]], component_refdes: str) -> Path:
    body = "\n".join(
        f"{item['net']} {item['layer']} {item['order']}"
        for item in order_lines
        if item.get("net") and item.get("layer")
    )
    text = f"{body}\n\n{component_refdes.strip()}"
    path = work_dir / "order_input.txt"
    path.write_text(text, encoding="utf-8")
    return path


def write_arc_constrain(work_dir: Path, constraints: Any) -> Path:
    line_width = 3.0
    line_spacing = 4.5
    if isinstance(constraints, dict):
        try:
            if constraints.get("LineWidth") is not None:
                line_width = float(constraints["LineWidth"])
        except (TypeError, ValueError):
            pass
        try:
            if constraints.get("LineSpacing") is not None:
                line_spacing = float(constraints["LineSpacing"])
        except (TypeError, ValueError):
            pass
    path = work_dir / "constrain.txt"
    path.write_text(
        f"CrossLayer:1\nviaradius: 8\nkeepoutlength: 9.5\nkeepoutradius: 14.0\n"
        f"LineWidth:{line_width:g}\nLineSpacing:{line_spacing:g}\n",
        encoding="utf-8",
    )
    return path


def parse_order_input_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return {"selectedBGA": "", "orderLines": [], "constraints": {}}

    selected_bga = lines[0]
    index = 1
    constraints: dict[str, Any] = {}
    while index < len(lines):
        parts = lines[index].split()
        if len(parts) == 1:
            try:
                number = float(parts[0])
            except ValueError:
                break
            if "LineWidth" not in constraints:
                constraints["LineWidth"] = number
            elif "LineSpacing" not in constraints:
                constraints["LineSpacing"] = number
            else:
                break
            index += 1
            continue
        break

    order_lines: list[dict[str, Any]] = []
    for line in lines[index:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        net, layer, order_text = parts[0], parts[1], parts[2]
        try:
            order = int(order_text)
        except ValueError:
            continue
        order_lines.append({"net": net, "layer": layer, "order": order})

    order_lines.sort(key=lambda item: item.get("order", 0))
    for idx, item in enumerate(order_lines, start=1):
        item["order"] = idx
    return {
        "selectedBGA": selected_bga,
        "orderLines": order_lines,
        "constraints": constraints,
    }


def parse_order_input_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"未生成 order_input.txt: {path}")
    return parse_order_input_text(path.read_text(encoding="utf-8", errors="replace"))


def _run_layer_assign(
    work_dir: Path,
    router_dir: Path,
    router_type: str,
    layout_name: str,
    component_name: str,
) -> None:
    binary = _find_binary(router_dir, "layer_assign_cpp")
    if binary is None:
        raise FileNotFoundError(f"缺少 layer_assign_cpp (router_dir={router_dir})")
    args = [layout_name, component_name, "--output", "layer_input.txt"]
    if router_execution_family(router_type) == "arc":
        args = ["-arc", *args]
    _require_success(
        _run_process(_router_binary_args(binary, *args), work_dir),
        "layer_assign_cpp",
    )
    layer_path = work_dir / "layer_input.txt"
    if not layer_path.is_file() or layer_path.stat().st_size <= 0:
        raise RuntimeError("layer_assign_cpp 未生成有效 layer_input.txt")


def _run_escape_order(work_dir: Path, router_dir: Path, layout_name: str) -> None:
    binary = _find_binary(router_dir, "escape_order_cpp")
    if binary is None:
        raise FileNotFoundError(f"缺少 escape_order_cpp (router_dir={router_dir})")
    _require_success(
        _run_process(_router_binary_args(binary, "layer_input.txt", layout_name), work_dir),
        "escape_order_cpp",
    )
    order_path = work_dir / "order_input.txt"
    if not order_path.is_file() or order_path.stat().st_size <= 0:
        raise RuntimeError("escape_order_cpp 未生成有效 order_input.txt")


def _run_router_main(work_dir: Path, router_dir: Path, router_type: str, layout_name: str) -> None:
    family = router_execution_family(router_type)
    main_stem = "135_main" if family == "135" else "arc_main"
    binary = _find_binary(router_dir, main_stem)
    if binary is None:
        raise FileNotFoundError(f"缺少 {main_stem} (router_dir={router_dir})")
    if family == "135":
        args = [layout_name, "order_input.txt"]
    else:
        args = ["order_input.txt", layout_name, "constrain.txt"]
    _require_success(_run_process(_router_binary_args(binary, *args), work_dir), main_stem)


def _read_report(work_dir: Path) -> str:
    report_path = work_dir / "data.txt"
    if report_path.is_file():
        return report_path.read_text(encoding="utf-8", errors="replace").strip()
    return "布线完成（无详细报告）"


def _resolve_import_lines_path(work_dir: Path, router_type: str) -> Path:
    family = router_execution_family(router_type)
    candidates = ("line.out",) if family == "135" else ("ARC_output.txt", "arc_output.txt")
    for name in candidates:
        path = work_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    raise FileNotFoundError(f"{router_type} 布线器未生成可导入 importLines 的原始记录文件")


def _resolve_routing_result_path(work_dir: Path, router_type: str, router_dir: Path, layout_path: Path) -> Path:
    routing_path = work_dir / "routing_input.txt"
    if routing_path.is_file() and routing_path.stat().st_size > 0:
        return routing_path.resolve()

    turn_script = None
    family = router_execution_family(router_type)
    if family == "135":
        turn_script = router_dir / "Turn_135_QYF.py"
        record_name = "line.out"
    else:
        turn_script = router_dir / "Turn_QYF.py"
        record_name = "ARC_output.txt"

    record_path = work_dir / record_name
    if turn_script.is_file() and record_path.is_file():
        from tools import pcb_tools

        _require_success(
            pcb_tools._run_python_script_inprocess(
                turn_script,
                work_dir,
                layout_path.name,
                record_name,
                "routing_input.txt",
            ),
            f"{turn_script.name}",
        )
        if routing_path.is_file() and routing_path.stat().st_size > 0:
            return routing_path.resolve()

    if layout_path.is_file():
        shutil.copy2(layout_path, routing_path)
        if family == "135" and (work_dir / "line.out").is_file():
            from tools import pcb_tools

            pcb_tools._repair_135_routing_wires(work_dir)
        if routing_path.is_file() and routing_path.stat().st_size > 0:
            return routing_path.resolve()

    raise FileNotFoundError("布线器未生成 routing_input.txt")


def generate_fanout_params(
    *,
    project_data: str,
    selected_bga: str,
    router_type: str,
    work_dir: Path,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run layer_assign + escape_order and convert order_input.txt to fanoutParams."""
    normalized = normalize_router_type(router_type)
    if normalized not in SUPPORTED_ROUTER_TYPES:
        raise ValueError(f"不支持的 routerType: {router_type}")

    work_dir.mkdir(parents=True, exist_ok=True)
    router_dir = resolve_router_dir(normalized, work_dir=work_dir)
    if not bjut_router_available(normalized, work_dir=work_dir):
        raise RuntimeError(f"BJUT 布线器不可用: routerType={normalized}, dir={router_dir}")

    layout_path = write_layout_inputs(work_dir, project_data)
    write_component_input(work_dir, selected_bga)
    if router_execution_family(normalized) == "arc":
        write_arc_constrain(work_dir, constraints or {})

    _run_layer_assign(work_dir, router_dir, normalized, layout_path.name, "component_input.txt")
    _run_escape_order(work_dir, router_dir, layout_path.name)

    parsed = parse_order_input_file(work_dir / "order_input.txt")
    merged_constraints = dict(constraints or {})
    merged_constraints.update(parsed.get("constraints") or {})

    order_lines = parsed.get("orderLines") or []
    if not order_lines:
        layer_path = work_dir / "layer_input.txt"
        if layer_path.is_file():
            order_lines = [
                {"net": net, "layer": layer, "order": idx + 1}
                for idx, (net, layer) in enumerate(_parse_layer_input_pairs(layer_path))
            ]

    return {
        "selectedBGA": selected_bga or parsed.get("selectedBGA") or "",
        "routerType": normalized,
        "orderLines": order_lines,
        "constraints": merged_constraints or {"LineWidth": 4, "LineSpacing": 3},
    }


def _parse_layer_input_pairs(layer_path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in layer_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def run_bjut_route(
    *,
    project_data: str,
    fanout_params: dict[str, Any],
    work_dir: Path,
) -> RouterRunOutputs:
    router_type = normalize_router_type(fanout_params.get("routerType") or "")
    if router_type not in SUPPORTED_ROUTER_TYPES:
        raise ValueError(f"不支持的 routerType: {router_type}")

    selected_bga = str(fanout_params.get("selectedBGA") or "").strip()
    order_lines = fanout_params.get("orderLines") or []
    if not selected_bga:
        raise ValueError("fanoutParams.selectedBGA 不能为空")
    if not order_lines:
        raise ValueError("fanoutParams.orderLines 不能为空")

    work_dir.mkdir(parents=True, exist_ok=True)
    router_dir = resolve_router_dir(router_type, work_dir=work_dir)
    if not bjut_router_available(router_type, work_dir=work_dir):
        raise RuntimeError(f"BJUT 布线器不可用: routerType={router_type}, dir={router_dir}")

    layout_path = write_layout_inputs(work_dir, project_data)
    write_component_input(work_dir, selected_bga)
    write_order_input(work_dir, order_lines, selected_bga)
    constraints = fanout_params.get("constraints") or {}
    if router_execution_family(router_type) == "arc":
        write_arc_constrain(work_dir, constraints)

    _run_router_main(work_dir, router_dir, router_type, layout_path.name)

    routing_result = _resolve_routing_result_path(work_dir, router_type, router_dir, layout_path)
    import_lines = _resolve_import_lines_path(work_dir, router_type)
    report = _read_report(work_dir)
    return RouterRunOutputs(
        routing_result_path=routing_result,
        import_lines_path=import_lines,
        report=report or "布线完成（无详细报告）",
    )
