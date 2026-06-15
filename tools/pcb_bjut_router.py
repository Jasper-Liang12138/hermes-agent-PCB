"""BJUT (0518) / RL (bk_routing) router adapter for BGA fanout.

Pipeline (per router README):
  1. layer_assign_cpp  (+ -arc for arc family)
  2. escape_order_cpp
  3. 135_main / arc_main

Also exposes fanout param generation (steps 1-2 only) for WebSocket fanoutParams.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUPPORTED_ROUTER_TYPES = frozenset({
    "arc",
    "135",
    "rl",
    "rl_arc",
    "rl_135",
    "ga",
    "ga_arc",
    "ga_135",
    "auto",
    "auto_arc",
    "auto_135",
})
_ROUTER_TYPE_ALIASES = {
    "auto": "auto",
    "auto_router": "auto",
    "auto_135": "auto_135",
    "auto_arc": "auto_arc",
    "智能": "auto",
    "自动": "auto",
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
    "ga": "ga",
    "ga_router": "ga",
    "ga_135": "ga_135",
    "ga_arc": "ga_arc",
    "遗传": "ga",
    "遗传算法": "ga",
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
    if normalized in {"135", "rl", "rl_135", "ga", "ga_135", "auto", "auto_135"}:
        return "135"
    return "arc"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _config_paths() -> list[Path]:
    paths: list[Path] = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / "config.ini")
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        paths.append(Path(bundled) / "config.ini")
    paths.append(_repo_root() / "config.ini")
    return paths


def load_router_config() -> tuple[configparser.ConfigParser, Path | None]:
    parser = configparser.ConfigParser()
    last = None
    for path in _config_paths():
        if path.is_file():
            parser.read(path, encoding="utf-8-sig")
            last = path
            break
    return parser, last.parent.resolve() if last else None


def _expand_path(value: str, base_dir: Path | None = None) -> Path:
    raw = os.path.expanduser(value.strip())
    raw = re.sub(r"%([^%]+)%", lambda match: os.getenv(match.group(1), match.group(0)), raw)
    raw = os.path.expandvars(raw)
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return Path(raw)
    if os.sep == "/" and "\\" in raw:
        raw = raw.replace("\\", "/")
    expanded = Path(raw)
    if not expanded.is_absolute() and base_dir is not None:
        return (base_dir / expanded).resolve()
    return expanded


def _config_path(parser: configparser.ConfigParser, section: str, key: str, base_dir: Path | None = None) -> Path | None:
    if parser.has_option(section, key):
        raw = parser.get(section, key, fallback="").strip()
        if raw:
            return _expand_path(raw, base_dir=base_dir)
    return None


def _router_config_value(key: str) -> tuple[str, Path | None]:
    parser, config_base_dir = load_router_config()
    if parser.has_section("router") and parser.has_option("router", key):
        return parser.get("router", key, fallback="").strip(), config_base_dir
    return "", config_base_dir


def _auto_router_subdir(router_root: Path, family: str) -> Path | None:
    if not router_root.is_dir():
        return None
    platform_tag = "windows" if sys.platform == "win32" else "linux"
    preferred_names = [
        f"{family}_{platform_tag}",
        f"{family}_{platform_tag}_0519",
    ]
    if sys.platform != "win32":
        preferred_names.append(f"{family}_linux")
    else:
        preferred_names.append(f"{family}_windows_0519")
    for name in preferred_names:
        preferred = router_root / name
        if preferred.is_dir():
            return preferred
    matches = sorted(router_root.glob(f"{family}_*"))
    if sys.platform != "win32":
        linux_matches = [path for path in matches if "linux" in path.name.lower()]
        if linux_matches:
            return linux_matches[0]
    return matches[0] if matches else None


def _auto_rl_subdir(rl_root: Path, family: str) -> Path | None:
    return _auto_router_subdir(rl_root, family)


def resolve_router_dir(router_type: str, work_dir: Path | None = None) -> Path:
    normalized = normalize_router_type(router_type)
    env_map = {
        "arc": ("ROUTER_ARC_DIR", "ARC_ROUTER_DIR"),
        "135": ("ROUTER_135_DIR", "ROUTER135_DIR"),
        "rl_arc": ("ROUTER_RL_ARC_DIR", "RL_ARC_ROUTER_DIR"),
        "rl_135": ("ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
        "rl": ("ROUTER_RL_DIR", "ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
        "ga_arc": ("ROUTER_GA_ARC_DIR", "GA_ARC_ROUTER_DIR", "ROUTER_RL_ARC_DIR", "RL_ARC_ROUTER_DIR"),
        "ga_135": ("ROUTER_GA_135_DIR", "GA_135_ROUTER_DIR", "ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
        "ga": ("ROUTER_GA_DIR", "ROUTER_GA_135_DIR", "GA_135_ROUTER_DIR", "ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
        "auto_arc": ("ROUTER_AUTO_ARC_DIR", "ROUTER_RL_ARC_DIR", "RL_ARC_ROUTER_DIR"),
        "auto_135": ("ROUTER_AUTO_135_DIR", "ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
        "auto": ("ROUTER_AUTO_DIR", "ROUTER_AUTO_135_DIR", "ROUTER_RL_135_DIR", "RL_135_ROUTER_DIR"),
    }
    for key in env_map.get(normalized, ()):
        value = os.getenv(key, "").strip()
        if value:
            return _expand_path(value)

    parser, config_base_dir = load_router_config()
    if parser.has_section("router"):
        key_by_type = {
            "arc": "arc_dir",
            "135": "135_dir",
            "rl_arc": "rl_arc_dir",
            "rl_135": "rl_135_dir",
            "rl": "rl_135_dir",
            "ga_arc": "ga_arc_dir",
            "ga_135": "ga_135_dir",
            "ga": "ga_135_dir",
            "auto_arc": "auto_arc_dir",
            "auto_135": "auto_135_dir",
            "auto": "auto_135_dir",
        }
        config_key = key_by_type.get(normalized)
        if config_key:
            configured = _config_path(parser, "router", config_key, base_dir=config_base_dir)
            if configured:
                return configured
        if normalized.startswith("ga"):
            ga_root = _config_path(parser, "router", "ga_root_dir", base_dir=config_base_dir)
            if ga_root:
                auto = _auto_router_subdir(ga_root, router_execution_family(normalized))
                if auto:
                    return auto
        if normalized.startswith("auto"):
            auto_root = _config_path(parser, "router", "auto_root_dir", base_dir=config_base_dir)
            if auto_root:
                auto = _auto_router_subdir(auto_root, router_execution_family(normalized))
                if auto:
                    return auto
        if normalized.startswith(("rl", "ga", "auto")):
            rl_root = _config_path(parser, "router", "rl_root_dir", base_dir=config_base_dir)
            if rl_root:
                auto = _auto_rl_subdir(rl_root, router_execution_family(normalized))
                if auto:
                    return auto

    if work_dir is not None:
        return work_dir
    return Path(".")


def _find_binary(router_dir: Path, stem: str) -> Path | None:
    aliases = {
        "layer_assign_cpp": ("layer_assign_cpp", "layer_assign", "d.out", "a.out"),
        "escape_order_cpp": ("escape_order_cpp", "escape_order", "e.out", "b.out"),
        "135_main": ("135_main", "f.out", "main"),
        "arc_main": ("arc_main", "c.out"),
    }
    candidates = list(aliases.get(stem, (stem,)))
    candidates.extend(f"{name}.exe" for name in list(candidates) if not name.endswith(".exe"))
    if stem.endswith("_cpp"):
        candidates.extend([stem.replace("_cpp", ""), f"{stem.replace('_cpp', '')}.exe"])
    for name in candidates:
        path = router_dir / name
        if path.is_file():
            return path
    return None


def bjut_router_available(router_type: str, work_dir: Path | None = None) -> bool:
    normalized = normalize_router_type(router_type)
    if normalized.startswith("auto"):
        return any(
            bjut_router_available(candidate, work_dir=work_dir)
            for candidate in _auto_router_candidates(normalized)
        )
    router_dir = resolve_router_dir(router_type, work_dir=work_dir)
    if not router_dir.is_dir():
        return False
    if _find_binary(router_dir, "layer_assign_cpp") is None:
        return False
    if _find_binary(router_dir, "escape_order_cpp") is None:
        return False
    family = router_execution_family(router_type)
    main_stem = "135_main" if family == "135" else "arc_main"
    binaries = [
        _find_binary(router_dir, "layer_assign_cpp"),
        _find_binary(router_dir, "escape_order_cpp"),
        _find_binary(router_dir, main_stem),
    ]
    return all(binary is not None and _elf_matches_current_machine(binary) for binary in binaries)


def _router_binary_args(binary_path: Path, *args: str) -> list[str]:
    header = binary_path.read_bytes()[:4]
    if header == b"\x7fELF":
        if not _elf_matches_current_machine(binary_path):
            raise RuntimeError(
                f"{binary_path.name} 是 {_elf_machine_name(binary_path)} ELF，"
                f"当前容器架构为 {platform.machine()}，无法直接运行。"
            )
        return [str(binary_path), *args]
    if header[:2] == b"MZ":
        if sys.platform == "win32":
            return [str(binary_path), *args]
        raise RuntimeError(
            f"{binary_path.name} 是 Windows 可执行文件，当前 {sys.platform} 环境无法直接运行；"
            "请配置 rl_*_linux 目录或在 Windows 上执行布线。"
        )
    return [sys.executable, str(binary_path), *args]


def _elf_machine_code(binary_path: Path) -> int | None:
    try:
        header = binary_path.read_bytes()[:20]
    except OSError:
        return None
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    return int.from_bytes(header[18:20], "little")


def _elf_machine_name(binary_path: Path) -> str:
    code = _elf_machine_code(binary_path)
    return {
        0x3E: "x86_64",
        0xB7: "aarch64",
        0x28: "arm",
        0x03: "x86",
    }.get(code, f"unknown({code})")


def _elf_matches_current_machine(binary_path: Path) -> bool:
    code = _elf_machine_code(binary_path)
    if code is None:
        return True
    current = platform.machine().lower()
    if code == 0x3E:
        return current in {"x86_64", "amd64"}
    if code == 0xB7:
        return current in {"aarch64", "arm64"}
    if code == 0x28:
        return current.startswith("arm") and current not in {"aarch64", "arm64"}
    return True


def _binary_tool_name(binary_path: Path, generic_name: str) -> str:
    return binary_path.name if binary_path.name.endswith(".out") else generic_name


def _native_order_filename(router_type: str) -> str:
    return "order_out.txt" if router_execution_family(router_type) == "135" else "order_input.txt"


def _search_router_baseline_type(router_type: str) -> str:
    normalized = normalize_router_type(router_type)
    if normalized in {"ga_arc", "rl_arc", "auto_arc"}:
        return "arc"
    if normalized in {"ga", "ga_135", "rl", "rl_135", "auto", "auto_135"}:
        return "135"
    return normalized


def _write_native_order_file(work_dir: Path, router_type: str, order_lines: list[dict[str, Any]]) -> Path:
    source = work_dir / "order_input.txt"
    target = work_dir / _native_order_filename(router_type)
    if router_execution_family(router_type) != "135":
        if source != target and source.is_file():
            shutil.copy2(source, target)
        return target

    entries = []
    for index, item in enumerate(order_lines, start=1):
        if not isinstance(item, dict):
            continue
        net = str(item.get("net") or "").strip()
        layer = str(item.get("layer") or "").strip()
        if not net or not layer:
            continue
        native_layer = "BOTTOM" if layer.upper() in {"BOTTOM", "BOT", "B"} else "TOP"
        try:
            order = int(item.get("order", index))
        except (TypeError, ValueError):
            order = index
        entries.append((net, native_layer, max(1, order)))

    ordered = sorted(entries, key=lambda item: (0 if item[1] == "TOP" else 1, item[2], item[0]))
    target.write_text(
        "\n".join(f"{net} {layer} {order}" for net, layer, order in ordered) + ("\n" if ordered else ""),
        encoding="utf-8",
    )
    return target


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


def write_order_input(
    work_dir: Path,
    order_lines: list[dict[str, Any]],
    component_refdes: str,
    constraints: Any | None = None,
) -> Path:
    _ = constraints
    text = format_order_input_text(order_lines, component_refdes)
    path = work_dir / "order_input.txt"
    path.write_text(text, encoding="utf-8")
    return path


def format_order_input_text(order_lines: list[dict[str, Any]], component_refdes: str) -> str:
    layers: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(order_lines, start=1):
        if not isinstance(item, dict):
            continue
        net = str(item.get("net") or "").strip()
        layer = str(item.get("layer") or "").strip()
        if not net or not layer:
            continue
        try:
            order = int(item.get("order", index))
        except (TypeError, ValueError):
            order = index
        layers.setdefault(layer, []).append({"net": net, "layer": layer, "order": max(1, order)})

    lines = [component_refdes.strip(), str(len(layers))]
    for layer_entries in layers.values():
        ordered_entries = sorted(layer_entries, key=lambda item: item["order"])
        lines.append(str(len(ordered_entries)))
        lines.extend(f"{item['net']} {item['layer']} {item['order']}" for item in ordered_entries)
    return "\n".join(lines)


def copy_arc_constrain(work_dir: Path, router_dir: Path) -> Path:
    source = router_dir / "constrain.txt"
    if not source.is_file():
        raise FileNotFoundError(f"arc 布线器缺少 constrain.txt: {source}")
    target = work_dir / "constrain.txt"
    shutil.copyfile(source, target)
    return target


def parse_order_input_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return {"selectedBGA": "", "orderLines": [], "constraints": {}}

    selected_bga = ""
    index = 0
    first_parts = lines[0].split()
    if len(first_parts) < 3:
        selected_bga = lines[0]
        index = 1
    grouped = _parse_layer_grouped_order_lines(lines, index)
    if grouped is not None:
        return {
            "selectedBGA": selected_bga,
            "orderLines": _normalize_order_lines_by_order(grouped),
            "constraints": {},
        }

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

    return {
        "selectedBGA": selected_bga,
        "orderLines": _normalize_order_lines_by_order(order_lines),
        "constraints": constraints,
    }


def _normalize_order_lines_by_order(order_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [dict(item) for item in order_lines]
    normalized.sort(key=lambda item: item.get("order", 0))
    for idx, item in enumerate(normalized, start=1):
        item["order"] = idx
    return normalized


def _parse_layer_grouped_order_lines(lines: list[str], index: int) -> list[dict[str, Any]] | None:
    if index >= len(lines):
        return None
    try:
        layer_count = int(lines[index])
    except ValueError:
        return None
    if layer_count < 0:
        return None

    cursor = index + 1
    order_lines: list[dict[str, Any]] = []
    for _ in range(layer_count):
        if cursor >= len(lines):
            return None
        try:
            block_count = int(lines[cursor])
        except ValueError:
            return None
        if block_count < 0:
            return None
        cursor += 1
        for _ in range(block_count):
            if cursor >= len(lines):
                return None
            parts = lines[cursor].split()
            cursor += 1
            if len(parts) < 3:
                return None
            net, layer, order_text = parts[0], parts[1], parts[2]
            try:
                order = int(order_text)
            except ValueError:
                return None
            order_lines.append({"net": net, "layer": layer, "order": order})

    return order_lines if cursor == len(lines) else None


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
    step = _binary_tool_name(binary, "layer_assign_cpp")
    if binary.name in {"d.out", "a.out"}:
        args = [layout_name, component_name]
    else:
        args = [layout_name, component_name, "--output", "layer_input.txt"]
        if router_execution_family(router_type) == "arc":
            args = ["-arc", *args]
    _require_success(
        _run_process(_router_binary_args(binary, *args), work_dir),
        step,
    )
    if router_execution_family(router_type) == "arc":
        layer_path = work_dir / "layer_input.txt"
        if not layer_path.is_file() or layer_path.stat().st_size <= 0:
            raise RuntimeError(f"{step} 未生成有效 layer_input.txt")


def _run_escape_order(work_dir: Path, router_dir: Path, layout_name: str) -> None:
    binary = _find_binary(router_dir, "escape_order_cpp")
    if binary is None:
        raise FileNotFoundError(f"缺少 escape_order_cpp (router_dir={router_dir})")
    step = _binary_tool_name(binary, "escape_order_cpp")
    if binary.name == "e.out":
        args = ["net_list.txt", layout_name]
        order_path = work_dir / "order_out.txt"
    else:
        args = ["layer_input.txt", layout_name]
        order_path = work_dir / "order_input.txt"
    _require_success(
        _run_process(_router_binary_args(binary, *args), work_dir),
        step,
    )
    if not order_path.is_file() or order_path.stat().st_size <= 0:
        raise RuntimeError(f"{step} 未生成有效 {order_path.name}")
    if order_path.name != "order_input.txt":
        shutil.copy2(order_path, work_dir / "order_input.txt")


def _run_router_main(work_dir: Path, router_dir: Path, router_type: str, layout_name: str) -> None:
    family = router_execution_family(router_type)
    main_stem = "135_main" if family == "135" else "arc_main"
    binary = _find_binary(router_dir, main_stem)
    if binary is None:
        raise FileNotFoundError(f"缺少 {main_stem} (router_dir={router_dir})")
    step = _binary_tool_name(binary, main_stem)
    if family == "135":
        order_name = _native_order_filename(router_type) if binary.name == "f.out" else "order_input.txt"
        if not (work_dir / order_name).is_file() and (work_dir / "order_input.txt").is_file():
            shutil.copy2(work_dir / "order_input.txt", work_dir / order_name)
        args = [order_name, layout_name] if binary.name == "f.out" else [layout_name, order_name]
    else:
        args = ["order_input.txt", layout_name, "constrain.txt"]
    _require_success(_run_process(_router_binary_args(binary, *args), work_dir), step)


def _is_rl_router(router_type: str) -> bool:
    return normalize_router_type(router_type) in {"rl", "rl_135", "rl_arc"}


def _is_ga_router(router_type: str) -> bool:
    return normalize_router_type(router_type) in {"ga", "ga_135", "ga_arc"}


def _is_search_router(router_type: str) -> bool:
    return _is_rl_router(router_type) or _is_ga_router(router_type)


def _rl_script_name(router_type: str) -> str:
    return "train_dqn_arc.py" if router_execution_family(router_type) == "arc" else "train_dqn_135.py"


def _ga_script_name(router_type: str) -> str:
    return "train_ga_arc.py" if router_execution_family(router_type) == "arc" else "train_ga_135.py"


def _rl_layout_name(router_type: str) -> str:
    return "1231_4_arc.txt" if router_execution_family(router_type) == "arc" else "402Pin_08BGA_8L_S_01141700.txt"


def _search_module_name(router_type: str) -> str:
    return "ga" if _is_ga_router(router_type) else "rl"


def _search_display_name(router_type: str) -> str:
    return "GA" if _is_ga_router(router_type) else "RL"


def _search_script_name(router_type: str) -> str:
    return _ga_script_name(router_type) if _is_ga_router(router_type) else _rl_script_name(router_type)


def _auto_router_candidates(router_type: str) -> list[str]:
    normalized = normalize_router_type(router_type)
    if normalized == "auto_arc":
        return ["ga_arc", "rl_arc", "arc"]
    if normalized == "auto_135":
        return ["ga_135", "rl_135", "135"]
    family = router_execution_family(normalized)
    if family == "arc":
        return ["ga_arc", "rl_arc", "arc"]
    return ["ga_135", "rl_135", "135"]


def _rl_python_executable() -> str:
    configured, config_base_dir = _router_config_value("rl_python")
    configured = os.getenv("PCB_RL_PYTHON", "").strip() or configured
    if configured:
        return str(_expand_path(configured, base_dir=config_base_dir))
    if config_base_dir:
        portable = config_base_dir / "python_runtime" / ("python.exe" if sys.platform == "win32" else "bin/python")
        if portable.is_file():
            return str(portable)
    return "python" if getattr(sys, "frozen", False) else sys.executable


def _search_python_executable(router_type: str) -> str:
    if _is_ga_router(router_type):
        configured, config_base_dir = _router_config_value("ga_python")
        configured = os.getenv("PCB_GA_PYTHON", "").strip() or configured
        if configured:
            return str(_expand_path(configured, base_dir=config_base_dir))
    return _rl_python_executable()


def _rl_eval_budget(router_type: str) -> int:
    configured, _config_base_dir = _router_config_value("rl_eval_budget")
    raw = os.getenv("PCB_RL_EVAL_BUDGET", "").strip() or configured
    if not raw:
        raw = "200" if router_execution_family(router_type) == "arc" else "500"
    try:
        return max(1, int(raw))
    except ValueError:
        return 200 if router_execution_family(router_type) == "arc" else 500


def _ga_eval_budget(router_type: str) -> int:
    configured, _config_base_dir = _router_config_value("ga_eval_budget")
    raw = os.getenv("PCB_GA_EVAL_BUDGET", "").strip() or configured
    if not raw:
        raw = "200" if router_execution_family(router_type) == "arc" else "500"
    try:
        return max(1, int(raw))
    except ValueError:
        return 200 if router_execution_family(router_type) == "arc" else 500


def _search_eval_budget(router_type: str) -> int:
    return _ga_eval_budget(router_type) if _is_ga_router(router_type) else _rl_eval_budget(router_type)


def _search_timeout_seconds(router_type: str) -> int:
    prefix = "GA" if _is_ga_router(router_type) else "RL"
    config_key = "ga_timeout_seconds" if _is_ga_router(router_type) else "rl_timeout_seconds"
    timeout_text = os.getenv(f"PCB_{prefix}_TIMEOUT_SECONDS", "").strip() or _router_config_value(config_key)[0] or "1800"
    try:
        return max(60, int(timeout_text))
    except ValueError:
        return 1800


def _prepare_search_project_inputs(work_dir: Path, router_dir: Path, router_type: str, layout_path: Path) -> None:
    shutil.copy2(layout_path, router_dir / _rl_layout_name(router_type))
    native_order = work_dir / _native_order_filename(router_type)
    if native_order.is_file():
        shutil.copy2(native_order, router_dir / native_order.name)
    if (work_dir / "order_input.txt").is_file():
        shutil.copy2(work_dir / "order_input.txt", router_dir / "order_input.txt")
    if router_execution_family(router_type) == "arc" and (work_dir / "constrain.txt").is_file():
        shutil.copy2(work_dir / "constrain.txt", router_dir / "constrain.txt")


def _prepare_rl_project_inputs(work_dir: Path, router_dir: Path, router_type: str, layout_path: Path) -> None:
    _prepare_search_project_inputs(work_dir, router_dir, router_type, layout_path)


def _latest_search_run_dir(output_root: Path) -> Path | None:
    if not output_root.is_dir():
        return None
    candidates = [
        path for path in output_root.iterdir()
        if path.is_dir() and (path / "best_layer_order.txt").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _latest_rl_run_dir(output_root: Path) -> Path | None:
    return _latest_search_run_dir(output_root)


def _run_search_fanout_search(work_dir: Path, router_dir: Path, router_type: str, layout_path: Path) -> Path:
    module = _search_module_name(router_type)
    search_dir = (router_dir / module).resolve()
    script = search_dir / _search_script_name(router_type)
    if not script.is_file():
        raise FileNotFoundError(f"缺少 {_search_display_name(router_type)} 层分配/逃逸顺序脚本: {script}")

    native_order = work_dir / _native_order_filename(router_type)
    if not native_order.is_file() and (work_dir / "order_input.txt").is_file():
        parsed = parse_order_input_file(work_dir / "order_input.txt")
        _write_native_order_file(work_dir, router_type, parsed.get("orderLines") or [])
    _prepare_search_project_inputs(work_dir, router_dir, router_type, layout_path)
    output_root = work_dir / f"{module}_search_runs"
    tag = f"hermes_{normalize_router_type(router_type)}"
    args = [
        _search_python_executable(router_type),
        str(script.resolve()),
        "--eval-budget",
        str(_search_eval_budget(router_type)),
        "--initial-order",
        str(work_dir / _native_order_filename(router_type)),
        "--output-root",
        str(output_root),
        "--tag",
        tag,
    ]
    if _is_rl_router(router_type):
        args[4:4] = [
            "--device",
            os.getenv("PCB_RL_DEVICE", "").strip() or _router_config_value("rl_device")[0] or "cpu",
        ]
    proc = _run_process(args, search_dir, timeout=_search_timeout_seconds(router_type))
    _require_success(proc, script.name)

    run_dir = _latest_search_run_dir(output_root)
    if run_dir is None:
        raise RuntimeError(f"{_search_display_name(router_type)} 脚本未生成 best_layer_order.txt: output_root={output_root}")

    best_order = run_dir / "best_layer_order.txt"
    native_order = work_dir / _native_order_filename(router_type)
    shutil.copy2(best_order, native_order)
    if native_order.name != "order_input.txt":
        shutil.copy2(best_order, work_dir / "order_input.txt")
    best_dir = run_dir / "best_full"
    if not best_dir.is_dir():
        best_dir = run_dir / "best_partial"
    for name in (
        "line.out",
        "ARC_output.txt",
        "1234_1_arc_output.txt",
        "output.txt",
        "statistical.out",
        "data.txt",
        "summary.json",
        "explanation.md",
        _native_order_filename(router_type),
    ):
        source = (run_dir / name) if (run_dir / name).is_file() else (best_dir / name)
        if source.is_file():
            shutil.copy2(source, work_dir / name)
    return best_order


def _run_rl_fanout_search(work_dir: Path, router_dir: Path, router_type: str, layout_path: Path) -> Path:
    return _run_search_fanout_search(work_dir, router_dir, router_type, layout_path)


def _run_ga_fanout_search(work_dir: Path, router_dir: Path, router_type: str, layout_path: Path) -> Path:
    return _run_search_fanout_search(work_dir, router_dir, router_type, layout_path)


def _read_report(work_dir: Path) -> str:
    for report_name in ("data.txt", "statistical.out", "statistical.txt", "report.txt", "route_report.txt"):
        report_path = work_dir / report_name
        if not report_path.is_file() or report_path.stat().st_size <= 0:
            continue
        for encoding in ("utf-8", "gbk", "gb18030"):
            try:
                text = report_path.read_text(encoding=encoding).strip()
                if text:
                    return _compact_statistical_report(text)
            except UnicodeDecodeError:
                continue
        text = report_path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return _compact_statistical_report(text)
    return "布线完成（无详细报告）"


def _read_search_explanation_report(work_dir: Path, router_dir: Path, router_type: str) -> str:
    normalized = normalize_router_type(router_type)
    if not _is_search_router(normalized):
        return ""

    module = _search_module_name(normalized)
    roots = [work_dir, router_dir / module]
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / "explanation.md"
        if direct.is_file() and direct.stat().st_size > 0:
            candidates.append(direct)
        search_root = root / "search_runs"
        if search_root.is_dir():
            candidates.extend(
                path for path in search_root.rglob("explanation.md")
                if path.is_file() and path.stat().st_size > 0
            )

    if not candidates:
        return ""

    explanation_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    try:
        text = _read_text_lossy(explanation_path).strip()
    except OSError:
        return ""
    return text


def _read_rl_explanation_report(work_dir: Path, router_dir: Path, router_type: str) -> str:
    return _read_search_explanation_report(work_dir, router_dir, router_type)


def _fallback_search_explanation_report(work_dir: Path, router_type: str) -> str:
    normalized = normalize_router_type(router_type)
    if not _is_search_router(normalized):
        return ""

    order_path = work_dir / "order_input.txt"
    try:
        parsed = parse_order_input_file(order_path)
    except Exception as exc:
        display = _search_display_name(normalized)
        return (
            f"本次未读取到 {display} explanation.md；"
            f"同时无法解析本次 order_input.txt 生成概要报告：{exc}"
        )

    order_lines = parsed.get("orderLines") or []
    layers = []
    for item in order_lines:
        layer = str(item.get("layer") or "").strip()
        if layer and layer not in layers:
            layers.append(layer)
    preview_items = []
    for item in order_lines[:8]:
        net = str(item.get("net") or "").strip()
        layer = str(item.get("layer") or "").strip()
        order = item.get("order")
        if net and layer:
            preview_items.append(f"{net}->{layer}#{order}")

    display = _search_display_name(normalized)
    return (
        f"本次未读取到 {display} explanation.md，以下为 Agent 基于本次层分配和逃逸顺序文件生成的概要。\n"
        f"- 搜索器类型：{normalized}\n"
        f"- orderLines 数量：{len(order_lines)}\n"
        f"- 涉及层：{'、'.join(layers) if layers else '未解析到层'}\n"
        f"- 顺序预览：{'；'.join(preview_items) if preview_items else '无'}"
    )


def _fallback_rl_explanation_report(work_dir: Path, router_type: str) -> str:
    return _fallback_search_explanation_report(work_dir, router_type)


def _search_explanation_report(work_dir: Path, router_dir: Path, router_type: str) -> str:
    report = _read_search_explanation_report(work_dir, router_dir, router_type)
    if report:
        return report
    return _fallback_search_explanation_report(work_dir, router_type)


def _rl_explanation_report(work_dir: Path, router_dir: Path, router_type: str) -> str:
    return _search_explanation_report(work_dir, router_dir, router_type)


def _combine_route_reports(base_report: str, explanation_report: str) -> str:
    base = str(base_report or "").strip()
    explanation = _compact_rl_explanation_report(explanation_report)
    if not explanation:
        return base
    section = f"层分配和逃逸顺序生成报告：\n{explanation}"
    return f"{base}\n\n{section}".strip() if base else section


def _compact_rl_explanation_report(text: str) -> str:
    raw_lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not raw_lines:
        return ""

    output: list[str] = []
    table_rows: list[list[str]] = []
    keep_next_bullet = False

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        for cells in table_rows:
            if len(cells) >= 4:
                output.append(f"{cells[0]}：{cells[1]} -> {cells[2]}，变化 {cells[3]}")
        table_rows = []

    for line in raw_lines:
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if not cells or cells[0] in {"指标", "---"} or set(cells[0]) <= {"-", ":"}:
                continue
            table_rows.append(cells)
            continue

        flush_table()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title and title not in {"指标对比", "层和顺序变化", "为什么这个结果更好", "未布通网络"}:
                output.append(title)
            continue

        if line.startswith("- "):
            bullet = line[2:].strip()
            if (
                bullet.startswith("层负载")
                or bullet.startswith("变化规模")
                or bullet.startswith("代表性变化")
                or bullet.startswith("最佳候选来源")
                or bullet.startswith("布通数量")
                or bullet.startswith("总线长")
                or bullet.startswith("过孔")
                or bullet.startswith("最佳方案仍有")
                or bullet.startswith("相比初始方案")
            ):
                output.append(bullet)
                keep_next_bullet = True
            elif keep_next_bullet and len(output) < 14:
                output.append(bullet)
            continue

        keep_next_bullet = False
        if line:
            output.append(line)

    flush_table()

    deduped: list[str] = []
    for item in output:
        item = re.sub(r"`([^`]+)`", r"\1", item).strip()
        if item and item not in deduped:
            deduped.append(item)
    return "\n".join(deduped[:16]).strip()


def _compact_statistical_report(text: str, *, pin_preview_limit: int = 30) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if "引脚名称:" not in lines:
        return str(text or "").strip()

    output: list[str] = []
    current_pin_label = ""
    pins: list[str] = []

    def flush_pins() -> None:
        nonlocal pins, current_pin_label
        if not current_pin_label:
            pins = []
            return
        preview = "、".join(pins[:pin_preview_limit])
        suffix = f" 等 {len(pins)} 个" if len(pins) > pin_preview_limit else f"（共 {len(pins)} 个）"
        output.append(f"{current_pin_label}: {preview}{suffix}" if preview else f"{current_pin_label}: 无")
        pins = []
        current_pin_label = ""

    for line in lines:
        if line == "引脚名称:":
            continue
        success_match = re.match(r"布线成功的引脚个数[:：]\s*(.+)", line)
        failure_match = re.match(r"布线失败的引脚个数[:：]\s*(.+)", line)
        if success_match or failure_match:
            flush_pins()
            output.append(line)
            current_pin_label = "成功引脚" if success_match else "失败引脚"
            continue
        if current_pin_label and re.fullmatch(r"[A-Za-z]+\d+(?:[._-]?[A-Za-z0-9]+)?", line):
            pins.append(line)
            continue
        flush_pins()
        output.append(line)

    flush_pins()
    return "\n".join(output).strip()


def _resolve_import_lines_path(work_dir: Path, router_type: str, constraints: Any | None = None) -> Path:
    family = router_execution_family(router_type)
    candidates = ("line.out",) if family == "135" else ("ARC_output.txt", "arc_output.txt")
    for name in candidates:
        path = work_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    raise FileNotFoundError(f"{router_type} 布线器未生成可导入 importLines 的原始记录文件")


def _read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


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


def _candidate_summary_metrics(work_dir: Path) -> tuple[float, float, float, float, float]:
    summary_path = work_dir / "summary.json"
    if not summary_path.is_file():
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    if not isinstance(summary, dict):
        return (0.0, 0.0, 0.0, 0.0, 0.0)

    primary_kind = str(summary.get("primary_best_kind") or "")
    candidates: list[dict[str, Any]] = []
    for key in ("best_full", "best_partial", "best_wire_full", "best_via_full"):
        value = summary.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            if key == primary_kind:
                candidates.insert(0, value)
    if not candidates:
        return (0.0, 0.0, 0.0, 0.0, 0.0)

    candidate = candidates[0]
    stats = candidate.get("stats") if isinstance(candidate.get("stats"), dict) else {}
    try:
        routed = float(stats.get("routed_nets") or 0)
        completion = float(stats.get("completion_rate") or 0)
        total_wire = float(stats.get("total_wire_length") or 0)
        vias = float(stats.get("vias") or 0)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0, 0.0)
    is_full = 1.0 if primary_kind == "best_full" or completion >= 0.999 else 0.0
    return (is_full, routed, completion, -total_wire, -vias)


def _fanout_candidate_score(fanout_params: dict[str, Any], work_dir: Path, router_type: str) -> tuple[float, ...]:
    metrics = _candidate_summary_metrics(work_dir)
    order_count = float(len(fanout_params.get("orderLines") or []))
    preference = {
        "ga": 0.30,
        "ga_135": 0.30,
        "ga_arc": 0.30,
        "rl": 0.20,
        "rl_135": 0.20,
        "rl_arc": 0.20,
        "135": 0.10,
        "arc": 0.10,
    }.get(normalize_router_type(router_type), 0.0)
    return (*metrics, order_count, preference)


def _copy_fanout_candidate_outputs(source_dir: Path, target_dir: Path) -> None:
    names = [
        "layout_input.txt",
        "版图信息.txt",
        "component_input.txt",
        "layer_input.txt",
        "order_input.txt",
        "order_out.txt",
        "constrain.txt",
        "line.in",
        "line.out",
        "ARC_output.txt",
        "1234_1_arc_output.txt",
        "output.txt",
        "routing_input.txt",
        "data.txt",
        "statistical.out",
        "statistical.txt",
        "report.txt",
        "route_report.txt",
        "summary.json",
        "explanation.md",
    ]
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = source_dir / name
        if source.is_file():
            shutil.copy2(source, target_dir / name)
    for pattern in ("*_search_runs",):
        for source in source_dir.glob(pattern):
            target = target_dir / source.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)


def _generate_single_fanout_params(
    *,
    project_data: str,
    selected_bga: str,
    router_type: str,
    work_dir: Path,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_router_type(router_type)
    if normalized not in SUPPORTED_ROUTER_TYPES or normalized.startswith("auto"):
        raise ValueError(f"不支持的 routerType: {router_type}")

    work_dir.mkdir(parents=True, exist_ok=True)
    router_dir = resolve_router_dir(normalized, work_dir=work_dir)
    if not bjut_router_available(normalized, work_dir=work_dir):
        raise RuntimeError(f"BJUT 布线器不可用: routerType={normalized}, dir={router_dir}")

    layout_path = write_layout_inputs(work_dir, project_data)
    write_component_input(work_dir, selected_bga)
    if router_execution_family(normalized) == "arc":
        copy_arc_constrain(work_dir, router_dir)

    _run_layer_assign(work_dir, router_dir, normalized, layout_path.name, "component_input.txt")
    _run_escape_order(work_dir, router_dir, layout_path.name)
    if _is_search_router(normalized):
        _run_search_fanout_search(work_dir, router_dir, normalized, layout_path)

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


def _generate_auto_fanout_params(
    *,
    project_data: str,
    selected_bga: str,
    router_type: str,
    work_dir: Path,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    best: tuple[tuple[float, ...], dict[str, Any], Path] | None = None
    work_dir.mkdir(parents=True, exist_ok=True)
    parent = work_dir.parent if work_dir.parent != Path("") else Path(".")
    with tempfile.TemporaryDirectory(prefix="fanout_auto_", dir=str(parent)) as temp_root_text:
        temp_root = Path(temp_root_text)
        for candidate_type in _auto_router_candidates(router_type):
            if not bjut_router_available(candidate_type, work_dir=work_dir):
                errors.append(f"{candidate_type}: 不可用")
                continue
            candidate_dir = temp_root / candidate_type
            try:
                candidate = _generate_single_fanout_params(
                    project_data=project_data,
                    selected_bga=selected_bga,
                    router_type=candidate_type,
                    work_dir=candidate_dir,
                    constraints=constraints,
                )
            except Exception as exc:
                errors.append(f"{candidate_type}: {exc}")
                logger.info("fanout auto candidate failed: %s", errors[-1])
                continue
            score = _fanout_candidate_score(candidate, candidate_dir, candidate_type)
            if best is None or score > best[0]:
                best = (score, candidate, candidate_dir)

        if best is None:
            raise RuntimeError("自动层分配/逃逸顺序搜索失败：" + "；".join(errors))
        _copy_fanout_candidate_outputs(best[2], work_dir)
        selected = dict(best[1])
        selected["autoRouterType"] = normalize_router_type(router_type)
        selected["routerType"] = normalize_router_type(selected.get("routerType") or "")
        if errors:
            selected["_autoRouterErrors"] = errors
        return selected


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
    if normalized.startswith("auto"):
        return _generate_auto_fanout_params(
            project_data=project_data,
            selected_bga=selected_bga,
            router_type=normalized,
            work_dir=work_dir,
            constraints=constraints,
        )
    return _generate_single_fanout_params(
        project_data=project_data,
        selected_bga=selected_bga,
        router_type=normalized,
        work_dir=work_dir,
        constraints=constraints,
    )


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
    if router_type.startswith("auto"):
        raise ValueError("routerType=auto 只能用于生成 fanoutParams；执行布线时必须使用自动选择后的实际 routerType")
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
    constraints = fanout_params.get("constraints") or {}
    if router_execution_family(router_type) == "arc":
        copy_arc_constrain(work_dir, router_dir)

    write_order_input(work_dir, order_lines, selected_bga, constraints)
    _write_native_order_file(work_dir, router_type, order_lines)
    _run_router_main(work_dir, router_dir, router_type, layout_path.name)

    routing_result = _resolve_routing_result_path(work_dir, router_type, router_dir, layout_path)
    import_lines = _resolve_import_lines_path(work_dir, router_type, constraints)
    report = _read_report(work_dir)
    explanation_report = _search_explanation_report(work_dir, router_dir, router_type)
    return RouterRunOutputs(
        routing_result_path=routing_result,
        import_lines_path=import_lines,
        report=_combine_route_reports(report or "布线完成（无详细报告）", explanation_report),
    )
