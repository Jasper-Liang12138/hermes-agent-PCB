"""PCB Intelligence Tools - BGA Fanout Routing

Registers PCB-specific tools for BGA fanout routing with Qiyunfang PCB client.

工具调用链路：
  Hermes Agent (executor thread)
       ↓ registry.dispatch() → tool handler (同步)
       ↓ run_coroutine_threadsafe → main event loop
  WebSocketAdapter.send_tool_call()
       ↓ WebSocket message (tool-calls)
  Qiyunfang PCB Client
       ↓ WebSocket message (tool-results)
  WebSocketAdapter._handle_tool_results() → Future.set_result()
       ↑ tool handler 等待结果返回
"""
import json
import asyncio
import subprocess
import os
import sys
import shutil
import runpy
import io
import contextlib
import threading
import uuid
import logging
import re
import tempfile
import importlib.util
import math
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import Future as ThreadFuture
from types import SimpleNamespace
from typing import Dict, Any, Optional
from pathlib import Path

from tools import pcb_model_runtime
from tools.pcb_drc_agent_report import generate_drc_agent_report
from tools.pcb_explain_report import generate_explain_report
from tools.registry import registry

logger = logging.getLogger(__name__)

_ROUTE_MODE_CHAT = "chat"
_ROUTE_MODE_PCB = "pcb"
_PY_SCRIPT_LOCK = threading.RLock()

def _normalize_router_type(value: Any) -> str:
    from tools.pcb_bjut_router import normalize_router_type

    return normalize_router_type(value)


def _router_type_from_payload(user_data_obj: Any, route_params: Any) -> str:
    keys = ("routerType", "router_type", "routerProfile", "router_profile", "router")
    for payload in (user_data_obj, route_params):
        if not isinstance(payload, dict):
            continue
        for key in keys:
            router_type = _normalize_router_type(payload.get(key))
            if router_type:
                return router_type
    return _normalize_router_type(os.getenv("ROUTER_TYPE") or os.getenv("PCB_ROUTER_TYPE"))


def _router_profile_dir(router_type: str, work_dir: Path) -> Path:
    from tools.pcb_bjut_router import resolve_router_dir

    return resolve_router_dir(router_type, work_dir=work_dir)


def _copy_runtime_file(src_dir: Path, work_dir: Path, name: str) -> Path:
    src = src_dir / name
    target_name = name
    if not src.exists() and name.endswith(".out"):
        exe_name = f"{name[:-4]}.exe"
        exe_src = src_dir / exe_name
        if exe_src.exists():
            src = exe_src
            target_name = exe_name
    target = work_dir / target_name
    if src.exists() and src.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
    if not target.exists():
        raise FileNotFoundError(f"布线器运行文件缺失: {name} (router_dir={src_dir})")
    return target


def _copy_runtime_support_files(src_dir: Path, work_dir: Path) -> None:
    """Copy helper scripts/configs required by packaged router executables."""
    if not src_dir.exists():
        return
    allowed_suffixes = {".py", ".csv", ".exe"}
    allowed_names = {"parameter.txt"}
    for src in src_dir.iterdir():
        if not src.is_file():
            continue
        if src.suffix.lower() not in allowed_suffixes and src.name not in allowed_names:
            continue
        target = work_dir / src.name
        if src.resolve() != target.resolve():
            target.write_bytes(src.read_bytes())


def _run_process(args: list[str], work_dir: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    logger.info("Executing router step: %s in %s", args, work_dir)
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


def _constraint_values(constraints: Any) -> tuple[Any, Any]:
    if not isinstance(constraints, dict):
        return None, None
    return constraints.get("LineWidth"), constraints.get("LineSpacing")


def _write_arc_constraint(work_dir: Path, constraints: Any) -> Path:
    line_width, line_spacing = _constraint_values(constraints)
    if line_width is None:
        line_width = 3
    if line_spacing is None:
        line_spacing = 4.5
    path = work_dir / "constrain.txt"
    path.write_text(f"LineWidth:{line_width}\nLineSpacing:{line_spacing}\n", encoding="utf-8")
    return path


def _copy_arc_constraint(work_dir: Path, router_dir: Path) -> Path:
    source = router_dir / "constrain.txt"
    if not source.is_file():
        raise FileNotFoundError(f"arc 布线器缺少 constrain.txt: {source}")
    path = work_dir / "constrain.txt"
    shutil.copyfile(source, path)
    return path


def _write_order_input(work_dir: Path, order_lines: list[dict[str, Any]], component_refdes: str) -> Path:
    from tools.pcb_bjut_router import format_order_input_text

    order_text = format_order_input_text(order_lines, component_refdes)
    path = work_dir / "order_input.txt"
    path.write_text(order_text, encoding="utf-8")
    return path


def _write_arc_layer_input(work_dir: Path, order_lines: list[dict[str, Any]], component_refdes: str) -> Path:
    layer_text = "\n".join(
        f"{item['net']} {item['layer']}"
        for item in order_lines
    )
    layer_text = f"{layer_text}\n\n{component_refdes}"
    path = work_dir / "layer_input.txt"
    path.write_text(layer_text, encoding="utf-8")
    return path


def _write_component_input(work_dir: Path, component_refdes: str) -> Path:
    path = work_dir / "component_input.txt"
    path.write_text(f"{component_refdes}\n", encoding="utf-8")
    return path


def _run_python_script_inprocess(script_path: Path, work_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run router conversion scripts without spawning agent.exe in PyInstaller builds."""
    script_path = script_path.resolve()
    work_dir = work_dir.resolve()
    logger.info("Executing router Python step in-process: %s %s in %s", script_path, args, work_dir)
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_argv = sys.argv[:]
    old_cwd = os.getcwd()
    returncode = 0

    with _PY_SCRIPT_LOCK:
        try:
            sys.argv = [str(script_path), *args]
            os.chdir(work_dir)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                runpy.run_path(str(script_path), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                returncode = 0
            elif isinstance(code, int):
                returncode = code
            else:
                returncode = 1
                stderr.write(str(code))
        except Exception as exc:
            returncode = 1
            stderr.write(f"{type(exc).__name__}: {exc}")
        finally:
            os.chdir(old_cwd)
            sys.argv = old_argv

    return subprocess.CompletedProcess(
        args=[str(script_path), *args],
        returncode=returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def _router_binary_args(binary_path: Path, *args: str) -> list[str]:
    """Run real router binaries directly; allow text scripts in tests."""
    try:
        header = binary_path.read_bytes()[:4]
        if header == b"\x7fELF":
            if os.name == "nt":
                raise RuntimeError(
                    f"{binary_path.name} 是 Linux ELF 布线器，当前纯 Windows 环境不能直接执行；"
                    "请提供 Windows 原生 exe 版本，或在非 Windows 环境运行。"
                )
            return [str(binary_path), *args]
        if header[:2] == b"MZ":
            return [str(binary_path), *args]
        return [sys.executable, str(binary_path), *args]
    except Exception:
        if sys.exc_info()[0] is RuntimeError:
            raise
        pass
    return [str(binary_path), *args]


def _read_router_result(work_dir: Path) -> str:
    result_file = work_dir / "routing_input.txt"
    if not result_file.exists():
        raise FileNotFoundError("布线器未生成 routing_input.txt")
    return _read_text_lossy(result_file)


def _router_result_path(work_dir: Path) -> Path:
    result_file = work_dir / "routing_input.txt"
    if not result_file.exists():
        raise FileNotFoundError("布线器未生成 routing_input.txt")
    return result_file.resolve()


def _router_import_lines_path(work_dir: Path, router_type: str, constraints: Any = None) -> Path:
    """Return the router-native records file expected by EDA importLines."""
    from tools.pcb_bjut_router import router_execution_family

    family = router_execution_family(router_type)
    candidates = ("line.out",) if family == "135" else ("ARC_output.txt", "arc_output.txt")
    for filename in candidates:
        path = work_dir / filename
        if path.exists() and path.stat().st_size > 0:
            return path.resolve()
    raise FileNotFoundError(f"{router_type} 布线器未生成可导入 importLines 的原始记录文件")


def _read_router_report(work_dir: Path, fallback: str = "布线完成（无详细报告）") -> str:
    report_file = work_dir / "data.txt"
    if report_file.exists() and report_file.stat().st_size > 0:
        return _compact_statistical_report(_read_text_lossy(report_file).strip()) or fallback

    for filename in ("statistical.out", "statistical.txt", "report.txt", "route_report.txt"):
        candidate = work_dir / filename
        if candidate.exists() and candidate.stat().st_size > 0:
            text = _read_text_lossy(candidate).strip()
            if text:
                return _compact_statistical_report(text)

    output_summaries: list[str] = []
    for filename in ("line.out", "ARC_output.txt", "arc_output.txt", "routing_input.txt"):
        candidate = work_dir / filename
        if not candidate.exists() or candidate.stat().st_size <= 0:
            continue
        try:
            line_count = sum(1 for _ in candidate.open("rb"))
        except OSError:
            line_count = 0
        output_summaries.append(f"{filename}: {candidate.stat().st_size} bytes, {line_count} lines")
    if output_summaries:
        return "布线器未输出详细报告，已生成布线结果文件：\n" + "\n".join(output_summaries)
    return fallback


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


def _read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_ROUTER_STALE_FILES = (
    "layout_input.txt",
    "routing_input.txt",
    "ARC_output.txt",
    "data.txt",
    "net_list.txt",
    "order_out.txt",
    "line.in",
    "line.out",
    "layer_input.txt",
)


def _remove_file_if_exists(path: Path) -> None:
    try:
        if path.exists() and path.is_file():
            path.unlink()
    except OSError as exc:
        raise RuntimeError(f"无法清理旧布线中间文件 {path.name}: {exc}") from exc


def _cleanup_router_work_dir(work_dir: Path, component_refdes: str) -> None:
    for name in _ROUTER_STALE_FILES:
        _remove_file_if_exists(work_dir / name)
    if component_refdes:
        _remove_file_if_exists(work_dir / f"{component_refdes}_pins.csv")


def _write_current_layout_inputs(work_dir: Path, project_data: str) -> None:
    for name in ("版图信息.txt", "layout_input.txt"):
        (work_dir / name).write_text(project_data, encoding="utf-8")


def _ensure_nonempty_file(work_dir: Path, name: str, step: str) -> Path:
    path = work_dir / name
    if not path.exists():
        raise FileNotFoundError(f"{step} 未生成 {name}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"{step} 生成的 {name} 为空")
    return path


def _count_pin_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    for raw in _read_text_lossy(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("units"):
            continue
        first = line.split(",", 1)[0].strip()
        if not first or first.lower() == "pinnumber":
            continue
        count += 1
    return count


def _expected_component_pin_count(project_data: str, component_refdes: str) -> Optional[int]:
    if not project_data or not component_refdes:
        return None
    pattern = re.compile(rf'\(component\s+"{re.escape(component_refdes)}"(?=\s|\))', re.IGNORECASE)
    match = pattern.search(project_data)
    if not match:
        return None

    depth = 0
    started = False
    in_string = False
    end = len(project_data)
    for index in range(match.start(), len(project_data)):
        char = project_data[index]
        if char == '"' and (index == 0 or project_data[index - 1] != "\\"):
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "(":
            depth += 1
            started = True
        elif char == ")":
            depth -= 1
            if started and depth == 0:
                end = index + 1
                break
    block = project_data[match.start():end]
    count = len(re.findall(r"\(pin\b", block, flags=re.IGNORECASE))
    return count or None


def _validate_component_pins(
    work_dir: Path,
    component_refdes: str,
    project_data: str,
    step: str,
) -> None:
    pin_file = _ensure_nonempty_file(work_dir, f"{component_refdes}_pins.csv", step)
    actual = _count_pin_csv_rows(pin_file)
    expected = _expected_component_pin_count(project_data, component_refdes)
    if actual <= 0:
        raise RuntimeError(f"{step} 未提取到 {component_refdes} 的有效引脚")
    if expected and actual < max(1, int(expected * 0.8)):
        raise RuntimeError(
            f"{step} 提取到的 {component_refdes} 引脚数异常：{actual}/{expected}。"
            "请检查 adapter 是否仍在读取旧 layout_input.txt 或旧 pins.csv。"
        )


def _validate_net_list(work_dir: Path, component_refdes: str, step: str) -> None:
    path = _ensure_nonempty_file(work_dir, "net_list.txt", step)
    content = _read_text_lossy(path)
    if f"{component_refdes}." not in content:
        raise RuntimeError(f"{step} 生成的 net_list.txt 不包含目标器件 {component_refdes} 的网络")


def _arc_diff_pair_key(net_name: str) -> Optional[tuple[str, str]]:
    if not isinstance(net_name, str):
        return None
    match = re.match(r"^(.*)_(P|N)_(.+)$", net_name.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    return (f"{match.group(1)}__{match.group(3)}".casefold(), match.group(2).upper())


def _validate_arc_order_lines(order_lines: list[dict[str, Any]]) -> None:
    pair_sides: dict[str, set[str]] = {}
    for item in order_lines:
        pair = _arc_diff_pair_key(str(item.get("net", "")).strip())
        if not pair:
            continue
        pair_key, side = pair
        pair_sides.setdefault(pair_key, set()).add(side)

    if not any({"P", "N"}.issubset(sides) for sides in pair_sides.values()):
        raise RuntimeError(
            "arc 布线器仅支持差分对网络；当前 orderLines 不包含成对的 *_P_* / *_N_* 网络。"
        )


def _line_width_or_default(constraints: Any) -> float:
    line_width, _ = _constraint_values(constraints)
    try:
        width = float(line_width)
        if width > 0:
            return width
    except (TypeError, ValueError):
        pass
    return 4.0


def _normalize_135_netlist_widths(work_dir: Path, constraints: Any) -> None:
    path = _ensure_nonempty_file(work_dir, "net_list.txt", "135 e.out")
    fallback_width = _line_width_or_default(constraints)
    changed = False
    normalized_lines: list[str] = []

    for raw_line in _read_text_lossy(path).splitlines():
        parts = raw_line.split(";")
        if len(parts) >= 3:
            width_text = parts[2].strip()
            try:
                width = float(width_text)
            except ValueError:
                parts[2] = f" {fallback_width:g} "
                raw_line = ";".join(parts)
                changed = True
            else:
                if width <= 0:
                    parts[2] = f" {fallback_width:g} "
                    raw_line = ";".join(parts)
                    changed = True
        normalized_lines.append(raw_line)

    if changed:
        path.write_text("\n".join(normalized_lines) + "\n", encoding="utf-8")
        logger.info("Normalized 135 net_list.txt unspecified widths to %g mil", fallback_width)


def _scale_mils_to_int(value: str) -> int:
    try:
        scaled = Decimal(str(value).strip()) * Decimal("100")
        return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0


def _normalize_conductor_layer(layer: str) -> str:
    layer = (layer or "").strip()
    if not layer:
        return "Conductor/Unknown"
    return f"Conductor/{layer.lower().capitalize()}"


def _build_wire_blocks_from_line_output(line_output_path: Path) -> tuple[str, int]:
    lines: list[str] = []
    wire_count = 0

    for raw in _read_text_lossy(line_output_path).splitlines():
        parts = [part.strip() for part in raw.strip().split("!")]
        if len(parts) != 9 or parts[1].upper() != "LINE":
            continue
        raw_layer, _, _, net, x1, y1, x2, y2, width = parts
        layer = _normalize_conductor_layer(raw_layer)
        w = _scale_mils_to_int(width)

        lines.append("        (wire")
        lines.append(f'            (net "{net}")')
        lines.append("            (path")
        lines.append('                (issamewidth "true")')
        lines.append("                (lineseg")
        lines.append(f"                    (pt {_scale_mils_to_int(x1)} {_scale_mils_to_int(y1)})")
        lines.append(f"                    (w {w})")
        lines.append("                )")
        lines.append("                (lineseg")
        lines.append(f"                    (pt {_scale_mils_to_int(x2)} {_scale_mils_to_int(y2)})")
        lines.append(f"                    (w {w})")
        lines.append("                )")
        lines.append("                (props)")
        lines.append(f'                (layer "{layer}")')
        lines.append("            )")
        lines.append("        )")
        wire_count += 1

    if not lines:
        return "", 0
    return "\n".join(["    (wires", *lines, "    )"]) + "\n", wire_count


def _find_matching_paren(text: str, open_pos: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(open_pos, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _replace_or_insert_group(content: str, group_name: str, group_text: str, before_group: str) -> str:
    group_match = re.search(rf"(?m)^[ \t]*\({re.escape(group_name)}\b", content)
    if group_match:
        group_end = _find_matching_paren(content, group_match.start())
        if group_end != -1:
            return content[:group_match.start()] + group_text + content[group_end + 1:]

    before_match = re.search(rf"(?m)^[ \t]*\({re.escape(before_group)}\b", content)
    if before_match:
        return content[:before_match.start()] + group_text + content[before_match.start():]

    layout_end = content.rfind(")")
    if layout_end == -1:
        return content + "\n" + group_text
    return content[:layout_end] + group_text + content[layout_end:]


def _repair_135_routing_wires(work_dir: Path) -> None:
    line_output_path = _ensure_nonempty_file(work_dir, "line.out", "135 f.out")
    routing_path = _ensure_nonempty_file(work_dir, "routing_input.txt", "135 Turn_135_QYF.py")
    wire_blocks, wire_count = _build_wire_blocks_from_line_output(line_output_path)
    if wire_count <= 0:
        raise RuntimeError("135 line.out 未包含可转换的 LINE 走线段")

    content = _read_text_lossy(routing_path)
    before_count = len(re.findall(r"(?m)^[ \t]*\(wire\b", content))
    content = _replace_or_insert_group(content, "wires", wire_blocks, "vias")
    routing_path.write_text(content, encoding="utf-8")
    logger.info("Repaired 135 routing_input.txt wires: before=%d after=%d", before_count, wire_count)


def _router_error_report(exc: Exception, router_type: str, component_refdes: str) -> str:
    detail = str(exc)
    if "layout_input.txt" in detail or "pins.csv" in detail:
        return (
            f"{router_type} 布线器输入准备失败：{detail}。"
            "当前 adapter 已强制刷新本次版图与清理旧中间文件；若仍失败，请检查布线器脚本对目标器件命名的匹配规则。"
        )
    if "差分对网络" in detail or "*_P_* / *_N_*" in detail:
        return (
            "arc 布线器当前只支持差分对网络输入。"
            f"{detail} 请改用包含成对 *_P_* / *_N_* 网络的扇出参数，或切换到 135 布线器。"
        )
    if "net_list.txt" in detail:
        if router_type == "arc":
            return (
                f"arc 布线器网络提取失败：{detail}。"
                f"当前 Windows 版 arc 会从版图中提取 {component_refdes} 的差分对网络；"
                "如果版图本身没有可识别的 *_P_* / *_N_* 网络，ARC_output.txt 会为空。"
            )
        return f"{router_type} 布线器网络提取失败：{detail}。请检查 adapter 是否能从本次版图中提取 {component_refdes} 的网络。"
    if "order_out.txt" in detail or "line.in" in detail or "line.out" in detail:
        return (
            f"{router_type} 布线器预处理失败：{detail}。"
            "这通常表示 135 adapter 的 d/e/f 中间链路没有生成有效逃逸顺序或折线输入。"
        )
    if "routing_input.txt" in detail:
        return (
            f"{router_type} 布线器结果转换失败：{detail}。"
            "预处理可能已执行，但最终转换脚本没有产出可回写的 routing_input.txt。"
        )
    return f"{router_type} 布线器异常：{detail}"


def _run_arc_router(
    work_dir: Path,
    router_dir: Path,
    component_refdes: str,
    constraints: Any,
    order_lines: list[dict[str, Any]],
    project_data: str,
) -> None:
    _copy_runtime_support_files(router_dir, work_dir)
    layout_path = work_dir / "layout_input.txt"
    if not layout_path.exists():
        raise FileNotFoundError("缺少版图输入文件 layout_input.txt")
    source = work_dir / "版图信息.txt"
    if not source.exists():
        raise FileNotFoundError("缺少版图输入文件 版图信息.txt")
    _validate_arc_order_lines(order_lines)
    _remove_file_if_exists(work_dir / f"{component_refdes}_pins.csv")
    _write_component_input(work_dir, component_refdes)
    _ = constraints
    constrain_path = _copy_arc_constraint(work_dir, router_dir)
    _write_arc_layer_input(work_dir, order_lines, component_refdes)

    c_out = _copy_runtime_file(router_dir, work_dir, "c.out")
    turn_script = _copy_runtime_file(router_dir, work_dir, "Turn_QYF.py")

    pins_helper = work_dir / "get_pins.py"
    if pins_helper.exists():
        _require_success(
            _run_python_script_inprocess(pins_helper, work_dir, layout_path.name, component_refdes),
            "arc get_pins.py",
        )
        _validate_component_pins(work_dir, component_refdes, project_data, "arc get_pins.py")

    _require_success(
        _run_process(
            _router_binary_args(c_out, "order_input.txt", layout_path.name, constrain_path.name, "component_input.txt"),
            work_dir,
        ),
        "arc c.out",
    )
    _validate_component_pins(work_dir, component_refdes, project_data, "arc c.out")
    _validate_net_list(work_dir, component_refdes, "arc c.out")
    _ensure_nonempty_file(work_dir, "ARC_output.txt", "arc c.out")
    _require_success(
        _run_python_script_inprocess(turn_script, work_dir, layout_path.name, "ARC_output.txt", "routing_input.txt"),
        "arc Turn_QYF.py",
    )
    _ensure_nonempty_file(work_dir, "routing_input.txt", "arc Turn_QYF.py")


def _run_135_router(work_dir: Path, router_dir: Path, component_refdes: str, constraints: Any) -> None:
    _copy_runtime_support_files(router_dir, work_dir)
    layout_path = work_dir / "layout_input.txt"
    if not layout_path.exists():
        raise FileNotFoundError("缺少版图输入文件 layout_input.txt")
    source = work_dir / "版图信息.txt"
    if not source.exists():
        raise FileNotFoundError("缺少版图输入文件 版图信息.txt")
    project_data = source.read_text(encoding="utf-8")
    _remove_file_if_exists(work_dir / f"{component_refdes}_pins.csv")
    _write_component_input(work_dir, component_refdes)

    d_out = _copy_runtime_file(router_dir, work_dir, "d.out")
    e_out = _copy_runtime_file(router_dir, work_dir, "e.out")
    f_out = _copy_runtime_file(router_dir, work_dir, "f.out")
    turn_script = _copy_runtime_file(router_dir, work_dir, "Turn_135_QYF.py")

    _require_success(_run_process(_router_binary_args(d_out, layout_path.name, "component_input.txt"), work_dir), "135 d.out")
    pins_helper = work_dir / "get_135_pins.py"
    if not (work_dir / f"{component_refdes}_pins.csv").exists() and pins_helper.exists():
        _require_success(
            _run_python_script_inprocess(pins_helper, work_dir, layout_path.name, component_refdes),
            "135 get_135_pins.py",
        )
    _validate_component_pins(work_dir, component_refdes, project_data, "135 d.out")
    _require_success(_run_process(_router_binary_args(e_out, "net_list.txt", layout_path.name), work_dir), "135 e.out")
    _validate_net_list(work_dir, component_refdes, "135 e.out")
    _normalize_135_netlist_widths(work_dir, constraints)
    _require_success(_run_process(_router_binary_args(f_out, "order_out.txt", layout_path.name), work_dir), "135 f.out")
    _ensure_nonempty_file(work_dir, "order_out.txt", "135 f.out")
    _ensure_nonempty_file(work_dir, "line.in", "135 f.out")
    _ensure_nonempty_file(work_dir, "line.out", "135 f.out")
    _require_success(
        _run_python_script_inprocess(turn_script, work_dir, layout_path.name, "line.out", "routing_input.txt"),
        "135 Turn_135_QYF.py",
    )
    _ensure_nonempty_file(work_dir, "routing_input.txt", "135 Turn_135_QYF.py")
    _repair_135_routing_wires(work_dir)


def _clean_component_refdes(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("label") or value.get("name") or value.get("refdes")
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("`'\"").strip("，。,.!?！？:：;；")
    if not cleaned:
        return None
    match = re.search(r"[A-Za-z_][A-Za-z0-9_.-]*", cleaned)
    return match.group(0) if match else None


def _component_from_payload(*payloads: Any) -> Optional[str]:
    keys = (
        "selectedBGA",
        "selected_bga",
        "selectedBga",
        "targetRefdes",
        "target_refdes",
        "component",
        "refdes",
        "label",
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            refdes = _clean_component_refdes(payload.get(key))
            if refdes:
                return refdes
    return None


def _component_from_session(session_id: Optional[str]) -> Optional[str]:
    try:
        adapter = _transport.get_adapter()
        selected_targets = getattr(adapter, "_session_selected_targets", {}) or {}
        return _clean_component_refdes(selected_targets.get(session_id))
    except Exception:
        return None


def _infer_component_from_project_data(project_data: str) -> Optional[str]:
    if not isinstance(project_data, str) or not project_data.strip():
        return None

    quoted_matches = list(re.finditer(r'\(component\s+"([^"]+)"', project_data))
    for index, match in enumerate(quoted_matches):
        label = match.group(1).strip()
        next_start = quoted_matches[index + 1].start() if index + 1 < len(quoted_matches) else len(project_data)
        block = project_data[match.start():next_start]
        if "bga" in block.lower():
            return label
    if quoted_matches:
        return quoted_matches[0].group(1).strip()

    named_matches = list(re.finditer(r'\(component\s+\(name\s+"([^"]+)"\)', project_data))
    for index, match in enumerate(named_matches):
        label = match.group(1).strip()
        next_start = named_matches[index + 1].start() if index + 1 < len(named_matches) else len(project_data)
        block = project_data[match.start():next_start]
        if "bga" in block.lower():
            return label
    if named_matches:
        return named_matches[0].group(1).strip()

    return None


def _resolve_component_refdes(user_data_obj: Any, route_params: Any, session_id: Optional[str], project_data: str) -> str:
    return (
        _component_from_payload(user_data_obj, route_params)
        or _component_from_session(session_id)
        or _infer_component_from_project_data(project_data)
        or "U1"
    )


# ============================================================================
# WebSocket Transport Singleton
# 保存 adapter 引用、主 event loop 引用、当前活跃 session_id
# ============================================================================

class WebSocketTransportSingleton:
    """
    全局单例，连接 PCB 工具与 WebSocket 适配器。

    - _websocket_adapter: WebSocketAdapter 实例，由 connect() 时注入
    - _main_loop: 主 asyncio event loop，用于 run_coroutine_threadsafe
    - current_session_id: 当前活跃的 WebSocket session（最近一次收到消息的 session）
    """

    _instance = None
    _websocket_adapter = None
    _main_loop: Optional[asyncio.AbstractEventLoop] = None
    current_session_id: Optional[str] = None
    _cached_project_data: Dict[str, str] = {}  # session_id -> getProjectData 结果缓存
    _cached_project_data_paths: Dict[str, str] = {}  # session_id -> getProjectData 文件路径缓存
    _cached_reroute_context: Dict[str, Dict[str, Any]] = {}  # session_id -> drop_net 结果缓存
    _session_modes: Dict[str, str] = {}  # session_id -> chat/pcb
    _pending_pcb_fields: Dict[str, Dict[str, Any]] = {}  # session_id -> fields emitted by tools

    @classmethod
    def get_instance(cls) -> "WebSocketTransportSingleton":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_adapter(self, adapter, loop: asyncio.AbstractEventLoop):
        """由 WebSocketAdapter.connect() 调用，注入 adapter 和主 loop。"""
        self._websocket_adapter = adapter
        self._main_loop = loop
        logger.info("PCB transport: adapter and main loop registered")

    def get_adapter(self):
        return self._websocket_adapter

    def get_main_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._main_loop

    def resolve_session_id(self, session_id: Optional[str] = None) -> Optional[str]:
        candidate = str(session_id or "").strip()
        if candidate:
            if (
                candidate in self._session_modes
                or candidate in self._cached_project_data
                or candidate in self._cached_project_data_paths
                or candidate in self._cached_reroute_context
            ):
                return candidate
            try:
                connections = getattr(self._websocket_adapter, "_connections", {}) or {}
                if candidate in connections:
                    return candidate
            except Exception:
                pass
        if self.current_session_id:
            return self.current_session_id
        return candidate or None

    def set_session_mode(self, session_id: str, mode: str) -> None:
        if not session_id:
            return
        normalized = _ROUTE_MODE_PCB if mode == _ROUTE_MODE_PCB else _ROUTE_MODE_CHAT
        self._session_modes[session_id] = normalized

    def get_session_mode(self, session_id: Optional[str]) -> str:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return _ROUTE_MODE_CHAT
        return self._session_modes.get(session_id, _ROUTE_MODE_CHAT)

    def is_pcb_mode(self, session_id: Optional[str]) -> bool:
        return self.get_session_mode(session_id) == _ROUTE_MODE_PCB

    def clear_session(self, session_id: str) -> None:
        self._session_modes.pop(session_id, None)
        self._cached_project_data.pop(session_id, None)
        self._cached_project_data_paths.pop(session_id, None)
        self._cached_reroute_context.pop(session_id, None)
        self._pending_pcb_fields.pop(session_id, None)
        if self.current_session_id == session_id:
            self.current_session_id = None

    def cache_project_data(self, data: str, session_id: Optional[str] = None) -> None:
        """保存 getProjectData 返回的版图数据，供 route 工具直接使用。"""
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        self._cached_project_data[session_id] = data

    def get_cached_project_data(self, session_id: Optional[str] = None) -> Optional[str]:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return None
        return self._cached_project_data.get(session_id)

    def cache_project_data_path(self, path: str, session_id: Optional[str] = None) -> None:
        """保存 getProjectData 返回的版图文件路径，供 pcb_extract_bga 脚本直接读取。"""
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        normalized = str(path or "").strip()
        if normalized:
            self._cached_project_data_paths[session_id] = normalized

    def get_cached_project_data_path(self, session_id: Optional[str] = None) -> Optional[str]:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return None
        return self._cached_project_data_paths.get(session_id)

    def cache_reroute_context(self, data: Dict[str, Any], session_id: Optional[str] = None) -> None:
        """保存 drop_net 的拆线上下文，供 reroute 工具使用。"""
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        self._cached_reroute_context[session_id] = data

    def clear_reroute_context(self, session_id: Optional[str] = None) -> None:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        self._cached_reroute_context.pop(session_id, None)

    def get_cached_reroute_context(self, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return None
        return self._cached_reroute_context.get(session_id)

    def set_pending_pcb_fields(self, fields: Dict[str, Any], session_id: Optional[str] = None) -> None:
        session_id = self.resolve_session_id(session_id)
        if not session_id or not isinstance(fields, dict) or not fields:
            return
        pending = self._pending_pcb_fields.setdefault(session_id, {})
        pending.update(fields)

    def pop_pending_pcb_fields(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return {}
        return self._pending_pcb_fields.pop(session_id, {})

    def call_tool_sync(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: float = 30.0,
        session_id: Optional[str] = None,
    ) -> Any:
        """
        在 executor 线程中同步调用 WebSocket 工具，等待结果。

        使用 run_coroutine_threadsafe 将协程调度到主 event loop，
        阻塞当前 executor 线程直到结果返回。
        """
        adapter = self._websocket_adapter
        if not adapter:
            raise RuntimeError("WebSocket adapter not initialized. Is the websocket gateway running?")

        loop = self._main_loop
        if not loop or not loop.is_running():
            raise RuntimeError("Main event loop not available")

        session_id = self.resolve_session_id(session_id)
        if not session_id:
            raise RuntimeError("No active WebSocket session. Is the PCB client connected?")
        if not self.is_pcb_mode(session_id):
            raise RuntimeError(
                f"Tool '{tool_name}' blocked: session '{session_id}' is in chat mode"
            )

        call_id = f"call_{uuid.uuid4().hex[:8]}"

        # 在主 loop 中调度异步调用，阻塞等待结果
        future = asyncio.run_coroutine_threadsafe(
            adapter.send_tool_call(
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                timeout=timeout,
            ),
            loop,
        )

        return future.result(timeout=timeout + 5.0)  # 留 5s 余量

    def send_status(self, content: str, session_id: Optional[str] = None) -> None:
        """Best-effort non-final status message for long PCB tool execution."""
        adapter = self._websocket_adapter
        loop = self._main_loop
        session_id = self.resolve_session_id(session_id)
        if not adapter or not loop or not loop.is_running() or not session_id:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                adapter.send(
                    chat_id=session_id,
                    content=str(content or ""),
                    metadata={"is_final": False},
                ),
                loop,
            )
        except Exception:
            logger.debug("Failed to send PCB tool status", exc_info=True)


_transport = WebSocketTransportSingleton.get_instance()


def _session_mode_error(tool_name: str, session_id: Optional[str] = None) -> str:
    session_id = _transport.resolve_session_id(session_id)
    mode = _transport.get_session_mode(session_id)
    return (
        f"工具 {tool_name} 被拒绝：当前会话处于 {mode} 模式。"
        f"请先明确进入 PCB 布线流程后再调用。session={session_id or 'none'}"
    )


# ============================================================================
# Tool 1: getProjectData
# ============================================================================

def get_project_data(session_id: Optional[str] = None) -> str:
    """
    获取 PCB 项目数据（S 表达式格式）。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslExport.ExportDbData 接口。
    Agent 拿到数据后分析其中的 BGA 元件，生成选择列表。

    Returns:
        PCB 数据的 S 表达式字符串
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("getProjectData", session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        logger.info("getProjectData start")
        result = _transport.call_tool_sync(
            tool_name="getProjectData",
            arguments={},
            timeout=30.0,
            session_id=session_id,
        )
        data = result if isinstance(result, str) else json.dumps(result)
        _transport.cache_project_data(data, session_id=session_id)  # 缓存供 route 工具使用
        logger.info("getProjectData success: %d chars", len(data))
        return data
    except Exception as e:
        logger.error("getProjectData failed: %s", e)
        return json.dumps({"error": str(e)})


registry.register(
    name="getProjectData",
    toolset="pcb",
    schema={
        "name": "getProjectData",
        "description": (
            "获取 PCB 项目数据（S 表达式格式）。"
            "若 pcb_extract_bga 工具可用，获取数据后立即调用它提取 BGA 列表；"
            "否则直接分析数据识别 BGA 元件。"
            "最终通过 ##PCB_FIELDS## 标记将 selection 返回给用户选择。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "projectID": {
                    "type": "string",
                    "description": "兼容旧版保留字段；当前前端工具获取当前打开版图，无需传参",
                }
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: get_project_data(session_id=kwargs.get("session_id")),
    check_fn=lambda: _transport.get_adapter() is not None,
)


# ============================================================================
# Tool 2: getSelectedElements / GetSelectedElements
# ============================================================================

def get_selected_elements(
    projectID: str = "",
    PFindType: str = "TRACES",
    session_id: Optional[str] = None,
    frontend_tool_name: str = "getSelectedElements",
) -> str:
    """
    获取用户在 PCB 中框选的元素 ID 列表。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslSelect.GetSelectedElements 接口。
    用于拆线重步场景：用户框选了走线，Agent 获取 ID 后执行重步布线。

    Args:
        projectID: PCB 项目的 UUID

    Returns:
        JSON 字符串: {"ids": ["wire_001", "wire_002", ...]}
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error(frontend_tool_name, session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        find_type = str(PFindType or "TRACES").strip() or "TRACES"
        logger.info("%s start: projectID=%s PFindType=%s", frontend_tool_name, projectID, find_type)
        result = _transport.call_tool_sync(
            tool_name=frontend_tool_name,
            arguments={"PFindType": find_type},
            timeout=30.0,
            session_id=session_id,
        )
        data = result if isinstance(result, str) else json.dumps(result)
        logger.info("%s success: %d chars", frontend_tool_name, len(data))
        return data
    except Exception as e:
        logger.error("%s failed: %s", frontend_tool_name, e)
        return json.dumps({"error": str(e)})


registry.register(
    name="getSelectedElements",
    toolset="pcb",
    schema={
        "name": "getSelectedElements",
        "description": (
            "Get user-selected PCB element ids from the frontend. "
            "For local rip-up/reroute this must be called with PFindType='TRACES'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "PFindType": {
                    "type": "string",
                    "description": "Selected object type. Local reroute uses TRACES.",
                    "default": "TRACES",
                }
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: get_selected_elements(
        PFindType=args.get("PFindType", "TRACES"),
        session_id=kwargs.get("session_id"),
        frontend_tool_name="getSelectedElements",
    ),
    check_fn=lambda: _transport.get_adapter() is not None,
)


registry.register(
    name="GetSelectedElements",
    toolset="pcb",
    schema={
        "name": "GetSelectedElements",
        "description": (
            "获取用户在 PCB 中框选的元素 ID 列表，用于拆线重步功能。"
            "若返回的 ids 为空，提示用户先在 PCB 中框选需要重步的走线（<40 Pin）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "projectID": {
                    "type": "string",
                    "description": "PCB 项目的 UUID",
                }
            },
            "required": ["projectID"],
        },
    },
    handler=lambda args, **kwargs: get_selected_elements(
        args.get("projectID", ""),
        PFindType=args.get("PFindType", "TRACES"),
        session_id=kwargs.get("session_id"),
        frontend_tool_name="GetSelectedElements",
    ),
    check_fn=lambda: _transport.get_adapter() is not None,
)


def delete_traces_by_id(ids: list[str], session_id: Optional[str] = None) -> str:
    """Delete selected trace ids through the PCB frontend."""
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("deleteTracesById", session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    normalized_ids = [str(item).strip() for item in (ids or []) if str(item).strip()]
    if not normalized_ids:
        return json.dumps({"error": "No trace ids were provided.", "ids": []}, ensure_ascii=False)

    try:
        logger.info("deleteTracesById start: session=%s count=%d", session_id, len(normalized_ids))
        result = _transport.call_tool_sync(
            tool_name="deleteTracesById",
            arguments={"ids": normalized_ids},
            timeout=60.0,
            session_id=session_id,
        )
        return json.dumps({"ids": normalized_ids, "result": result}, ensure_ascii=False)
    except Exception as e:
        logger.error("deleteTracesById failed: %s", e)
        return json.dumps({"ids": normalized_ids, "error": str(e)}, ensure_ascii=False)


registry.register(
    name="deleteTracesById",
    toolset="pcb",
    schema={
        "name": "deleteTracesById",
        "description": "Delete PCB traces by selected trace ids.",
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Trace ids returned by getSelectedElements.",
                }
            },
            "required": ["ids"],
        },
    },
    handler=lambda args, **kwargs: delete_traces_by_id(
        args.get("ids", []),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: _transport.get_adapter() is not None,
)


# ============================================================================
# Tool 3: route
# ============================================================================

def route_bga(userData: str, session_id: Optional[str] = None) -> str:
    """
    执行 BGA 扇出布线算法（北科大规则布线器）。

    工作流程：
      1. 从 session 缓存取版图数据（getProjectData 调用时自动保存）
      2. 写入输入文件：版图信息.txt, order_input.txt, component_input.txt, constrain.txt（arc）
      3. 根据 routerType 执行 arc 或 135 布线器 adapter
      4. 读取输出文件：routing_input.txt, data.txt
      5. 返回布线结果和报告

    Args:
        userData: 扇出参数 JSON 字符串，格式：
            {
              "orderLines": [{"net": "GND", "layer": "SIG03", "order": 1}, ...],
              "selectedBGA": "U27",
              "constraints": {"LineWidth": 4, "LineSpacing": 3}
            }
            orderLines 必填；selectedBGA 建议传入；constraints 可选。

    Returns:
        短文本报告；完整 routingResult 走 WebSocket 结构化字段。
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("route", session_id)
        logger.warning(msg)
        return json.dumps({"routingResult": "", "report": msg}, ensure_ascii=False)

    # 解析 userData
    try:
        user_data_obj = json.loads(userData) if isinstance(userData, str) else userData
    except json.JSONDecodeError:
        return json.dumps({"routingResult": "", "report": f"无效的 userData JSON: {userData[:200]}"})

    # 从 session 缓存取版图数据
    project_data = _transport.get_cached_project_data(session_id=session_id)
    if not project_data:
        return json.dumps({"routingResult": "", "report": "缺少版图数据，请先调用 getProjectData"})

    work_dir = Path(os.getenv("ROUTER_WORK_DIR", "."))
    work_dir.mkdir(parents=True, exist_ok=True)

    router_type = ""
    component_refdes = ""
    try:
        logger.info("route local start: session=%s work_dir=%s", session_id, work_dir)
        route_params = user_data_obj.get("fanoutParams") if isinstance(user_data_obj.get("fanoutParams"), dict) else user_data_obj
        if not isinstance(route_params, dict):
            return json.dumps({"routingResult": "", "report": "userData 必须是 JSON 对象"})

        router_type = _router_type_from_payload(user_data_obj, route_params)
        if not router_type:
            return json.dumps({
                "routingResult": "",
                "report": "缺少 routerType，请选择布线器：arc、135、rl、rl_arc、rl_135",
            }, ensure_ascii=False)
        from tools.pcb_bjut_router import SUPPORTED_ROUTER_TYPES, bjut_router_available, run_bjut_route

        if router_type not in SUPPORTED_ROUTER_TYPES:
            return json.dumps({
                "routingResult": "",
                "report": f"未知布线器类型: {router_type}，可选值为 {', '.join(sorted(SUPPORTED_ROUTER_TYPES))}",
            }, ensure_ascii=False)

        # Step 1: 解析目标器件并清理旧中间文件，避免读取上一次布线缓存
        component_refdes = _resolve_component_refdes(user_data_obj, route_params, session_id, project_data)
        _cleanup_router_work_dir(work_dir, component_refdes)

        # Step 2: 强制写入本次 getProjectData 版图。layout_input.txt 不再复用旧文件。
        _write_current_layout_inputs(work_dir, project_data)

        # Step 3: 写入通用 order_input.txt，arc/135 均以此作为顺序输入
        order_lines = route_params.get("orderLines", [])
        if not order_lines:
            return json.dumps({"routingResult": "", "report": "userData.orderLines 为空，无法布线"})
        logger.info("route local start: %d order lines", len(order_lines))
        _write_order_input(work_dir, order_lines, component_refdes)
        logger.info("Wrote order_input.txt: %d lines, component=%s", len(order_lines), component_refdes)

        # Step 4: 约束按布线器 profile 转换为对应 README 需要的格式
        constraints = route_params.get("constraints") or user_data_obj.get("constraints")

        # Step 5: 执行布线器
        logger.info("Resolved router profile: %s", router_type)
        fanout_params = dict(route_params)
        fanout_params.setdefault("selectedBGA", component_refdes)
        fanout_params.setdefault("routerType", router_type)
        fanout_params.setdefault("orderLines", order_lines)
        fanout_params.setdefault("constraints", constraints or {})

        if bjut_router_available(router_type, work_dir=work_dir):
            bjut_outputs = run_bjut_route(
                project_data=project_data,
                fanout_params=fanout_params,
                work_dir=work_dir,
            )
            routing_result_path = bjut_outputs.routing_result_path
            import_lines_path = bjut_outputs.import_lines_path
            report = bjut_outputs.report
        elif router_type in {"arc", "135"}:
            if router_type == "arc":
                _run_arc_router(
                    work_dir,
                    _router_profile_dir("arc", work_dir),
                    component_refdes,
                    constraints,
                    order_lines,
                    project_data,
                )
            else:
                _run_135_router(work_dir, _router_profile_dir("135", work_dir), component_refdes, constraints)
            routing_result_path = _router_result_path(work_dir)
            import_lines_path = _router_import_lines_path(work_dir, router_type, constraints)
            report = _read_router_report(work_dir)
        else:
            return json.dumps({
                "routingResult": "",
                "report": (
                    f"{router_type} 布线器目录未配置或缺少 BJUT 可执行文件；"
                    "请检查 config.ini 的 rl_*_dir 配置。"
                ),
            }, ensure_ascii=False)

        # Step 6: 传递输出文件路径，避免通过 WebSocket 发送大块版图文本
        routing_result_size = routing_result_path.stat().st_size
        report_text = str(report or "").strip().rstrip("。")
        _transport.set_pending_pcb_fields(
            {
                "routingResult": str(routing_result_path),
                "importLinesFilePath": str(import_lines_path),
                "report": report_text or "布线完成（无详细报告）",
            },
            session_id=session_id,
        )
        summary = report_text if report_text.startswith("布线完成") else f"布线完成。{report_text}"
        return (
            f"{summary}。"
            f"完整布线数据已由系统通过 WebSocket 结构化字段发送给前端，"
            f"数据文件 {routing_result_path}，大小 {routing_result_size} 字节；"
            f"EDA 导入使用布线器原始记录文件 {import_lines_path}。"
        )

    except subprocess.TimeoutExpired:
        return json.dumps({"routingResult": "", "report": "布线器执行超时（> 5 分钟）"})
    except Exception as e:
        logger.error("Router execution failed: %s", e, exc_info=True)
        return json.dumps({
            "routingResult": "",
            "report": _router_error_report(e, router_type or "未知", component_refdes or "未知器件"),
        }, ensure_ascii=False)


registry.register(
    name="route",
    toolset="pcb",
    schema={
        "name": "route",
        "description": (
            "执行 BGA 扇出布线算法，生成布线结果和报告。"
            "版图数据由系统自动从缓存获取，无需传入。"
            "执行完成后只向用户总结报告；完整 routingResult 由系统自动通过 WebSocket 结构化字段发送，"
            "不要在正文或 ##PCB_FIELDS## 中复述 routingResult。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userData": {
                    "type": "string",
                    "description": (
                        "扇出参数 JSON 字符串，格式：\n"
                        '{"orderLines": [{"net": "GND", "layer": "SIG03", "order": 1}, ...], '
                        '"selectedBGA": "U27", "routerType": "arc", '
                        '"constraints": {"LineWidth": 4, "LineSpacing": 3}}\n'
                        "orderLines 必填，selectedBGA 建议传入；"
                        "routerType 必填，可选 arc/135/rl/rl_arc/rl_135；constraints 会按布线器 README 转换。"
                    ),
                },
            },
            "required": ["userData"],
        },
    },
    handler=lambda args, **kwargs: route_bga(
        args.get("userData", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: True,
)


def _fanout_positive_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def generate_fanout_params_tool(
    selectedBGA: str = "",
    routerType: str = "",
    constraints: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> str:
    """Generate fanoutParams from cached board data via the BJUT layer/order adapter."""
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("generateFanoutParams", session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    project_data = _transport.get_cached_project_data(session_id=session_id)
    if not project_data:
        return json.dumps({
            "error": "缺少版图数据，请先调用 getProjectData。",
            "fanoutParams": None,
        }, ensure_ascii=False)

    router_type = _normalize_router_type(routerType)
    if not router_type:
        return json.dumps({
            "error": "缺少 routerType，请先让用户选择 arc、135、rl、rl_arc 或 rl_135。",
            "fanoutParams": None,
        }, ensure_ascii=False)

    selected_bga = str(selectedBGA or "").strip()
    if not selected_bga:
        selected_bga = _resolve_component_refdes(
            {"selectedBGA": selected_bga, "routerType": router_type},
            {"selectedBGA": selected_bga, "routerType": router_type},
            session_id,
            project_data,
        )

    normalized_constraints = constraints if isinstance(constraints, dict) else {}
    normalized_constraints = {
        "LineWidth": _fanout_positive_number(normalized_constraints.get("LineWidth"), 4),
        "LineSpacing": _fanout_positive_number(normalized_constraints.get("LineSpacing"), 3),
    }

    try:
        from tools.pcb_bjut_router import bjut_router_available, generate_fanout_params

        work_dir = Path(os.getenv("ROUTER_WORK_DIR", ".")).resolve()
        if not bjut_router_available(router_type, work_dir=work_dir):
            return json.dumps({
                "error": (
                    f"{router_type} fanoutParams 生成器不可用；"
                    "请检查 config.ini 中对应布线器目录。"
                ),
                "fanoutParams": None,
            }, ensure_ascii=False)

        fanout_params = generate_fanout_params(
            project_data=project_data,
            selected_bga=selected_bga,
            router_type=router_type,
            work_dir=work_dir,
            constraints=normalized_constraints,
        )
        if not isinstance(fanout_params, dict):
            fanout_params = {}
        fanout_params.setdefault("selectedBGA", selected_bga)
        fanout_params.setdefault("routerType", router_type)
        fanout_params.setdefault("constraints", normalized_constraints)
        return json.dumps({"fanoutParams": fanout_params}, ensure_ascii=False)
    except Exception as exc:
        logger.error("generateFanoutParams failed: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc), "fanoutParams": None}, ensure_ascii=False)


registry.register(
    name="generateFanoutParams",
    toolset="pcb",
    schema={
        "name": "generateFanoutParams",
        "description": (
            "Generate BGA fanoutParams from cached getProjectData board data. "
            "Call this after getProjectData and pcb_extract_bga, once selectedBGA and routerType are known. "
            "Return the resulting fanoutParams to the user for confirmation before calling route."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "selectedBGA": {
                    "type": "string",
                    "description": "Selected BGA refdes, for example U22.",
                },
                "routerType": {
                    "type": "string",
                    "description": "Router type: arc, 135, rl, rl_arc, or rl_135.",
                },
                "constraints": {
                    "type": "object",
                    "description": "Optional routing constraints, e.g. LineWidth/LineSpacing in mil.",
                },
            },
            "required": ["selectedBGA", "routerType"],
        },
    },
    handler=lambda args, **kwargs: generate_fanout_params_tool(
        selectedBGA=args.get("selectedBGA", ""),
        routerType=args.get("routerType", ""),
        constraints=args.get("constraints") if isinstance(args.get("constraints"), dict) else None,
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: True,
)


_NET_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:net|NET)[A-Za-z0-9_.+\-/]*")


def extract_reroute_nets(user_text: str) -> list[str]:
    """Extract net names from a natural-language reroute request."""
    found: list[str] = []
    text = str(user_text or "")
    found.extend(match.group(0) for match in _NET_TOKEN_RE.finditer(text))
    for quoted in re.findall(r"[`'\"“”‘’]([^`'\"“”‘’]{1,80})[`'\"“”‘’]", text):
        candidate = quoted.strip()
        if _NET_TOKEN_RE.fullmatch(candidate):
            found.append(candidate)

    seen: set[str] = set()
    nets: list[str] = []
    for raw in found:
        net = raw.strip().strip("，。,.!?！？:：;；、")
        key = net.casefold()
        if net and key != "net" and key not in seen:
            seen.add(key)
            nets.append(net)
    return nets


def _parse_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except Exception:
        return value


def _normalize_id_list(value: Any) -> list[str]:
    parsed = _parse_json_value(value)
    if isinstance(parsed, dict):
        for key in ("ids", "selectedIds", "selectedTraceIds", "result"):
            if key in parsed:
                return _normalize_id_list(parsed[key])
        return []
    if isinstance(parsed, (list, tuple, set)):
        ids: list[str] = []
        for item in parsed:
            raw = item.get("id") or item.get("ID") or item.get("uid") if isinstance(item, dict) else item
            text = str(raw or "").strip()
            if text:
                ids.append(text)
        return ids
    return []


def _delete_traces_succeeded(value: Any) -> bool:
    parsed = _parse_json_value(value)
    if isinstance(parsed, dict):
        if parsed.get("success") is True or parsed.get("ok") is True:
            return True
        if parsed.get("success") is False or parsed.get("ok") is False:
            return False
        return any(_delete_traces_succeeded(parsed[key]) for key in ("result", "message", "status") if key in parsed)
    text = str(parsed or "").strip().lower()
    return bool(text) and (
        "已成功删除" in text
        or "成功" in text
        or "success" in text
        or text in {"ok", "true", "deleted"}
    )


def _first_text_value(payload: Dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_board_file(path_text: str) -> tuple[str, str]:
    if not path_text:
        return "", ""
    path = Path(path_text).expanduser()
    if not path.is_file():
        logger.warning("Board data path is not a file: %s", path_text)
        return "", path_text
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except OSError as exc:
        logger.warning("Failed reading board data file %s: %s", path_text, exc)
        return "", str(path)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _convert_module_path() -> Path:
    configured = os.getenv("PCB_CONVERT_PY", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    candidates = [
        _repo_root() / "convert.py",
    ]
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        bundle_path = Path(bundled_root)
        candidates.insert(0, bundle_path / "convert.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _load_convert_module():
    module_path = _convert_module_path()
    if not module_path.is_file():
        raise FileNotFoundError(f"convert.py not found: {module_path}")
    spec = importlib.util.spec_from_file_location("_hermes_pcb_convert", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load convert.py from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_hermes_pcb_convert"] = module
    spec.loader.exec_module(module)
    return module


def _looks_like_kicad_board_data(text: str) -> bool:
    return bool(re.search(r"(?is)^\s*\(\s*kicad_pcb\b", text or ""))


def _looks_like_layout_txt_data(text: str) -> bool:
    return bool(re.search(r"(?is)^\s*\(\s*layout\b|Pcb-Design_Version|layermanager|conductives", text or ""))


def _safe_reroute_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return cleaned[:96] or "reroute"


def _write_reroute_debug_artifact(
    *,
    output_dir: str,
    session_id: str,
    label: str,
    content: str,
) -> str:
    if os.getenv("PCB_REROUTE_WRITE_MODEL_OUTPUTS", "").strip().lower() not in ("1", "true", "yes", "on"):
        return ""
    if not output_dir or not content:
        return ""
    path = Path(output_dir) / "model_outputs" / f"{_safe_reroute_name(session_id)}_{_safe_reroute_name(label)}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _write_internal_board_data(
    *,
    board_data: str,
    board_path: str,
    output_dir: str,
    session_id: str,
    label: str,
) -> tuple[str, str, list[str]]:
    """
    Normalize frontend PCB Builder txt/S-expression data into an internal KiCad file.

    Public websocket fields must not expose the KiCad path. The returned path is
    only used by reroute DRC/patch internals and output conversion.
    """
    notes: list[str] = []
    if board_path and str(board_path).lower().endswith(".kicad_pcb"):
        text, resolved = _read_board_file(board_path)
        if text:
            return text, resolved, notes

    if _looks_like_kicad_board_data(board_data):
        base_dir = Path(output_dir) / "_internal"
        base_dir.mkdir(parents=True, exist_ok=True)
        path = base_dir / f"{_safe_reroute_name(session_id)}_{label}.kicad_pcb"
        path.write_text(board_data, encoding="utf-8")
        return board_data, str(path), notes

    if not board_data:
        return "", "", notes

    if not _looks_like_layout_txt_data(board_data) and not board_path.lower().endswith(".txt"):
        return board_data, "", notes

    base_dir = Path(output_dir) / "_internal"
    txt_dir = base_dir / "txt"
    kicad_dir = base_dir / "kicad"
    txt_dir.mkdir(parents=True, exist_ok=True)
    kicad_dir.mkdir(parents=True, exist_ok=True)

    if board_path and str(board_path).lower().endswith(".txt") and Path(board_path).is_file():
        txt_path = Path(board_path)
    else:
        txt_path = txt_dir / f"{_safe_reroute_name(session_id)}_{label}.txt"
        txt_path.write_text(board_data, encoding="utf-8")

    convert_mod = _load_convert_module()
    result = convert_mod.convert_one("txt_to_kicad", txt_path, kicad_dir, None)
    output_path = Path(str(result.get("output") or ""))
    if not output_path.is_file():
        raise RuntimeError(f"txt_to_kicad did not create output for reroute input: {txt_path}")
    notes.append(f"converted_input_txt_to_kicad:{txt_path}")
    return output_path.read_text(encoding="utf-8"), str(output_path), notes


def _convert_internal_kicad_to_public_txt(
    *,
    kicad_path: str,
    output_dir: str,
    session_id: str,
    output_subdir: str = "txt",
) -> tuple[str, list[str]]:
    if not kicad_path:
        return "", []
    path = Path(kicad_path)
    if path.suffix.lower() == ".txt":
        if output_subdir != "txt" and path.is_file():
            txt_dir = Path(output_dir) / output_subdir
            txt_dir.mkdir(parents=True, exist_ok=True)
            output_path = txt_dir / path.name
            if path.resolve() != output_path.resolve():
                shutil.copyfile(path, output_path)
            return str(output_path), [f"copied_output_txt_to_{output_subdir}:{output_path}"]
        return str(path), []
    if path.suffix.lower() != ".kicad_pcb" or not path.is_file():
        return "", []

    txt_dir = Path(output_dir) / (output_subdir or "txt")
    txt_dir.mkdir(parents=True, exist_ok=True)
    convert_mod = _load_convert_module()
    result = convert_mod.convert_one("kicad_to_txt", path, txt_dir, None)
    output_path = Path(str(result.get("output") or ""))
    if not output_path.is_file():
        raise RuntimeError(f"kicad_to_txt did not create output for reroute result: {path}")
    return str(output_path), [f"converted_output_kicad_to_txt:{output_path}"]


def _kicad_mm_to_mil_text(value: str) -> str:
    return f"{(float(value) / 0.0254):.2f}"


def _kicad_layer_to_import_layer(layer: str) -> str:
    layer = str(layer or "").strip().strip('"')
    if not layer:
        return "Conductor/Top"
    convert_mod = _load_convert_module()
    txt_layer = convert_mod.layer_kicad_to_txt(layer)
    return convert_mod.conductor_layer_txt(txt_layer)


def _kicad_net_id_to_name(board_text: str) -> dict[str, str]:
    return {net_id: net_name for net_name, net_id in _parse_kicad_net_name_to_id(board_text).items()}


def _write_reroute_incremental_import_file(
    *,
    patch_text: str,
    board_text: str,
    output_dir: str,
    session_id: str,
) -> tuple[str, list[str]]:
    """Write a small importLines input containing only the reroute patch wires."""
    patch_text = str(patch_text or "").strip()
    if not patch_text:
        return "", []

    net_id_to_name = _kicad_net_id_to_name(board_text)
    wire_blocks: list[str] = []
    for block in _extract_balanced_sexpr_blocks(patch_text, "segment"):
        start = re.search(r"\(\s*start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        end = re.search(r"\(\s*end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        width = re.search(r"\(\s*width\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        layer = re.search(r"\(\s*layer\s+\"?([^\s\)\"]+)\"?\s*\)", block, re.IGNORECASE)
        net = re.search(r"\(\s*net\s+(\d+)\s*\)", block, re.IGNORECASE)
        if not (start and end and width and layer and net):
            continue

        net_name = net_id_to_name.get(net.group(1).strip(), net.group(1).strip())
        import_layer = _kicad_layer_to_import_layer(layer.group(1))
        width_dbu = _scale_mils_to_int(_kicad_mm_to_mil_text(width.group(1)))
        x1 = _scale_mils_to_int(_kicad_mm_to_mil_text(start.group(1)))
        y1 = _scale_mils_to_int(_kicad_mm_to_mil_text(start.group(2)))
        x2 = _scale_mils_to_int(_kicad_mm_to_mil_text(end.group(1)))
        y2 = _scale_mils_to_int(_kicad_mm_to_mil_text(end.group(2)))

        wire_blocks.extend(
            [
                "    (wire",
                f'        (net "{net_name}")',
                "        (path",
                '            (issamewidth "true")',
                "            (lineseg",
                f"                (pt {x1} {y1})",
                f"                (w {width_dbu})",
                "            )",
                "            (lineseg",
                f"                (pt {x2} {y2})",
                f"                (w {width_dbu})",
                "            )",
                "            (props)",
                f'            (layer "{import_layer}")',
                "        )",
                "    )",
            ]
        )

    if not wire_blocks:
        return "", []

    import_dir = Path(output_dir) / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "session").strip("_") or "session"
    output_path = import_dir / f"{safe_session}_reroute_import.txt"
    output_path.write_text("\n".join(["(wires", *wire_blocks, ")"]) + "\n", encoding="utf-8")
    return str(output_path), [f"generated_reroute_incremental_import:{output_path}"]


_INTERNAL_REROUTE_PATH_KEYS = {
    "routedBoardDataFilePath",
    "originalBoardDataFilePath",
    "droppedBoardDataFilePath",
    "filledBoardDataFilePath",
    "kicadPatch",
    "kicad_patch",
    "patchText",
    "rawModelOutput",
    "modelRawOutputFilePath",
    "extractedPatchFilePath",
}

_INTERNAL_KICAD_PATH_RE = re.compile(
    r"([A-Za-z]:[\\/][^\s\"'，,;；]+\.kicad_pcb|/[^\s\"'，,;；]+\.kicad_pcb)",
    re.IGNORECASE,
)
def _strip_internal_reroute_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_internal_reroute_paths(item)
            for key, item in value.items()
            if key not in _INTERNAL_REROUTE_PATH_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_reroute_paths(item) for item in value]
    if isinstance(value, str):
        text = _INTERNAL_KICAD_PATH_RE.sub("内部版图文件", value).replace(".kicad_pcb", "")
        return re.sub(r"(?i)kicad(?:_pcb)?", "PCB版图", text)
    return value


def _compact_public_text(value: Any, limit: int = 1200, *, preserve_newlines: bool = False) -> str:
    text = str(_strip_internal_reroute_paths(value or "")).strip()
    if preserve_newlines:
        text = "\n".join(re.sub(r"[ \t\f\v]+", " ", line).rstrip() for line in text.splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _compact_public_drc_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return _strip_internal_reroute_paths(value)
    result: Dict[str, Any] = {}
    for key in ("ok", "pass", "score", "score_name", "error"):
        if key in value:
            result[key] = _strip_internal_reroute_paths(value.get(key))
    details = value.get("details")
    if isinstance(details, dict):
        result["details"] = {
            key: _strip_internal_reroute_paths(details.get(key))
            for key in ("hard_issue_count", "hard_penalty", "hard_rule_counts")
            if key in details
        }
    artifacts = value.get("artifacts")
    issues = artifacts.get("issues") if isinstance(artifacts, dict) else None
    if isinstance(issues, list):
        result["issuesPreview"] = [
            {
                "rule": item.get("rule"),
                "severity": item.get("severity"),
                "message": _compact_public_text(item.get("message") or item.get("description"), 220),
                "net": item.get("net"),
                "layer": item.get("layer"),
                "pad_id": item.get("pad_id"),
            }
            for item in issues[:5]
            if isinstance(item, dict)
        ]
        result["issueCount"] = len(issues)
    return result


def _compact_public_drc_agent_report(value: Any) -> Any:
    if not isinstance(value, dict):
        return _strip_internal_reroute_paths(value)
    result: Dict[str, Any] = {
        "ok": bool(value.get("ok")),
    }
    if value.get("jsonPath") or value.get("json_path"):
        result["jsonPath"] = str(value.get("jsonPath") or value.get("json_path"))
    if value.get("returncode") is not None:
        result["returncode"] = value.get("returncode")
    if value.get("error"):
        result["error"] = _compact_public_text(value.get("error"), 900)

    payload = value.get("payload")
    if not isinstance(payload, dict):
        return result

    compact_payload: Dict[str, Any] = {}
    for key in ("schema_version", "language"):
        if key in payload:
            compact_payload[key] = _strip_internal_reroute_paths(payload.get(key))
    if payload.get("message_zh"):
        compact_payload["message_zh"] = _compact_public_text(
            payload.get("message_zh"),
            5000,
            preserve_newlines=True,
        )
    for key in ("result", "board_info", "routing_metrics", "precheck"):
        if isinstance(payload.get(key), dict):
            compact_payload[key] = _strip_internal_reroute_paths(payload.get(key))
    issues = payload.get("issues")
    if isinstance(issues, list):
        compact_payload["issueCount"] = len(issues)
        compact_payload["issuesPreview"] = [
            {
                "rule": item.get("rule"),
                "rule_name_zh": item.get("rule_name_zh"),
                "severity": item.get("severity"),
                "severity_zh": item.get("severity_zh"),
                "location_zh": _compact_public_text(item.get("location_zh"), 260),
                "suggestion_zh": _compact_public_text(item.get("suggestion_zh"), 260),
            }
            for item in issues[:5]
            if isinstance(item, dict)
        ]
    result["payload"] = compact_payload
    return result


def _compact_public_attempt(attempt: Any) -> Any:
    if not isinstance(attempt, dict):
        return _strip_internal_reroute_paths(attempt)
    result = {
        "iteration": attempt.get("iteration"),
        "passed": attempt.get("passed"),
        "fillDetail": _strip_internal_reroute_paths(attempt.get("fillDetail")),
        "failureSummary": _compact_public_text(attempt.get("failureSummary"), 700),
    }
    if isinstance(attempt.get("drcResult"), dict):
        result["drcResult"] = _compact_public_drc_result(attempt.get("drcResult"))
    return result


def _compact_public_check_report(value: Any) -> Any:
    if not isinstance(value, dict):
        return _strip_internal_reroute_paths(value)
    result = dict(_strip_internal_reroute_paths(value))
    checks = result.get("checks")
    if isinstance(checks, list):
        compact_checks = []
        for check in checks:
            if not isinstance(check, dict):
                continue
            item = dict(check)
            if "detail" in item:
                item["detail"] = _compact_public_text(item.get("detail"), 700)
            compact_checks.append(item)
        result["checks"] = compact_checks
    errors = result.get("errors")
    if isinstance(errors, list):
        result["errors"] = [_compact_public_text(error, 500) for error in errors[:8]]
        result["errorCount"] = len(errors)
    return result


def _compact_public_reroute_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return _strip_internal_reroute_paths(value)
    result = dict(_strip_internal_reroute_paths(value))
    if isinstance(result.get("drcAgentReport"), dict):
        result["drcAgentReport"] = _compact_public_drc_agent_report(result["drcAgentReport"])
    attempts = result.get("drcAttempts")
    if isinstance(attempts, list):
        result["drcAttempts"] = [_compact_public_attempt(item) for item in attempts[-3:]]
        result["drcAttemptCount"] = len(attempts)
    reasons = result.get("drcFailureReasons")
    if isinstance(reasons, list):
        result["drcFailureReasons"] = [_compact_public_text(reason, 700) for reason in reasons[-3:]]
        result["drcFailureReasonCount"] = len(reasons)
    operations = result.get("operations")
    if isinstance(operations, list) and len(operations) > 12:
        result["operations"] = operations[:12]
        result["operationCount"] = len(operations)
    return result


def _compact_public_reroute_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(payload)
    if isinstance(result.get("rerouteResult"), dict):
        result["rerouteResult"] = _compact_public_reroute_result(result["rerouteResult"])
    if isinstance(result.get("checkReport"), dict):
        result["checkReport"] = _compact_public_check_report(result["checkReport"])
    if "explanation" in result:
        result["explanation"] = _compact_public_text(result.get("explanation"), 1800)
    if "content" in result:
        result["content"] = _compact_public_text(result.get("content"), 12000, preserve_newlines=True)
    if "report" in result:
        result["report"] = _compact_public_text(result.get("report"), 12000, preserve_newlines=True)
    return result


def _public_reroute_payload(
    payload: Dict[str, Any],
    public_txt_path: str,
    import_lines_path: str = "",
) -> Dict[str, Any]:
    result = _strip_internal_reroute_paths(payload)
    if not isinstance(result, dict):
        result = {}
    if public_txt_path:
        result["routedLayoutTxtFilePath"] = public_txt_path
        reroute_result = dict(result.get("rerouteResult") or {})
        reroute_result["routedLayoutTxtFilePath"] = public_txt_path
        result["rerouteResult"] = reroute_result
    if import_lines_path:
        result["importLinesFilePath"] = import_lines_path
        reroute_result = dict(result.get("rerouteResult") or {})
        reroute_result["importLinesFilePath"] = import_lines_path
        result["rerouteResult"] = reroute_result
    return _compact_public_reroute_payload(result)


def _pending_reroute_fields_for_frontend(
    public_payload: Dict[str, Any],
    public_txt_path: str,
    import_lines_path: str = "",
) -> Dict[str, Any]:
    if not isinstance(public_payload, dict) or not public_payload:
        return {}

    fields: Dict[str, Any] = {}
    reroute_result = public_payload.get("rerouteResult")
    if isinstance(reroute_result, dict):
        fields["rerouteResult"] = reroute_result
    if public_txt_path:
        fields["routedLayoutTxtFilePath"] = public_txt_path
    if import_lines_path:
        fields["importLinesFilePath"] = import_lines_path
    check_report = public_payload.get("checkReport")
    if isinstance(check_report, dict):
        fields["checkReport"] = check_report
    explanation = str(public_payload.get("explanation") or "").strip()
    if explanation:
        fields["explanation"] = explanation
    report = _nested_text_value(public_payload, "content", "report")
    if report:
        fields["report"] = report
    return fields


def _reroute_drc_passed_from_payload(payload: Dict[str, Any]) -> bool | None:
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    if isinstance(reroute_result, dict) and isinstance(reroute_result.get("drcPassed"), bool):
        return reroute_result.get("drcPassed")

    check_report = payload.get("checkReport") if isinstance(payload, dict) else {}
    checks = check_report.get("checks") if isinstance(check_report, dict) else []
    if isinstance(checks, list):
        for check in reversed(checks):
            if not isinstance(check, dict) or check.get("name") != "drc_validation":
                continue
            passed = check.get("passed")
            if isinstance(passed, bool):
                return passed
    return None


def _latest_reroute_failure_reason(payload: Dict[str, Any]) -> str:
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    if isinstance(reroute_result, dict):
        model_failure = str(reroute_result.get("modelGenerationFailure") or "").strip()
        if model_failure:
            return model_failure
        reasons = reroute_result.get("drcFailureReasons")
        if isinstance(reasons, list):
            for reason in reversed(reasons):
                text = str(reason or "").strip()
                if text:
                    return text
        attempts = reroute_result.get("drcAttempts")
        if isinstance(attempts, list):
            for attempt in reversed(attempts):
                if not isinstance(attempt, dict):
                    continue
                text = str(attempt.get("failureSummary") or "").strip()
                if text:
                    return text

    check_report = payload.get("checkReport") if isinstance(payload, dict) else {}
    checks = check_report.get("checks") if isinstance(check_report, dict) else []
    if isinstance(checks, list):
        for check in reversed(checks):
            if not isinstance(check, dict) or check.get("passed") is not False:
                continue
            detail = str(check.get("detail") or "").strip()
            if detail:
                return detail
    return ""


def _latest_reroute_drc_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    attempts = reroute_result.get("drcAttempts") if isinstance(reroute_result, dict) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            if isinstance(attempt, dict) and isinstance(attempt.get("drcResult"), dict):
                return attempt["drcResult"]
    return {}


def _latest_reroute_filled_board_path(payload: Dict[str, Any]) -> str:
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    attempts = reroute_result.get("drcAttempts") if isinstance(reroute_result, dict) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            if not isinstance(attempt, dict):
                continue
            path = str(attempt.get("filledBoardDataFilePath") or "").strip()
            if path:
                return path
    return ""


def _drc_agent_message_zh(payload: Dict[str, Any]) -> str:
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    report = reroute_result.get("drcAgentReport") if isinstance(reroute_result, dict) else None
    report = report if isinstance(report, dict) else {}
    report_payload = report.get("payload")
    if isinstance(report_payload, dict):
        message = str(report_payload.get("message_zh") or "").strip()
        if message:
            return _compact_public_text(message, 5000, preserve_newlines=True)
    return ""


def _drc_agent_failure_summary(payload: Dict[str, Any]) -> str:
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    report = reroute_result.get("drcAgentReport") if isinstance(reroute_result, dict) else None
    if not isinstance(report, dict) or report.get("ok"):
        return ""
    error = str(report.get("error") or "").strip()
    if error:
        return _compact_public_text(error, 700)
    report_payload = report.get("payload")
    if isinstance(report_payload, dict):
        nested_error = report_payload.get("error")
        if isinstance(nested_error, dict):
            text = str(nested_error.get("message") or nested_error.get("type") or "").strip()
            if text:
                return _compact_public_text(text, 700)
    return "增强 DRC 报告生成失败，已保留基础 DRC 摘要。"


def _append_drc_agent_report(
    payload: Dict[str, Any],
    *,
    board_path: str,
    output_dir: str,
    session_id: str,
    target_bga: str = "",
) -> Dict[str, Any]:
    result = dict(payload)
    if not str(board_path or "").strip():
        report = {
            "ok": False,
            "error": "缺少可用于增强 DRC 报告的模型回填版图文件。",
            "jsonPath": "",
        }
    else:
        try:
            report = generate_drc_agent_report(
                board_file_path=board_path,
                output_dir=output_dir,
                session_id=session_id,
                target_bga=target_bga,
            )
        except Exception as exc:
            report = {
                "ok": False,
                "error": f"增强 DRC 中文报告生成异常：{type(exc).__name__}: {exc}",
                "jsonPath": "",
            }
        if not isinstance(report, dict):
            report = {
                "ok": False,
                "error": f"增强 DRC 中文报告返回了非字典结果：{type(report).__name__}",
                "jsonPath": "",
            }
        elif "json_path" in report and "jsonPath" not in report:
            report = {**report, "jsonPath": report.get("json_path")}

    reroute_result = dict(result.get("rerouteResult") or {})
    reroute_result["drcAgentReport"] = report
    result["rerouteResult"] = reroute_result

    check_report = dict(result.get("checkReport") or {})
    checks = list(check_report.get("checks") or [])
    detail = (
        f"增强 DRC 中文报告已生成：{report.get('jsonPath') or report.get('json_path')}"
        if isinstance(report, dict) and report.get("ok")
        else f"增强 DRC 中文报告生成失败：{_compact_public_text((report or {}).get('error'), 700)}"
    )
    checks.append(
        {
            "name": "drc_agent_report",
            "passed": bool(report.get("ok")),
            "detail": detail,
            "blocking": False,
        }
    )
    check_report["checks"] = checks
    check_report["passed"] = bool(check_report.get("passed", True))
    result["checkReport"] = check_report
    return result


def _reroute_target_bga_for_drc_agent(*values: Any) -> str:
    payload_keys = (
        "targetBga",
        "targetBGA",
        "selectedBGA",
        "selectedBga",
        "component",
        "componentRefdes",
        "refdes",
    )
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in payload_keys:
            refdes = _clean_component_refdes(value.get(key))
            if refdes:
                return refdes
        for route in _frontend_missing_routes_from_context(value):
            for endpoint_key in ("start", "end"):
                endpoint = route.get(endpoint_key) if isinstance(route, dict) else None
                if isinstance(endpoint, dict):
                    refdes = _clean_component_refdes(endpoint.get("component"))
                    if refdes:
                        return refdes
    return ""


def _drc_failure_markdown_summary(payload: Dict[str, Any]) -> str:
    drc_result = _latest_reroute_drc_result(payload)
    details = drc_result.get("details") if isinstance(drc_result, dict) else {}
    if not isinstance(details, dict):
        details = {}
    hard_count = details.get("hard_issue_count")
    rule_counts = details.get("hard_rule_counts")
    issues = drc_result.get("issuesPreview") if isinstance(drc_result, dict) else None

    lines: list[str] = []
    if hard_count not in (None, ""):
        lines.append(f"- 硬 DRC 问题数：{hard_count}")
    if isinstance(rule_counts, dict) and rule_counts:
        rule_text = "；".join(f"{key}: {value}" for key, value in rule_counts.items())
        lines.append(f"- 规则计数：{rule_text}")
    if isinstance(issues, list) and issues:
        lines.append("- 主要问题：")
        for item in issues[:5]:
            if not isinstance(item, dict):
                continue
            rule = str(item.get("rule") or "DRC").strip()
            message = _compact_public_text(item.get("message") or item.get("description"), 220)
            if message:
                lines.append(f"  - `{rule}`：{message}")
    if lines:
        return "\n".join(lines)

    failure_reason = str(_strip_internal_reroute_paths(_latest_reroute_failure_reason(payload))).strip()
    return _compact_public_text(failure_reason, 1200) or "DRC 校验未通过，但未返回更具体的失败原因。"


def _compose_drc_analysis_report(
    payload: Dict[str, Any],
    public_txt_path: str,
    import_lines_path: str = "",
) -> str:
    drc_passed = _reroute_drc_passed_from_payload(payload)
    failure_reason = str(_strip_internal_reroute_paths(_latest_reroute_failure_reason(payload))).strip()
    reroute_result = payload.get("rerouteResult") if isinstance(payload, dict) else {}
    reroute_result = reroute_result if isinstance(reroute_result, dict) else {}
    txt_generated = bool(public_txt_path)
    import_allowed = drc_passed is True and txt_generated and bool(import_lines_path)

    if drc_passed is True:
        status = "通过"
        conclusion_label = "成功结论"
        local_policy = reroute_result.get("localReroutePolicy") if isinstance(reroute_result, dict) else {}
        if isinstance(local_policy, dict) and local_policy.get("mode") == "selected_net_local":
            inherited = local_policy.get("inheritedIssueCount", 0)
            conclusion = (
                "局部 DRC 校验通过，所选网络的重布结果满足导入门禁。"
                f"原板仍有 {inherited} 个非所选网络的全局遗留问题，已作为报告信息保留，不阻止本次 importLines。"
            )
        else:
            conclusion = "DRC 校验通过，重布结果满足当前硬约束。"
    elif drc_passed is False:
        status = "未通过"
        conclusion_label = "失败原因"
        conclusion = _drc_failure_markdown_summary(payload)
    else:
        status = "未执行"
        conclusion_label = "失败原因"
        conclusion = _compact_public_text(failure_reason, 1200) or "尚未进入 DRC 校验，通常是模型未生成可回填 patch 或输入数据不足。"

    if txt_generated:
        txt_status = f"已生成：{public_txt_path}"
    elif drc_passed is True:
        txt_status = "未生成，DRC 虽通过但结果尚未转换成可导入 txt。"
    else:
        txt_status = "未生成，DRC 未通过或未执行。"

    failed_txt_path = str(reroute_result.get("drcFailedLayoutTxtFilePath") or "").strip()
    failed_txt_status = f"已保存：{failed_txt_path}" if failed_txt_path else "未保存。"
    import_status = "允许，满足 DRC 通过且 txt 已生成。" if import_allowed else "不允许，必须 DRC 通过且生成 txt 后才允许 importLines。"
    lines = [
        "DRC 分析",
        "========",
        "",
        f"DRC 状态: {status}",
        f"{conclusion_label}: {conclusion}",
    ]
    drc_agent_message = _drc_agent_message_zh(payload)
    if drc_agent_message:
        lines.extend(["", drc_agent_message])
    else:
        drc_agent_failure = _drc_agent_failure_summary(payload)
        if drc_agent_failure:
            lines.extend(["", f"DRC规则检查结果：{drc_agent_failure}"])
    lines.extend(
        [
            f"txt 输出: {txt_status}",
            f"失败回填txt: {failed_txt_status}",
            f"importLines: {import_status}",
        ]
    )
    return "\n".join(lines).strip()


def _compose_reroute_report_content(
    *,
    payload: Dict[str, Any],
    public_txt_path: str,
    import_lines_path: str = "",
    explain_report: str,
) -> str:
    drc_report = _compose_drc_analysis_report(payload, public_txt_path, import_lines_path)
    explain_text = str(_strip_internal_reroute_paths(explain_report or "")).strip()
    if not explain_text:
        explain_text = "Explain 模型未返回可用报告。"
    explain_text = _compact_public_text(explain_text, 3000, preserve_newlines=True)
    explain_section = (
        "Explain 模型可解释性报告\n"
        "======================\n\n"
        "以下内容来自本地 explain 模型，仅作为布线质量解释，不覆盖上面的 DRC 结论。\n\n"
        f"{explain_text}"
    )
    return f"{drc_report}\n\n{explain_section}".strip()


def _normalize_openai_base_url(value: str) -> str:
    return pcb_model_runtime.normalize_openai_base_url(value)


def _env_first(*names: str) -> str:
    return pcb_model_runtime._env_first(*names)


def _looks_like_real_secret(value: str) -> bool:
    return pcb_model_runtime._looks_like_real_secret(value)


def _extract_explain_runtime_config_from_doc() -> Dict[str, str]:
    return pcb_model_runtime.extract_stage_runtime_config_from_doc(
        pcb_model_runtime.STAGE_EXPLAIN,
        doc_paths=[_repo_root() / "share" / "天翼云部署模型使用说明.md"],
    )


def _resolve_explain_runtime_config() -> Dict[str, str]:
    return pcb_model_runtime.resolve_model_runtime(
        pcb_model_runtime.STAGE_EXPLAIN,
        doc_paths=[_repo_root() / "share" / "天翼云部署模型使用说明.md"],
        require_api_key=True,
    )


def _strip_think_blocks(text: str) -> str:
    clean = pcb_model_runtime.strip_think_blocks(text)
    clean = re.sub(r"(?is)^```(?:text|markdown|md)?\s*", "", clean.strip())
    clean = re.sub(r"(?is)\s*```$", "", clean.strip())
    return clean.strip()


def _safe_json_for_explain(value: Any) -> Any:
    if isinstance(value, dict) and ("drcAttempts" in value or "drcFailureReasons" in value):
        return _compact_public_reroute_result(value)
    if isinstance(value, dict) and "checks" in value:
        return _compact_public_check_report(value)
    return _strip_internal_reroute_paths(value)


def _read_board_excerpt_for_explain(board_path: str, *, max_chars: int | None = None) -> Dict[str, Any]:
    raw_limit = os.getenv("PCB_EXPLAIN_MAX_BOARD_CHARS", "").strip()
    if max_chars is None:
        try:
            max_chars = int(raw_limit) if raw_limit else 24000
        except ValueError:
            max_chars = 24000
    max_chars = max(2000, min(120000, max_chars))
    board_text, _resolved = _read_board_file(board_path)
    if not board_text:
        return {"format": ".kicad_pcb", "available": False, "chars": 0, "content": ""}
    if len(board_text) <= max_chars:
        excerpt = board_text
        truncated = False
    else:
        head_chars = max_chars // 2
        tail_chars = max_chars - head_chars
        excerpt = (
            board_text[:head_chars]
            + "\n\n...<中间内容已截断，供可解释性模型使用>...\n\n"
            + board_text[-tail_chars:]
        )
        truncated = True
    return {
        "format": ".kicad_pcb",
        "available": True,
        "chars": len(board_text),
        "truncated": truncated,
        "content": excerpt,
    }


def _build_explain_prompt(
    *,
    internal_board_path: str,
    payload: Dict[str, Any],
    public_txt_path: str,
) -> list[Dict[str, str]]:
    explain_input = {
        "boardFile": _read_board_excerpt_for_explain(internal_board_path),
        "rerouteResult": _safe_json_for_explain((payload.get("rerouteResult") or {}) if isinstance(payload, dict) else {}),
        "checkReport": _safe_json_for_explain((payload.get("checkReport") or {}) if isinstance(payload, dict) else {}),
        "txtGenerated": bool(public_txt_path),
        "importPolicy": "DRC 通过才允许 importLines；DRC 失败不导入。",
    }
    system_prompt = (
        "你是 PCB 布线结果分析助手。请根据输入的内部 KiCad 版图内容、重布线结果和 DRC 检查结果，"
        "生成简洁中文可解释性报告。不要输出 JSON，不要编造未给出的文件路径，不要暴露任何 .kicad_pcb 路径。"
    )
    user_prompt = (
        "生成拆线重布后的可解释性报告。\n"
        "报告只解释当前重布结果、DRC 结论和是否可导入，不要复述内部路径。\n\n"
        f"{json.dumps(explain_input, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"/no_think\n{user_prompt}"},
    ]


def _call_ctyun_explain_chat(messages: list[Dict[str, str]]) -> str:
    timeout_raw = os.getenv("CTYUN_EXPLAIN_TIMEOUT", os.getenv("PCB_EXPLAIN_TIMEOUT", "180")).strip()
    max_tokens_raw = os.getenv("CTYUN_EXPLAIN_MAX_TOKENS", os.getenv("PCB_EXPLAIN_MAX_TOKENS", "2048")).strip()
    try:
        timeout = max(10.0, float(timeout_raw))
    except ValueError:
        timeout = 180.0
    try:
        max_tokens = max(256, min(8192, int(max_tokens_raw)))
    except ValueError:
        max_tokens = 2048

    content, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_EXPLAIN,
        runtime=_resolve_explain_runtime_config(),
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.2,
        timeout_s=timeout,
        require_api_key=True,
    )
    content = _strip_think_blocks(str(content or ""))
    if not content:
        raise RuntimeError("explain model returned empty content")
    return str(_strip_internal_reroute_paths(content)).strip()


def _extract_board_file_path_from_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    match = re.search(r"([A-Za-z]:[\\/][^\s\"'，,;；]+\.(?:kicad_pcb|txt)|/[^\s\"'，,;；]+\.(?:kicad_pcb|txt))", text)
    return match.group(1) if match else ""


def _nested_text_value(data: Dict[str, Any], *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_reroute_tool_result(raw: Any) -> Any:
    if isinstance(raw, dict) and "result" in raw:
        return _parse_reroute_tool_result(raw.get("result"))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"rawResult": raw}
    return raw


def _normalize_delete_for_rerouting_payload(
    raw_result: Any,
    *,
    user_text: str,
    project_id: str,
) -> Dict[str, Any]:
    parsed = _parse_reroute_tool_result(raw_result)
    if not isinstance(parsed, dict):
        parsed = {"rawResult": parsed}

    missing_routes = parsed.get("missing_routes") or parsed.get("missingRoutes") or []
    if not isinstance(missing_routes, list):
        missing_routes = []

    selected_nets = []
    for route in missing_routes:
        if isinstance(route, dict):
            net = str(route.get("net_name") or route.get("netName") or route.get("net") or "").strip()
            if net:
                selected_nets.append(net)
    if not selected_nets:
        for net in extract_reroute_nets(user_text):
            if net not in selected_nets:
                selected_nets.append(net)

    project_data = str(
        parsed.get("projectData")
        or parsed.get("project_data")
        or parsed.get("projectDataFilePath")
        or parsed.get("boardDataFilePath")
        or ""
    ).strip()
    dropped_board_data = ""
    dropped_board_path = ""
    if project_data:
        dropped_board_data, dropped_board_path = _read_board_file(project_data)
        if not dropped_board_data and ("\n" in project_data or "(" in project_data):
            dropped_board_data = project_data
            dropped_board_path = ""

    validation_error = _validate_delete_for_rerouting_payload(
        parsed=parsed,
        missing_routes=missing_routes,
        project_data=project_data,
        dropped_board_data=dropped_board_data,
        dropped_board_path=dropped_board_path,
    )
    frontend_error_value = parsed.get("error")
    error = str(
        parsed.get("message")
        or (frontend_error_value if isinstance(frontend_error_value, str) else "")
        or validation_error
        or ""
    ).strip()
    details = parsed.get("details")
    if isinstance(details, dict):
        skipped = details.get("skipped")
        if isinstance(skipped, list) and skipped:
            skipped_parts: list[str] = []
            for item in skipped[:8]:
                if not isinstance(item, dict):
                    continue
                net_name = str(item.get("netName") or item.get("net_name") or "").strip()
                reason = str(item.get("reason") or "").strip()
                if net_name and reason:
                    skipped_parts.append(f"{net_name}({reason})")
                elif net_name:
                    skipped_parts.append(net_name)
                elif reason:
                    skipped_parts.append(reason)
            if skipped_parts:
                error = f"{error}；跳过对象：{', '.join(skipped_parts)}" if error else f"跳过对象：{', '.join(skipped_parts)}"

    payload = {
        "selectedNets": selected_nets,
        "selectedTraceIds": [],
        "missingRoutes": missing_routes,
        "dropResult": parsed,
        "deleteResult": parsed,
        "droppedBoardDataFilePath": dropped_board_path or project_data,
        "originalBoardDataFilePath": dropped_board_path or project_data,
        "droppedBoardDataChars": len(dropped_board_data or ""),
        "droppedObjects": [
            {"net": net, "type": "missing_route", "deleted": True}
            for net in selected_nets
        ],
        "localContext": {
            "source": "deleteTracesForRerouting",
            "selectionCount": len(missing_routes),
            "selectedNetCount": len(selected_nets),
            "projectID": project_id,
            "missingRoutes": missing_routes,
        },
    }
    if error:
        payload["error"] = error
        payload["frontendError"] = {
            "code": 50001,
            "message": "Tool execution failed",
            "details": error,
        }
    return payload


def _validate_delete_for_rerouting_payload(
    *,
    parsed: Dict[str, Any],
    missing_routes: list[Any],
    project_data: str,
    dropped_board_data: str,
    dropped_board_path: str,
) -> str:
    if not missing_routes:
        return "未检测到框选走线，deleteTracesForRerouting 未返回 missing_routes。"
    if len(missing_routes) > 40:
        return f"框选走线数量超过 40Pin 限制：{len(missing_routes)}。"

    for index, route in enumerate(missing_routes, start=1):
        if not isinstance(route, dict):
            return f"missing_routes[{index}] 不是对象。"
        net_name = str(route.get("net_name") or route.get("netName") or route.get("net") or "").strip()
        if not net_name:
            return f"missing_routes[{index}] 缺少 net_name。"
        for endpoint_name in ("start", "end"):
            endpoint = route.get(endpoint_name)
            if not isinstance(endpoint, dict):
                return f"missing_routes[{index}].{endpoint_name} 缺少对象。"
            layer = str(endpoint.get("layer") or "").strip()
            if not layer:
                return f"missing_routes[{index}].{endpoint_name} 缺少 layer。"
            for coordinate in ("x", "y"):
                try:
                    float(endpoint.get(coordinate))
                except (TypeError, ValueError):
                    return f"missing_routes[{index}].{endpoint_name} 缺少有效 {coordinate} 坐标。"

    if not project_data:
        return "deleteTracesForRerouting 未返回 projectData。"
    if not dropped_board_data:
        return f"projectData 不可读：{project_data}"
    return ""


def delete_traces_for_rerouting(userText: str = "", projectID: str = "", session_id: Optional[str] = None) -> str:
    """Call the frontend one-shot reroute deletion tool and cache its returned reroute parameters."""
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("deleteTracesForRerouting", session_id)
        logger.warning(msg)
        return json.dumps({"selectedNets": [], "selectedTraceIds": [], "error": msg}, ensure_ascii=False)

    try:
        raw_result = _transport.call_tool_sync(
            tool_name="deleteTracesForRerouting",
            arguments={},
            timeout=120.0,
            session_id=session_id,
        )
        payload = _normalize_delete_for_rerouting_payload(raw_result, user_text=userText, project_id=projectID)
        if payload.get("error"):
            logger.warning("deleteTracesForRerouting returned unusable reroute context: %s", payload.get("error"))
            _transport.clear_reroute_context(session_id)
        else:
            _transport.cache_reroute_context(payload, session_id=session_id)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error("deleteTracesForRerouting failed: %s", e)
        return json.dumps({"selectedNets": [], "selectedTraceIds": [], "error": str(e)}, ensure_ascii=False)


def drop_net(userText: str = "", projectID: str = "", session_id: Optional[str] = None) -> str:
    """Backward-compatible alias for deleteTracesForRerouting."""
    return delete_traces_for_rerouting(userText=userText, projectID=projectID, session_id=session_id)


registry.register(
    name="deleteTracesForRerouting",
    toolset="pcb",
    schema={
        "name": "deleteTracesForRerouting",
        "description": (
            "Call the PCB frontend one-shot tool to delete the selected traces "
            "and return missing_routes plus projectData for local reroute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userText": {"type": "string", "description": "Original user reroute request text."},
                "projectID": {"type": "string", "description": "Current PCB project id."},
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: delete_traces_for_rerouting(
        args.get("userText", ""),
        args.get("projectID", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: True,
)


registry.register(
    name="drop_net",
    toolset="pcb",
    schema={
        "name": "drop_net",
        "description": (
            "Compatibility alias for deleteTracesForRerouting. "
            "Use deleteTracesForRerouting for the normal one-shot frontend delete/reroute-parameter flow."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userText": {"type": "string", "description": "Original user request."},
                "projectID": {"type": "string", "description": "Optional PCB project id."},
            },
            "required": ["userText"],
        },
    },
    handler=lambda args, **kwargs: drop_net(
        args.get("userText", ""),
        projectID=args.get("projectID", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: _transport.get_adapter() is not None,
)


def _build_fallback_reroute_payload(
    *,
    nets: list[str],
    dropped_board_data: str,
    dropped_board_path: str,
    dropped_objects: Any,
    local_context: Any,
    constraints: Any,
    check_report: Dict[str, Any],
    explanation_suffix: str = "",
    original_board_path: str = "",
    selected_trace_ids: list[str] | None = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    selected_trace_ids = selected_trace_ids or []
    reroute_result = {
        "type": "local_reroute",
        "mode": "selected_nets_after_drop" if nets else "selected_traces_after_delete",
        "selectedNets": nets,
        "selectedTraceIds": selected_trace_ids,
        "operations": [
            {"action": "reroute_net", "net": net, "scope": "local", "preserveOtherNets": True}
            for net in nets
        ] or [
            {
                "action": "reroute_selected_traces",
                "traceIds": selected_trace_ids,
                "scope": "local",
                "preserveOtherNets": True,
            }
        ],
        "constraints": constraints,
        "droppedObjects": dropped_objects,
        "localContext": local_context,
        "originalBoardDataFilePath": original_board_path,
        "droppedBoardDataFilePath": dropped_board_path,
        "droppedBoardDataChars": len(dropped_board_data or ""),
    }
    explanation = "已基于拆线结果生成局部重布结果包；本结果限定在所选走线或 selectedNets 范围内，其他网络默认保护。"
    if explanation_suffix:
        explanation = f"{explanation}{explanation_suffix}"
    return {"rerouteResult": reroute_result, "checkReport": check_report, "explanation": explanation}


def _append_explainability_report(
    payload: Dict[str, Any],
    *,
    internal_board_path: str,
    public_txt_path: str,
    import_lines_path: str = "",
) -> Dict[str, Any]:
    result = dict(payload)
    explain_report = ""
    try:
        explain_report = generate_explain_report(
            board_file_path=internal_board_path,
            reroute_result=(result.get("rerouteResult") or {}) if isinstance(result.get("rerouteResult"), dict) else {},
            check_report=(result.get("checkReport") or {}) if isinstance(result.get("checkReport"), dict) else {},
        )
    except Exception as exc:
        logger.warning("Local explain report generation failed: %s", exc)
        explain_report = f"可解释性报告生成失败：{_strip_internal_reroute_paths(str(exc))}"

    result["content"] = _compose_reroute_report_content(
        payload=result,
        public_txt_path=public_txt_path,
        import_lines_path=import_lines_path,
        explain_report=str(explain_report or ""),
    )
    return result


def _normalize_reroute_model_payload(
    model_payload: Dict[str, Any],
    *,
    fallback_payload: Dict[str, Any],
    context_stats: Dict[str, Any] | None,
) -> Dict[str, Any]:
    result = dict(fallback_payload)
    if context_stats and isinstance(result.get("rerouteResult"), dict):
        result["rerouteResult"] = {**result["rerouteResult"], "contextStats": context_stats}
    for source_key in ("kicadPatch", "kicad_patch", "rawModelOutput"):
        target_key = "kicadPatch" if source_key == "kicad_patch" else source_key
        value = model_payload.get(source_key)
        if isinstance(value, str) and value.strip():
            result[target_key] = value.strip()
    failure = str(model_payload.get("modelGenerationFailure") or "").strip()
    if failure and isinstance(result.get("rerouteResult"), dict):
        result["rerouteResult"] = {**result["rerouteResult"], "modelGenerationFailure": failure}
    return result


def _extract_balanced_sexpr_blocks(text: str, head: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    blocks: list[str] = []
    pattern = re.compile(rf"\(\s*{re.escape(head)}\b", re.IGNORECASE)
    for match in pattern.finditer(text):
        depth = 0
        in_string = False
        escaped = False
        for pos in range(match.start(), len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "(":
                depth += 1
                continue
            if char == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.start():pos + 1].strip())
                    break
    return blocks


def _extract_kicad_patch_from_model_text(text: str) -> str:
    blocks = []
    for head in ("segment", "via"):
        blocks.extend(_extract_balanced_sexpr_blocks(text, head))
    seen: set[str] = set()
    unique = []
    for block in blocks:
        if block in seen:
            continue
        seen.add(block)
        unique.append(block)
    return "\n".join(unique).strip()


def _summarize_reroute_model_failure(exc: Exception | str) -> str:
    raw = str(exc or "").strip()
    clean = str(_strip_internal_reroute_paths(raw)).strip()
    clean = re.sub(r"\s+", " ", clean)
    if len(clean) > 360:
        clean = clean[:360].rstrip() + "..."
    if "unable to parse JSON object" in clean or "segment" in clean.lower() or "via" in clean.lower():
        return (
            "模型输出中未找到合法的 `(segment ...)` 或 `(via ...)` 走线对象，"
            "因此无法回填版图，DRC 未执行，也不会生成 txt 或调用 importLines。"
            f"模型输出解析摘要：{clean}"
        )
    return (
        "模型未返回可回填的走线 patch，因此无法回填版图，DRC 未执行，"
        "也不会生成 txt 或调用 importLines。"
        f"模型调用/解析摘要：{clean}"
    )


def _collect_reroute_value_strings(value: Any, wanted_keys: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key or "").strip().lower().replace("-", "_")
            if normalized in wanted_keys:
                if isinstance(item, (list, tuple, set)):
                    found.extend(str(part).strip() for part in item if str(part).strip())
                elif str(item).strip():
                    found.append(str(item).strip())
            found.extend(_collect_reroute_value_strings(item, wanted_keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_reroute_value_strings(item, wanted_keys))
    return found


def _target_reroute_nets(nets: list[str], dropped_objects: Any, local_context: Any) -> tuple[list[str], list[str]]:
    name_keys = {"net", "netname", "net_name", "selectednet", "selected_nets", "selectednets"}
    id_keys = {"netid", "net_id", "netcode", "net_code"}
    names = list(nets or [])
    ids: list[str] = []
    for source in (dropped_objects, local_context):
        names.extend(_collect_reroute_value_strings(source, name_keys))
        ids.extend(_collect_reroute_value_strings(source, id_keys))

    clean_names: list[str] = []
    for item in names:
        text = str(item or "").strip().strip('"')
        if not text or text in clean_names:
            continue
        clean_names.append(text)

    clean_ids: list[str] = []
    for item in ids:
        match = re.search(r"\d+", str(item or ""))
        if not match:
            continue
        net_id = match.group(0)
        if net_id not in clean_ids:
            clean_ids.append(net_id)
    return clean_names, clean_ids


def _parse_reroute_net_declarations(board_text: str) -> tuple[dict[str, str], dict[str, str]]:
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    patterns = (
        r'\(\s*net\s+(\d+)\s+"([^"]*)"\s*\)',
        r'\(\s*net\s+(\d+)\s+([^\s\)]+)\s*\)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, board_text or "", flags=re.IGNORECASE):
            net_id, net_name = match.groups()
            net_name = _strip_kicad_atom(net_name)
            if not net_name:
                continue
            id_to_name.setdefault(net_id, net_name)
            name_to_id.setdefault(net_name, net_id)
    return id_to_name, name_to_id


def _coord_text(x: float, y: float) -> str:
    return f"({x:.2f}, {y:.2f})"


def _parse_float_pair(pattern: str, text: str) -> tuple[float, float] | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _parse_float_triplet(pattern: str, text: str) -> tuple[float, float, float] | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    third = match.group(3)
    return float(match.group(1)), float(match.group(2)), float(third) if third not in (None, "") else 0.0


def _strip_kicad_atom(value: str) -> str:
    return str(value or "").strip().strip('"')


def _module_pad_endpoints_for_nets(
    board_text: str,
    *,
    target_net_names: list[str],
    target_net_ids: list[str],
) -> list[dict[str, Any]]:
    id_to_name, name_to_id = _parse_reroute_net_declarations(board_text)
    target_ids = set(target_net_ids)
    target_names = set(target_net_names)
    for name in target_net_names:
        if name in name_to_id:
            target_ids.add(name_to_id[name])
    for net_id in target_net_ids:
        if net_id in id_to_name:
            target_names.add(id_to_name[net_id])
    if not target_ids and not target_names:
        return []

    endpoints: list[dict[str, Any]] = []
    for module in _extract_balanced_sexpr_blocks(board_text, "module"):
        module_head = re.search(r"\(\s*module\s+([^\s)]+)", module, flags=re.IGNORECASE)
        package = _strip_kicad_atom(module_head.group(1) if module_head else "module")
        module_layer_match = re.search(r"\(\s*layer\s+([^\s)]+)", module, flags=re.IGNORECASE)
        module_layer = _strip_kicad_atom(module_layer_match.group(1) if module_layer_match else "")
        first_pad_index = module.lower().find("( pad")
        module_header = module if first_pad_index < 0 else module[:first_pad_index]
        module_at = _parse_float_triplet(
            r"\(\s*at\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)(?:\s+(-?\d+(?:\.\d+)?))?",
            module_header,
        ) or (0.0, 0.0, 0.0)
        reference_match = re.search(r"\(\s*fp_text\s+reference\s+([^\s)]+)", module, flags=re.IGNORECASE)
        reference = _strip_kicad_atom(reference_match.group(1) if reference_match else package)
        for pad in _extract_balanced_sexpr_blocks(module, "pad"):
            net_match = re.search(r'\(\s*net\s+(\d+)(?:\s+"([^"]*)")?', pad, flags=re.IGNORECASE)
            if not net_match:
                continue
            net_id = net_match.group(1)
            net_name = net_match.group(2) or id_to_name.get(net_id, "")
            if net_id not in target_ids and net_name not in target_names:
                continue
            pad_match = re.search(r"\(\s*pad\s+([^\s)]+)", pad, flags=re.IGNORECASE)
            pad_name = _strip_kicad_atom(pad_match.group(1) if pad_match else "pad")
            pad_at = _parse_float_pair(r"\(\s*at\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", pad) or (0.0, 0.0)
            layers_match = re.search(r"\(\s*layers\s+([^)]+)\)", pad, flags=re.IGNORECASE)
            layer = module_layer
            if layers_match:
                for token in layers_match.group(1).split():
                    if token not in {"F.Paste", "F.Mask", "B.Paste", "B.Mask"}:
                        layer = _strip_kicad_atom(token)
                        break
            theta = math.radians(-module_at[2])
            local_y = -pad_at[1] if module_layer == "Bottom" else pad_at[1]
            pad_x = module_at[0] + pad_at[0] * math.cos(theta) - local_y * math.sin(theta)
            pad_y = module_at[1] + pad_at[0] * math.sin(theta) + local_y * math.cos(theta)
            endpoints.append(
                {
                    "netId": net_id,
                    "netName": net_name,
                    "component": reference,
                    "package": package,
                    "pad": pad_name,
                    "layer": layer,
                    "x": pad_x,
                    "y": pad_y,
                }
            )
    return endpoints


def _format_endpoint_for_prompt(endpoint: dict[str, Any]) -> str:
    component = str(endpoint.get("component") or "模块").strip()
    package = str(endpoint.get("package") or "module").strip()
    pad = str(endpoint.get("pad") or "").strip()
    layer = str(endpoint.get("layer") or "").strip() or "未知层"
    x = float(endpoint.get("x") or 0.0)
    y = float(endpoint.get("y") or 0.0)
    kind = "BGA" if "bga" in package.lower() else "模块"
    return f"{kind} {component}（{package}）的焊盘 {pad}，位于层 {layer}，坐标 {_coord_text(x, y)}"


def _frontend_missing_routes_from_context(local_context: Any) -> list[dict[str, Any]]:
    if not isinstance(local_context, dict):
        return []
    routes = local_context.get("missingRoutes") or local_context.get("missing_routes") or []
    if not isinstance(routes, list):
        return []
    return [route for route in routes if isinstance(route, dict)]


def _frontend_coord_to_internal_kicad(value: Any, *, axis: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    try:
        convert_mod = _load_convert_module()
        origin = float(getattr(convert_mod, "OUTLINE_ONLY_ORIGIN_X" if axis == "x" else "OUTLINE_ONLY_ORIGIN_Y"))
        dbu_mm = float(getattr(convert_mod, "DBU_MM"))
    except Exception:
        origin = 363386.0 if axis == "x" else 534646.0
        dbu_mm = 0.000254
    return (number * 100.0 + origin) * dbu_mm


def _endpoint_component_pad_key(endpoint: Any) -> tuple[str, str]:
    if not isinstance(endpoint, dict):
        return "", ""
    return str(endpoint.get("component") or "").strip(), str(endpoint.get("pad") or "").strip()


def _endpoint_from_internal_pad(
    endpoint: Any,
    *,
    pad_lookup: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    component, pad = _endpoint_component_pad_key(endpoint)
    if not component or not pad:
        return None
    internal = pad_lookup.get((component, pad))
    if not internal:
        return None
    result = dict(endpoint) if isinstance(endpoint, dict) else {}
    result["x"] = float(internal.get("x") or 0.0)
    result["y"] = float(internal.get("y") or 0.0)
    result["layer"] = str(internal.get("layer") or result.get("layer") or "")
    result["coordinateSource"] = "internal_pad_center"
    return result


def _normalize_frontend_endpoint_to_internal_kicad(
    endpoint: Any,
    *,
    pad_lookup: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    if not isinstance(endpoint, dict):
        return None, None, "missing_endpoint"
    frontend = dict(endpoint)
    internal = _endpoint_from_internal_pad(endpoint, pad_lookup=pad_lookup)
    if internal is not None:
        return internal, frontend, "internal_pad_center"
    x = _frontend_coord_to_internal_kicad(endpoint.get("x"), axis="x")
    y = _frontend_coord_to_internal_kicad(endpoint.get("y"), axis="y")
    if x is None or y is None:
        return dict(endpoint), frontend, "unconverted"
    internal = dict(endpoint)
    internal["x"] = x
    internal["y"] = y
    internal["coordinateSource"] = "frontend_txt_dbu_to_internal_kicad"
    return internal, frontend, "frontend_txt_dbu_to_internal_kicad"


def _normalize_reroute_local_context_coordinates(
    local_context: Any,
    *,
    board_text: str,
    nets: list[str],
    dropped_objects: Any,
) -> tuple[Any, dict[str, Any]]:
    if not isinstance(local_context, dict):
        return local_context, {"normalized": False, "reason": "local_context_not_dict"}
    routes = _frontend_missing_routes_from_context(local_context)
    if not routes:
        return local_context, {"normalized": False, "missingRouteCount": 0}
    target_net_names, target_net_ids = _target_reroute_nets(nets, dropped_objects, local_context)
    endpoints = _module_pad_endpoints_for_nets(
        board_text,
        target_net_names=target_net_names,
        target_net_ids=target_net_ids,
    )
    pad_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for endpoint in endpoints:
        component = str(endpoint.get("component") or "").strip()
        pad = str(endpoint.get("pad") or "").strip()
        if component and pad:
            pad_lookup[(component, pad)] = endpoint

    normalized_routes = []
    conversions = []
    for route in routes:
        new_route = dict(route)
        for key in ("start", "end"):
            internal, frontend, source = _normalize_frontend_endpoint_to_internal_kicad(
                route.get(key),
                pad_lookup=pad_lookup,
            )
            if frontend is not None:
                new_route[f"frontend{key.capitalize()}"] = frontend
            if internal is not None:
                new_route[key] = internal
            conversions.append(
                {
                    "net": route.get("net_name") or route.get("netName") or route.get("net"),
                    "endpoint": key,
                    "source": source,
                    "frontend": frontend,
                    "internal": internal,
                }
            )
        normalized_routes.append(new_route)

    normalized_context = dict(local_context)
    normalized_context["missingRoutes"] = normalized_routes
    normalized_context["coordinateSystem"] = "internal_kicad_mm"
    normalized_context["frontendCoordinateSystem"] = "pcb_builder_txt_mils"
    stats = {
        "normalized": True,
        "missingRouteCount": len(normalized_routes),
        "padEndpointCount": len(endpoints),
        "conversions": conversions,
    }
    return normalized_context, stats


def _format_frontend_route_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, dict):
        return "未知端点"
    component = str(endpoint.get("component") or "").strip()
    pad = str(endpoint.get("pad") or "").strip()
    layer = str(endpoint.get("layer") or "").strip() or "未知层"
    try:
        x = float(endpoint.get("x"))
        y = float(endpoint.get("y"))
        coord = _coord_text(x, y)
    except (TypeError, ValueError):
        coord = "未知坐标"
    owner = ""
    if component and pad:
        owner = f"{component}.{pad}，"
    elif component:
        owner = f"{component}，"
    elif pad:
        owner = f"pad {pad}，"
    return f"{owner}层 {layer}，坐标 {coord}"


def _build_missing_route_description(
    *,
    board_text: str,
    nets: list[str],
    dropped_objects: Any,
    local_context: Any,
    selected_trace_ids: list[str] | None,
) -> tuple[str, dict[str, Any]]:
    target_net_names, target_net_ids = _target_reroute_nets(nets, dropped_objects, local_context)
    endpoints = _module_pad_endpoints_for_nets(
        board_text,
        target_net_names=target_net_names,
        target_net_ids=target_net_ids,
    )
    endpoints_by_net: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        key = (str(endpoint.get("netId") or ""), str(endpoint.get("netName") or ""))
        endpoints_by_net.setdefault(key, []).append(endpoint)

    lines: list[str] = []
    route_index = 1
    net_name_to_id = _parse_kicad_net_name_to_id(board_text)
    frontend_routes = _frontend_missing_routes_from_context(local_context)
    for route in frontend_routes:
        net_name = str(route.get("net_name") or route.get("netName") or route.get("net") or "").strip()
        if not net_name:
            continue
        start_text = _format_frontend_route_endpoint(route.get("start"))
        end_text = _format_frontend_route_endpoint(route.get("end"))
        frontend_start_text = _format_frontend_route_endpoint(route.get("frontendStart") or route.get("start"))
        frontend_end_text = _format_frontend_route_endpoint(route.get("frontendEnd") or route.get("end"))
        net_id_text = f"，KiCad net id 必须使用 {net_name_to_id[net_name]}" if net_name in net_name_to_id else ""
        lines.append(f"{route_index}. 网络 {net_name}{net_id_text}")
        route_index += 1
        lines.append(
            "前端删除线段: "
            f"deleteTracesForRerouting 返回该网络缺失连接，前端原始起点为 {frontend_start_text}，"
            f"前端原始终点为 {frontend_end_text}。"
            f"agent 已将其归一化到内部 KiCad 坐标：起点 {start_text}，终点 {end_text}。"
            "生成重布 patch 时必须只使用内部 KiCad 坐标连接这两个端点；"
            "不得改变坐标正负号，不得只连接到附近坐标；"
            "前端原始坐标仅用于展示和排查，禁止直接作为 KiCad patch 坐标。"
        )

    if endpoints_by_net:
        for (net_id, net_name), net_endpoints in endpoints_by_net.items():
            label = f"{net_id}({net_name})" if net_id and net_name else net_name or net_id
            lines.append(f"{route_index}. 网络 {label}")
            route_index += 1
            if len(net_endpoints) >= 2:
                first, second = net_endpoints[0], net_endpoints[1]
                lines.append(
                    "缺失详情: "
                    f"{net_id or label}（{net_name or label}）存在走线缺失，走线连接的一端为 "
                    f"{_format_endpoint_for_prompt(first)}，走线另一端为 {_format_endpoint_for_prompt(second)}"
                )
                if len(net_endpoints) > 2:
                    extra = "；其它同网端点：" + "；".join(
                        _format_endpoint_for_prompt(item) for item in net_endpoints[2:6]
                    )
                    lines[-1] += extra
            else:
                lines.append(
                    "缺失详情: "
                    f"{net_id or label}（{net_name or label}）存在走线缺失，已识别端点："
                    + "；".join(_format_endpoint_for_prompt(item) for item in net_endpoints)
                )

    if not lines:
        selected = ", ".join(selected_trace_ids or []) or "未提供"
        target_names = ", ".join(target_net_names or target_net_ids or []) or "未知网络"
        lines = [
            f"1. 网络 {target_names}",
            f"缺失详情: 前端已框选并删除 trace ids: {selected}，请根据上下文补全同网局部走线。",
        ]

    stats = {
        "targetNetNames": target_net_names,
        "targetNetIds": target_net_ids,
        "endpointCount": len(endpoints),
        "selectedTraceIds": selected_trace_ids or [],
        "frontendMissingRouteCount": len(frontend_routes),
    }
    return "\n".join(lines).strip(), stats


def _chunk_text_for_reroute(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    if max_chars <= 0:
        return [text]
    overlap_chars = max(0, min(overlap_chars, max_chars - 1))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap_chars
    return chunks


def _tokenize_reroute_query(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?", str(text or "").lower())


def _build_single_shot_reroute_context(
    *,
    board_text: str,
    task_description: str,
    selected_trace_ids: list[str] | None,
    nets: list[str],
) -> dict[str, Any]:
    try:
        chunk_chars = int(os.getenv("PCB_REROUTE_SINGLE_SHOT_CHUNK_CHARS", "1600"))
    except ValueError:
        chunk_chars = 1600
    try:
        overlap_chars = int(os.getenv("PCB_REROUTE_SINGLE_SHOT_OVERLAP_CHARS", "600"))
    except ValueError:
        overlap_chars = 600
    try:
        retrieve_k = int(os.getenv("PCB_REROUTE_SINGLE_SHOT_RETRIEVE_K", "2"))
    except ValueError:
        retrieve_k = 2
    chunk_chars = max(512, min(6000, chunk_chars))
    retrieve_k = max(1, min(8, retrieve_k))

    chunks = _chunk_text_for_reroute(board_text or "", max_chars=chunk_chars, overlap_chars=overlap_chars)
    query_tokens = set(_tokenize_reroute_query("\n".join([task_description, " ".join(selected_trace_ids or []), " ".join(nets or [])])))
    scored: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        tokens = _tokenize_reroute_query(chunk)
        score = sum(1 for token in tokens if token in query_tokens) if query_tokens else 0
        scored.append((score, index, chunk))
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    selected = [(index, chunk) for score, index, chunk in scored[:retrieve_k] if score > 0]
    if not selected:
        selected = [(index, chunk) for _score, index, chunk in scored[:retrieve_k]]
    context_text = "\n".join(f"[片段 {index + 1}]\n{chunk}" for index, chunk in selected)
    return {
        "contextText": context_text,
        "stats": {
            "strategy": "cot_plan_single_shot",
            "chunkCount": len(chunks),
            "chunkChars": chunk_chars,
            "overlapChars": overlap_chars,
            "retrievedSegmentCount": len(selected),
            "retrievedSegmentIndexes": [index for index, _chunk in selected],
            "contextChars": len(context_text),
        },
    }


def _format_drc_iteration_history_for_prompt(history: list[Dict[str, Any]]) -> str:
    if not history:
        return "无"
    return "\n\n".join(
        json.dumps(
            {
                "iteration": item.get("iteration"),
                "passed": item.get("passed"),
                "kicadPatch": item.get("kicadPatch") or "",
                "failureSummary": item.get("failureSummary") or "",
                "drcResult": item.get("drcResult") or {},
            },
            ensure_ascii=False,
            indent=2,
        )
        for item in history
    )


def _drc_feedback_for_prompt(attempt: Any) -> str:
    summary = str(getattr(attempt, "failure_summary", "") or "").strip()
    drc_result = getattr(attempt, "drc_result", {}) or {}
    fill_detail = getattr(attempt, "fill_detail", {}) or {}
    parts = [summary] if summary else []
    if isinstance(drc_result, dict):
        details = drc_result.get("details") or {}
        hard_count = details.get("hard_issue_count")
        if hard_count is not None:
            parts.append(f"硬 DRC 问题数量：{hard_count}")
        rule_counts = details.get("hard_rule_counts") or {}
        if rule_counts:
            parts.append(f"规则计数：{json.dumps(rule_counts, ensure_ascii=False)}")
        issues = (drc_result.get("artifacts") or {}).get("issues") or []
        compact_issues = []
        for issue in issues[:8]:
            if isinstance(issue, dict):
                compact_issues.append(
                    {
                        "rule": issue.get("rule"),
                        "message": issue.get("message") or issue.get("description"),
                        "severity": issue.get("severity"),
                    }
                )
            else:
                compact_issues.append(str(issue))
        if compact_issues:
            parts.append(f"前几条 DRC issue：{json.dumps(compact_issues, ensure_ascii=False)}")
    if isinstance(fill_detail, dict) and fill_detail:
        parts.append(f"patch 回填统计：{json.dumps(fill_detail, ensure_ascii=False)}")
    parts.append("下一轮必须避开上述冲突/间距/短路问题，优先调整走线坐标或换层，不能原样重复上一轮 patch。")
    return "\n".join(part for part in parts if part)


def _build_reroute_generation_prompts(
    *,
    nets: list[str],
    dropped_board_path: str,
    dropped_objects: Any,
    local_context: Any,
    constraints: Any,
    original_board_path: str,
    context_text: str,
    context_stats: Dict[str, Any],
    task_description: str = "",
    task_id: str = "local-reroute",
    board_id: str = "frontend-selected-board",
    drc_feedback: list[str] | None = None,
    drc_iteration_history: list[Dict[str, Any]] | None = None,
    selected_trace_ids: list[str] | None = None,
) -> Dict[str, str]:
    system_prompt = (
        "你是资深 PCB 布线工程师和 KiCad 代码生成器。只输出合法 KiCad 走线对象，不要输出推理过程。"
    )
    feedback_text = ""
    if drc_feedback or drc_iteration_history:
        feedback_text = (
            "\n上一轮 DRC 反馈：\n"
            "历史 DRC 迭代:\n"
            f"{_format_drc_iteration_history_for_prompt(drc_iteration_history or [])}\n\n"
            "DRC 失败反馈（下一轮必须据此修改走线，不要重复失败 patch）:\n"
            f"{json.dumps(drc_feedback or [], ensure_ascii=False, indent=2)}\n\n"
        )
    user_prompt = (
        "/no_think\n"
        "你是一个 PCB 逃逸布线智能体。请根据 PCB 上下文和缺失走线描述，"
        "只生成补全缺失网络所需的 KiCad 走线对象。\n"
        "只允许输出纯文本形式的 (segment ...) 和必要时的 (via ...) 对象。\n"
        "不要输出 Markdown、代码块、解释、分析过程或 <think> 内容。\n\n"
        f"任务 ID：{task_id}\n"
        f"板卡 ID：{board_id}\n"
        "缺失走线描述：\n"
        f"{task_description.strip() or '前端已框选并删除局部走线，请根据上下文补全同网缺失连接。'}\n\n"
        "相关 KiCad 上下文片段：\n"
        f"{context_text}\n\n"
        f"{feedback_text}"
        "布线约束：\n"
        "- 必须保留 PCB 上下文中的 net id、层名、线宽和坐标含义。\n"
        "- 如果缺失走线描述中包含内部 KiCad start/end 端点，输出的 segment/via 必须精确连接这些端点。\n"
        "- 缺失走线描述中的前端原始坐标只用于展示，禁止直接作为 KiCad patch 坐标。\n"
        "- 优先短的曼哈顿或近似曼哈顿路径，避免不同网络在同一铜层交叉。\n"
        "- 避免不同网络在同一铜层发生交叉。\n"
        "- 除非必须换层，否则尽量不要使用 via。\n"
        "- 最终只输出缺失走线对象，不要输出其它内容。\n\n"
        "必须使用下面这种 KiCad 语法格式：\n"
        "(segment (start 47.300000 62.300000) (end 47.300000 68.300000) "
        "(width 0.152400) (layer Top) (net 73))\n"
        "(via (at 47.300000 62.300000) (size 0.457200) (drill 0.203200) "
        "(layers Top In1.Cu) (net 73))"
    )
    return {"system": system_prompt, "user": user_prompt}


def _generate_reroute_with_model(
    *,
    nets: list[str],
    dropped_board_data: str,
    dropped_board_path: str,
    dropped_objects: Any,
    local_context: Any,
    constraints: Any,
    check_report: Dict[str, Any],
    original_board_path: str = "",
    drc_feedback: list[str] | None = None,
    drc_iteration_history: list[Dict[str, Any]] | None = None,
    selected_trace_ids: list[str] | None = None,
    debug_output_dir: str = "",
    debug_label: str = "initial",
    session_id: str = "session",
) -> Dict[str, Any]:
    fallback_payload = _build_fallback_reroute_payload(
        nets=nets,
        selected_trace_ids=selected_trace_ids,
        dropped_board_data=dropped_board_data,
        dropped_board_path=dropped_board_path,
        dropped_objects=dropped_objects,
        local_context=local_context,
        constraints=constraints,
        check_report=check_report,
        original_board_path=original_board_path,
    )
    if not dropped_board_data:
        return fallback_payload

    try:
        from tools import pcb_chunking_tool as chunking

        runtime = pcb_model_runtime.resolve_model_runtime(
            pcb_model_runtime.STAGE_REROUTE,
            require_api_key=True,
        )
        adapter_factory = getattr(chunking, "_make_openai_compatible_chat_adapter", None)
        if adapter_factory is None:
            adapter_factory = chunking._OpenAICompatibleChatAdapter
        adapter = adapter_factory(
            base_url=runtime["base_url"],
            model=runtime["model"],
            api_key=runtime["api_key"],
            timeout_s=_get_reroute_model_timeout_seconds(),
        )
        task_description, task_stats = _build_missing_route_description(
            board_text=dropped_board_data,
            nets=nets,
            dropped_objects=dropped_objects,
            local_context=local_context,
            selected_trace_ids=selected_trace_ids,
        )
        context_result = _build_single_shot_reroute_context(
            board_text=dropped_board_data,
            task_description=task_description,
            selected_trace_ids=selected_trace_ids,
            nets=nets,
        )
        context_stats = {**(context_result.get("stats") or {}), **task_stats}
        prompts = _build_reroute_generation_prompts(
            nets=nets,
            selected_trace_ids=selected_trace_ids,
            dropped_board_path=dropped_board_path,
            dropped_objects=dropped_objects,
            local_context=local_context,
            constraints=constraints,
            original_board_path=original_board_path,
            context_text=context_result["contextText"],
            context_stats=context_stats,
            task_description=task_description,
            task_id=session_id or "local-reroute",
            board_id="frontend-selected-board",
            drc_feedback=drc_feedback,
            drc_iteration_history=drc_iteration_history,
        )
        prompt_bundle_cls = getattr(chunking, "_PromptBundle", None)
        if prompt_bundle_cls is not None:
            prompt_bundle = prompt_bundle_cls(system=prompts["system"], user=prompts["user"])
        else:
            prompt_bundle = SimpleNamespace(system=prompts["system"], user=prompts["user"])
        raw_text, _model_meta = adapter.generate(
            prompt_bundle,
            SimpleNamespace(
                max_new_tokens=_get_reroute_model_max_tokens(),
                temperature=_get_reroute_model_temperature(),
                top_p=_get_reroute_model_top_p(),
            ),
        )
        raw_output_path = _write_reroute_debug_artifact(
            output_dir=debug_output_dir,
            session_id=session_id,
            label=f"{debug_label}_model_raw",
            content=raw_text,
        )
        try:
            model_payload = chunking._extract_first_json_object(raw_text)
        except Exception:
            extracted_patch = _extract_kicad_patch_from_model_text(raw_text)
            if not extracted_patch:
                raise
            model_payload = {
                "kicadPatch": extracted_patch,
            }
        else:
            if not _model_patch_text(model_payload):
                extracted_patch = _extract_kicad_patch_from_model_text(raw_text)
                if extracted_patch:
                    model_payload["kicadPatch"] = extracted_patch
        patch_text = _model_patch_text(model_payload)
        if not patch_text:
            model_payload["modelGenerationFailure"] = _summarize_reroute_model_failure(
                "模型响应里没有可解析的 `(segment ...)` 或 `(via ...)` 走线对象。"
            )
        patch_output_path = _write_reroute_debug_artifact(
            output_dir=debug_output_dir,
            session_id=session_id,
            label=f"{debug_label}_extracted_patch",
            content=patch_text,
        )
        model_payload.setdefault("rawModelOutput", raw_text)
        if raw_output_path:
            model_payload.setdefault("modelRawOutputFilePath", raw_output_path)
        if patch_output_path:
            model_payload.setdefault("extractedPatchFilePath", patch_output_path)
        return _normalize_reroute_model_payload(
            model_payload,
            fallback_payload=fallback_payload,
            context_stats=context_stats,
        )
    except Exception as exc:
        logger.warning("reroute model generation failed; using fallback payload: %s", exc)
        failure_summary = _summarize_reroute_model_failure(exc)
        payload = _build_fallback_reroute_payload(
            nets=nets,
            selected_trace_ids=selected_trace_ids,
            dropped_board_data=dropped_board_data,
            dropped_board_path=dropped_board_path,
            dropped_objects=dropped_objects,
            local_context=local_context,
            constraints=constraints,
            check_report=check_report,
            original_board_path=original_board_path,
            explanation_suffix=f" {failure_summary}",
        )
        reroute_result = dict(payload.get("rerouteResult") or {})
        reroute_result["modelGenerationFailure"] = failure_summary
        payload["rerouteResult"] = reroute_result
        return payload


def _get_max_drc_iterations(user_data_obj: Dict[str, Any]) -> int:
    raw = None
    for key in ("maxDrcIterations", "max_drc_iterations"):
        if key in user_data_obj and user_data_obj.get(key) not in (None, ""):
            raw = user_data_obj.get(key)
            break
    if raw is None:
        env_value = os.getenv("PCB_REROUTE_MAX_DRC_ITERATIONS")
        raw = env_value if env_value not in (None, "") else 3
    try:
        return max(0, min(20, int(raw)))
    except (TypeError, ValueError):
        return 3


def _get_reroute_model_max_tokens() -> int:
    raw = os.getenv("PCB_REROUTE_MAX_TOKENS", "").strip()
    if not raw:
        parser = pcb_model_runtime._load_project_config_ini()
        if parser is not None:
            for section in ("reroute-model", "reroute_model", "reroute"):
                if not parser.has_section(section):
                    continue
                raw = (
                    parser.get(section, "max_tokens", fallback="").strip()
                    or parser.get(section, "max_new_tokens", fallback="").strip()
                )
                if raw:
                    break
    try:
        value = int(raw) if raw else 2048
    except ValueError:
        value = 2048
    return max(512, min(8192, value))


def _get_reroute_model_timeout_seconds() -> float:
    raw = (
        os.getenv("CTYUN_REROUTE_TIMEOUT", "").strip()
        or os.getenv("PCB_REROUTE_TIMEOUT", "").strip()
        or "600"
    )
    try:
        value = float(raw)
    except ValueError:
        value = 600.0
    return max(60.0, min(1800.0, value))


def _get_reroute_model_temperature() -> float:
    raw = os.getenv("PCB_REROUTE_TEMPERATURE", "").strip()
    try:
        return float(raw) if raw else 0.7
    except ValueError:
        return 0.7


def _get_reroute_model_top_p() -> float:
    raw = os.getenv("PCB_REROUTE_TOP_P", "").strip()
    try:
        return float(raw) if raw else 0.9
    except ValueError:
        return 0.9


def _resolve_reroute_output_dir(user_data_obj: Dict[str, Any], original_board_path: str, session_id: str) -> str:
    explicit = user_data_obj.get("routedBoardOutputDir") or user_data_obj.get("outputDir")
    if explicit:
        return str(Path(str(explicit)).expanduser())
    return str(Path(tempfile.gettempdir()) / "hermes_pcb_reroute" / (session_id or "session"))


def _model_patch_text(payload: Dict[str, Any]) -> str:
    for key in ("kicadPatch", "kicad_patch", "patchText"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_kicad_net_name_to_id(board_text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not isinstance(board_text, str) or not board_text:
        return mapping
    quoted = re.compile(r"\(\s*net\s+(\d+)\s+\"([^\"]+)\"\s*\)", re.IGNORECASE)
    bare = re.compile(r"\(\s*net\s+(\d+)\s+([^\s\)]+)\s*\)", re.IGNORECASE)
    for pattern in (quoted, bare):
        for match in pattern.finditer(board_text):
            net_id = match.group(1).strip()
            net_name = match.group(2).strip()
            if net_name and net_name not in mapping:
                mapping[net_name] = net_id
    return mapping


def _patch_block_points(block: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for match in re.finditer(r"\(\s*(?:start|end|at)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", block, re.IGNORECASE):
        try:
            points.append((float(match.group(1)), float(match.group(2))))
        except ValueError:
            continue
    return points


def _route_endpoint_points(route: dict[str, Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for key in ("start", "end"):
        endpoint = route.get(key)
        if not isinstance(endpoint, dict):
            continue
        try:
            points.append((float(endpoint.get("x")), float(endpoint.get("y"))))
        except (TypeError, ValueError):
            continue
    return points


def _squared_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _replace_patch_block_net_id(block: str, net_id: str) -> str:
    if re.search(r"\(\s*net\s+\d+\s*\)", block, re.IGNORECASE):
        return re.sub(r"\(\s*net\s+\d+\s*\)", f"(net {net_id})", block, count=1, flags=re.IGNORECASE)
    insert_at = max(block.rfind(")"), 0)
    return f"{block[:insert_at].rstrip()} (net {net_id}){block[insert_at:]}"


def _format_coord_pair(point: tuple[float, float]) -> str:
    return f"{point[0]:.6f}, {point[1]:.6f}"


def _single_missing_route_for_endpoint_guard(
    *,
    selected_nets: list[str],
    local_context: Any,
) -> dict[str, Any] | None:
    routes = _frontend_missing_routes_from_context(local_context)
    selected = [str(net).strip() for net in selected_nets if str(net).strip()]
    if len(selected) != 1 or len(routes) != 1:
        return None
    route = routes[0]
    net_name = str(route.get("net_name") or route.get("netName") or route.get("net") or "").strip()
    if net_name and net_name != selected[0]:
        return None
    if len(_route_endpoint_points(route)) < 2:
        return None
    return route


def _endpoint_guard_messages(
    patch_text: str,
    *,
    selected_nets: list[str],
    local_context: Any,
    tolerance: float = 0.05,
) -> list[str]:
    route = _single_missing_route_for_endpoint_guard(selected_nets=selected_nets, local_context=local_context)
    if not route:
        return []
    expected_points = _route_endpoint_points(route)
    patch_points: list[tuple[float, float]] = []
    for block in _extract_balanced_sexpr_blocks(patch_text, "segment"):
        patch_points.extend(_patch_block_points(block))
    if not patch_points:
        return ["模型 patch 未输出 segment 端点，无法确认是否连接前端 missing_routes 起终点。"]

    messages: list[str] = []
    tolerance_sq = tolerance * tolerance
    for label, expected in zip(("start", "end"), expected_points[:2]):
        if not any(_squared_distance(point, expected) <= tolerance_sq for point in patch_points):
            nearest = min(patch_points, key=lambda point: _squared_distance(point, expected))
            messages.append(
                f"模型 patch 未命中前端 {label} 端点 {_format_coord_pair(expected)}，"
                f"最近模型端点为 {_format_coord_pair(nearest)}。"
            )
    return messages


def _correct_single_missing_route_segment_endpoints(
    patch_text: str,
    *,
    selected_nets: list[str],
    local_context: Any,
) -> tuple[str, list[str]]:
    route = _single_missing_route_for_endpoint_guard(selected_nets=selected_nets, local_context=local_context)
    if not route:
        return patch_text, []
    points = _route_endpoint_points(route)
    if len(points) < 2:
        return patch_text, []
    segments = _extract_balanced_sexpr_blocks(patch_text, "segment")
    if not segments:
        return patch_text, []
    start, end = points[0], points[1]
    original = segments[0]

    def _replace_point(block: str, key: str, point: tuple[float, float]) -> str:
        return re.sub(
            rf"\(\s*{key}\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s*\)",
            f"({key} {point[0]:.6f} {point[1]:.6f})",
            block,
            count=1,
            flags=re.IGNORECASE,
        )

    corrected = _replace_point(original, "start", start)
    corrected = _replace_point(corrected, "end", end)
    if corrected == original:
        return patch_text, []
    fixed_text = patch_text.replace(original, corrected, 1)
    return fixed_text, [
        "已将模型 patch 的首段端点硬修正为前端 missing_routes："
        f"start {_format_coord_pair(start)}，end {_format_coord_pair(end)}。"
    ]


def _bind_reroute_patch_nets(
    patch_text: str,
    *,
    board_text: str,
    selected_nets: list[str],
    local_context: Any,
) -> tuple[str, list[str], list[str]]:
    patch_text = str(patch_text or "").strip()
    if not patch_text:
        return "", [], ["模型未生成可回填的重布 patch。"]

    net_name_to_id = _parse_kicad_net_name_to_id(board_text)
    if not net_name_to_id:
        return patch_text, [], []
    selected = [str(net).strip() for net in selected_nets if str(net).strip()]
    selected_net_ids = {net_name_to_id[net] for net in selected if net in net_name_to_id}
    missing_names = [net for net in selected if net not in net_name_to_id]
    if missing_names:
        return patch_text, [], [f"当前板图中找不到 selected net 的 KiCad net id：{', '.join(missing_names)}"]
    if not selected_net_ids:
        return patch_text, [], []

    blocks = []
    for head in ("segment", "via"):
        blocks.extend(_extract_balanced_sexpr_blocks(patch_text, head))
    if not blocks:
        return patch_text, [], ["模型输出中未找到合法的 `(segment ...)` 或 `(via ...)` 走线对象。"]

    warnings: list[str] = []
    route_candidates: list[tuple[str, list[tuple[float, float]]]] = []
    for route in _frontend_missing_routes_from_context(local_context):
        net_name = str(route.get("net_name") or route.get("netName") or route.get("net") or "").strip()
        net_id = net_name_to_id.get(net_name)
        points = _route_endpoint_points(route)
        if net_id and points:
            route_candidates.append((net_id, points))

    replacements: dict[str, str] = {}
    if len(selected_net_ids) == 1:
        target_id = next(iter(selected_net_ids))
        for block in blocks:
            fixed = _replace_patch_block_net_id(block, target_id)
            if fixed != block:
                warnings.append(f"已将模型 patch 的 net id 绑定为 selected net id {target_id}。")
            replacements[block] = fixed
    else:
        for block in blocks:
            block_points = _patch_block_points(block)
            target_id = ""
            best_distance = float("inf")
            for candidate_id, candidate_points in route_candidates:
                for block_point in block_points:
                    for route_point in candidate_points:
                        distance = _squared_distance(block_point, route_point)
                        if distance < best_distance:
                            best_distance = distance
                            target_id = candidate_id
            if target_id:
                fixed = _replace_patch_block_net_id(block, target_id)
                if fixed != block:
                    warnings.append(f"已按 missing_routes 端点将模型 patch 绑定到 net id {target_id}。")
                replacements[block] = fixed
            else:
                replacements[block] = block

    fixed_text = patch_text
    for old, new in replacements.items():
        fixed_text = fixed_text.replace(old, new, 1)

    invalid_ids = []
    for match in re.finditer(r"\(\s*net\s+(\d+)\s*\)", fixed_text, re.IGNORECASE):
        net_id = match.group(1).strip()
        if net_id not in selected_net_ids:
            invalid_ids.append(net_id)
    if invalid_ids:
        return fixed_text, warnings, [
            "模型 patch 含有不属于 selectedNets 的 net id："
            f"{', '.join(sorted(set(invalid_ids)))}；允许的 net id：{', '.join(sorted(selected_net_ids))}"
        ]
    return fixed_text, warnings, []


def _drc_issue_text(issue: Any) -> str:
    if isinstance(issue, dict):
        return " ".join(
            str(issue.get(key) or "")
            for key in ("rule", "message", "description", "net", "obj1", "obj2")
        )
    return str(issue or "")


def _drc_issue_is_selected_net_related(issue: Any, selected_nets: list[str]) -> bool:
    text = _drc_issue_text(issue)
    lowered = text.lower()
    for net in selected_nets:
        clean = str(net or "").strip()
        if clean and clean.lower() in lowered:
            return True
    return False


def _local_reroute_drc_passes(attempt: Any, *, selected_nets: list[str]) -> tuple[bool, str, dict[str, Any]]:
    drc_result = getattr(attempt, "drc_result", {}) or {}
    if bool(drc_result.get("ok")) and bool(drc_result.get("pass")):
        return True, "global DRC passed", {"mode": "global_pass"}
    issues = (drc_result.get("artifacts") or {}).get("issues") if isinstance(drc_result, dict) else []
    issues = issues if isinstance(issues, list) else []
    selected = [str(net).strip() for net in selected_nets if str(net).strip()]
    blocking: list[Any] = []
    inherited: list[Any] = []
    for issue in issues:
        rule = str(issue.get("rule") if isinstance(issue, dict) else "").strip().upper()
        related = _drc_issue_is_selected_net_related(issue, selected)
        if rule == "HR_DRC_SEGMENT_CROSSING":
            blocking.append(issue)
        elif related:
            blocking.append(issue)
        else:
            inherited.append(issue)
    if blocking:
        compact = [
            {
                "rule": item.get("rule"),
                "message": item.get("message") or item.get("description"),
                "severity": item.get("severity"),
            }
            if isinstance(item, dict)
            else str(item)
            for item in blocking[:5]
        ]
        return (
            False,
            f"selected net local DRC failed: {json.dumps(compact, ensure_ascii=False)}",
            {
                "mode": "selected_net_local",
                "blockingIssueCount": len(blocking),
                "inheritedIssueCount": len(inherited),
                "blockingIssues": compact,
            },
        )
    if issues:
        return (
            True,
            f"local DRC passed; ignored {len(inherited)} inherited global issue(s) outside selected nets",
            {
                "mode": "selected_net_local",
                "blockingIssueCount": 0,
                "inheritedIssueCount": len(inherited),
            },
        )
    return False, getattr(attempt, "failure_summary", "") or "DRC failed without issues.", {"mode": "selected_net_local"}


def _append_reroute_check(
    payload: Dict[str, Any],
    *,
    name: str,
    passed: bool,
    detail: str,
) -> Dict[str, Any]:
    result = dict(payload)
    check_report = dict(result.get("checkReport") or {})
    checks = list(check_report.get("checks") or [])
    checks.append({"name": name, "passed": bool(passed), "detail": detail})
    check_report["checks"] = checks
    check_report["passed"] = bool(check_report.get("passed", True)) and bool(passed)
    result["checkReport"] = check_report
    if not passed and detail:
        explanation = str(result.get("explanation") or "").strip()
        result["explanation"] = f"{explanation} {detail}".strip() if explanation else detail
    return result


def _apply_drc_validation_to_payload(payload: Dict[str, Any], *, validation: Any, original_board_path: str) -> Dict[str, Any]:
    result = dict(payload)
    reroute_result = dict(result.get("rerouteResult") or {})
    attempts = [
        {
            "iteration": attempt.iteration,
            "passed": attempt.passed,
            "filledBoardDataFilePath": attempt.filled_board_data_file_path,
            "fillDetail": attempt.fill_detail,
            "drcResult": attempt.drc_result,
            "failureSummary": attempt.failure_summary,
        }
        for attempt in validation.attempts
    ]
    reroute_result["drcPassed"] = validation.passed
    reroute_result["drcIterations"] = len(validation.attempts)
    reroute_result["drcAttempts"] = attempts
    reroute_result["originalBoardDataFilePath"] = original_board_path
    if validation.passed:
        reroute_result["routedBoardDataFilePath"] = validation.routed_board_data_file_path
        result["routedBoardDataFilePath"] = validation.routed_board_data_file_path
        if attempts:
            local_policy = (attempts[-1].get("drcResult") or {}).get("localReroutePolicy") or {}
            if local_policy:
                reroute_result["localReroutePolicy"] = local_policy
    else:
        reroute_result["routedBoardDataFilePath"] = original_board_path
        result["routedBoardDataFilePath"] = original_board_path
        reroute_result["drcFailureReasons"] = [
            attempt.failure_summary for attempt in validation.attempts if attempt.failure_summary
        ]
    result["rerouteResult"] = reroute_result

    check_report = dict(result.get("checkReport") or {})
    checks = list(check_report.get("checks") or [])
    checks.append(
        {
            "name": "drc_validation",
            "passed": validation.passed,
            "detail": (
                json.dumps(reroute_result.get("localReroutePolicy") or {}, ensure_ascii=False)
                if validation.passed and reroute_result.get("localReroutePolicy")
                else ("DRC passed" if validation.passed else validation.last_failure_summary)
            ),
        }
    )
    check_report["checks"] = checks
    check_report["passed"] = bool(check_report.get("passed", True)) and validation.passed
    result["checkReport"] = check_report
    if not validation.passed:
        result["explanation"] = (
            f"{result.get('explanation', '')} DRC 未通过，已返回原始版图文件地址：{original_board_path}。"
            f"最后失败原因：{validation.last_failure_summary}"
        ).strip()
    return result


def _run_reroute_drc_iterations(
    *,
    base_payload: Dict[str, Any],
    original_board_data: str,
    original_board_path: str,
    output_dir: str,
    sample_id: str,
    max_iterations: int,
    selected_nets: list[str],
    local_context: Any,
    regenerate,
    status_callback=None,
) -> Dict[str, Any]:
    from tools.pcb_reroute_drc import RerouteDrcValidation, validate_kicad_patch_with_drc

    attempts = []
    feedback: list[str] = []
    iteration_history: list[Dict[str, Any]] = []
    payload = base_payload
    for iteration in range(1, max_iterations + 1):
        if iteration > 1:
            if status_callback:
                status_callback(f"DRC 第 {iteration - 1} 轮未通过，正在根据错误反馈重新生成拆线重布结果...")
            payload = regenerate(feedback, iteration_history)
        patch_text = _model_patch_text(payload)
        patch_text, binding_warnings, binding_errors = _bind_reroute_patch_nets(
            patch_text,
            board_text=original_board_data,
            selected_nets=selected_nets,
            local_context=local_context,
        )
        endpoint_messages = _endpoint_guard_messages(
            patch_text,
            selected_nets=selected_nets,
            local_context=local_context,
        )
        endpoint_corrected = False
        if endpoint_messages:
            corrected_patch_text, endpoint_corrections = _correct_single_missing_route_segment_endpoints(
                patch_text,
                selected_nets=selected_nets,
                local_context=local_context,
            )
            if endpoint_corrections and corrected_patch_text != patch_text:
                patch_text = corrected_patch_text
                endpoint_corrected = True
                binding_warnings.extend(endpoint_corrections)
        if binding_warnings:
            reroute_result = dict(payload.get("rerouteResult") or {})
            existing_warnings = list(reroute_result.get("patchBindingWarnings") or [])
            reroute_result["patchBindingWarnings"] = existing_warnings + binding_warnings
            payload["rerouteResult"] = reroute_result
        if binding_errors:
            return _append_reroute_check(
                payload,
                name="patch_net_binding",
                passed=False,
                detail="；".join(binding_errors),
            )
        payload["kicadPatch"] = patch_text
        if status_callback:
            status_callback(f"正在进行拆线重布 DRC 校验（第 {iteration}/{max_iterations} 轮）...")
        attempt = validate_kicad_patch_with_drc(
            original_board_data=original_board_data,
            model_output_text=patch_text,
            output_dir=output_dir,
            sample_id=sample_id,
            iteration=iteration,
        )
        local_passed, local_summary, local_detail = _local_reroute_drc_passes(
            attempt,
            selected_nets=selected_nets,
        )
        if isinstance(attempt.drc_result, dict):
            attempt.drc_result["localReroutePolicy"] = local_detail
        if local_passed and not attempt.passed:
            attempt.passed = True
            attempt.failure_summary = local_summary
        attempts.append(attempt)
        if attempt.passed:
            if status_callback:
                status_callback(f"拆线重布 DRC 第 {iteration} 轮通过，正在生成可导入结果...")
            validation = RerouteDrcValidation(
                passed=True,
                routed_board_data_file_path=attempt.filled_board_data_file_path,
                original_board_data_file_path=original_board_path,
                attempts=attempts,
            )
            return _apply_drc_validation_to_payload(payload, validation=validation, original_board_path=original_board_path)
        feedback_parts = []
        if endpoint_messages:
            feedback_parts.append(
                "上一轮模型端点硬校验未通过："
                + "；".join(endpoint_messages)
                + ("；agent 已先修正端点并跑 DRC，但修正后仍未通过。" if endpoint_corrected else "；agent 无法自动修正该 patch。")
            )
        feedback_parts.append(_drc_feedback_for_prompt(attempt))
        feedback.append("\n".join(part for part in feedback_parts if part))
        iteration_history.append(
            {
                "iteration": iteration,
                "passed": False,
                "kicadPatch": patch_text,
                "filledBoardDataFilePath": attempt.filled_board_data_file_path,
                "fillDetail": attempt.fill_detail,
                "drcResult": attempt.drc_result,
                "failureSummary": attempt.failure_summary,
                "localReroutePolicy": local_detail,
                "endpointGuard": endpoint_messages,
                "endpointCorrected": endpoint_corrected,
            }
        )

    validation = RerouteDrcValidation(
        passed=False,
        routed_board_data_file_path=original_board_path,
        original_board_data_file_path=original_board_path,
        attempts=attempts,
        last_failure_summary=feedback[-1] if feedback else "DRC validation did not run.",
    )
    return _apply_drc_validation_to_payload(payload, validation=validation, original_board_path=original_board_path)


def reroute(userData: str = "", session_id: Optional[str] = None) -> str:
    """Generate a local selected-trace reroute payload from drop_net context."""
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("reroute", session_id)
        logger.warning(msg)
        return json.dumps({"rerouteResult": None, "checkReport": {"passed": False, "errors": [msg]}}, ensure_ascii=False)

    try:
        user_data_obj = json.loads(userData) if isinstance(userData, str) and userData.strip() else {}
        if not isinstance(user_data_obj, dict):
            user_data_obj = {}
    except json.JSONDecodeError:
        return json.dumps(
            {"rerouteResult": None, "checkReport": {"passed": False, "errors": [f"无效的 userData JSON: {userData[:200]}"]}},
            ensure_ascii=False,
        )

    cached = _transport.get_cached_reroute_context(session_id=session_id) or {}
    _transport.send_status("已收到拆线重布参数，正在准备局部重布上下文...", session_id=session_id)
    nets = (
        user_data_obj.get("selectedNets")
        or user_data_obj.get("nets")
        or cached.get("selectedNets")
        or extract_reroute_nets(user_data_obj.get("userText", ""))
    )
    nets = [str(net).strip() for net in nets if str(net).strip()] if isinstance(nets, list) else []
    selected_trace_ids = (
        user_data_obj.get("selectedTraceIds")
        or user_data_obj.get("traceIds")
        or cached.get("selectedTraceIds")
        or []
    )
    selected_trace_ids = [
        str(trace_id).strip()
        for trace_id in selected_trace_ids
        if str(trace_id).strip()
    ] if isinstance(selected_trace_ids, list) else []

    if not nets and not selected_trace_ids:
        return json.dumps(
            {
                "rerouteResult": None,
                "checkReport": {
                    "passed": False,
                    "errors": ["Missing selectedNets or selectedTraceIds; cannot generate local reroute result."],
                },
            },
            ensure_ascii=False,
        )

    dropped_board_data = (
        user_data_obj.get("droppedBoardData")
        or cached.get("droppedBoardData")
        or _transport.get_cached_project_data(session_id=session_id)
        or ""
    )
    dropped_board_path = user_data_obj.get("droppedBoardDataFilePath") or cached.get("droppedBoardDataFilePath") or ""
    if not dropped_board_data and dropped_board_path:
        dropped_board_data, dropped_board_path = _read_board_file(str(dropped_board_path))

    original_board_path = (
        user_data_obj.get("originalBoardDataFilePath")
        or cached.get("originalBoardDataFilePath")
        or _nested_text_value(cached.get("localContext") or {}, "originalBoardDataFilePath", "boardDataFilePath")
        or str(dropped_board_path or "")
    )
    original_board_data = user_data_obj.get("originalBoardData") or cached.get("originalBoardData") or ""
    if not original_board_data and original_board_path:
        original_board_data, original_board_path = _read_board_file(str(original_board_path))
    if not original_board_data:
        original_board_data = dropped_board_data

    dropped_objects = user_data_obj.get("droppedObjects") or cached.get("droppedObjects") or []
    local_context = user_data_obj.get("localContext") or cached.get("localContext") or {}
    constraints = user_data_obj.get("constraints") or {}
    max_drc_iterations = _get_max_drc_iterations(user_data_obj)
    output_dir = _resolve_reroute_output_dir(user_data_obj, str(original_board_path or ""), session_id or "")
    conversion_notes: list[str] = []
    internal_dropped_board_data, internal_dropped_board_path, notes = _write_internal_board_data(
        board_data=dropped_board_data,
        board_path=str(dropped_board_path or ""),
        output_dir=output_dir,
        session_id=session_id or "session",
        label="dropped",
    )
    conversion_notes.extend(notes)
    if internal_dropped_board_data:
        dropped_board_data = internal_dropped_board_data
        dropped_board_path = internal_dropped_board_path

    internal_original_board_data, internal_original_board_path, notes = _write_internal_board_data(
        board_data=original_board_data,
        board_path=str(original_board_path or ""),
        output_dir=output_dir,
        session_id=session_id or "session",
        label="original",
    )
    conversion_notes.extend(notes)
    if internal_original_board_data:
        original_board_data = internal_original_board_data
        original_board_path = internal_original_board_path
    elif dropped_board_data:
        original_board_data = dropped_board_data
        original_board_path = str(dropped_board_path or original_board_path or "")

    normalized_local_context, coordinate_stats = _normalize_reroute_local_context_coordinates(
        local_context,
        board_text=original_board_data or dropped_board_data,
        nets=nets,
        dropped_objects=dropped_objects,
    )
    if isinstance(normalized_local_context, dict):
        local_context = normalized_local_context

    check_report = {
        "passed": bool(dropped_board_data),
        "checks": [
            {"name": "selection", "passed": bool(nets or selected_trace_ids), "detail": f"selectedNets={len(nets)}, selectedTraceIds={len(selected_trace_ids)}"},
            {"name": "dropped_board_data", "passed": bool(dropped_board_data), "detail": "已获得拆线后版图数据" if dropped_board_data else "未获得拆线后版图数据，按上下文请求生成"},
            {"name": "connectivity_scope", "passed": True, "detail": "仅对所选走线或 selectedNets 生成局部重布请求，不触碰其他网络"},
            {
                "name": "coordinate_normalization",
                "passed": bool(coordinate_stats.get("normalized") or not _frontend_missing_routes_from_context(local_context)),
                "detail": json.dumps(_strip_internal_reroute_paths(coordinate_stats), ensure_ascii=False)[:1200],
            },
        ],
    }

    _transport.send_status("正在调用拆线重布模型生成候选走线...", session_id=session_id)
    payload = _generate_reroute_with_model(
        nets=nets,
        selected_trace_ids=selected_trace_ids,
        dropped_board_data=dropped_board_data,
        dropped_board_path=str(dropped_board_path or ""),
        dropped_objects=dropped_objects,
        local_context=local_context,
        constraints=constraints,
        check_report=check_report,
        original_board_path=str(original_board_path or ""),
        debug_output_dir=output_dir,
        debug_label="initial",
        session_id=session_id or "session",
    )
    patch_text = _model_patch_text(payload)
    if max_drc_iterations <= 0:
        payload = _append_reroute_check(
            payload,
            name="drc_validation",
            passed=False,
            detail="DRC 校验已被配置跳过，未生成可导入 txt。",
        )
    elif not original_board_data:
        payload = _append_reroute_check(
            payload,
            name="drc_validation",
            passed=False,
            detail="缺少原始版图数据，无法执行 DRC 校验，也不会导入重布结果。",
        )
    elif not patch_text:
        no_patch_detail = _latest_reroute_failure_reason(payload) or (
            "模型未生成可回填的重布 patch，无法执行 DRC 校验，也不会导入重布结果。"
        )
        payload = _append_reroute_check(
            payload,
            name="model_patch",
            passed=False,
            detail=no_patch_detail,
        )
    else:
        def _regenerate(feedback: list[str], iteration_history: list[Dict[str, Any]]) -> Dict[str, Any]:
            return _generate_reroute_with_model(
                nets=nets,
                selected_trace_ids=selected_trace_ids,
                dropped_board_data=dropped_board_data,
                dropped_board_path=str(dropped_board_path or ""),
                dropped_objects=dropped_objects,
                local_context=local_context,
                constraints=constraints,
                check_report=check_report,
                original_board_path=str(original_board_path or ""),
                drc_feedback=feedback,
                drc_iteration_history=iteration_history,
                debug_output_dir=output_dir,
                debug_label=f"drc_retry_{len(iteration_history) + 2}",
                session_id=session_id or "session",
            )

        payload = _run_reroute_drc_iterations(
            base_payload=payload,
            original_board_data=original_board_data,
            original_board_path=str(original_board_path or ""),
            output_dir=output_dir,
            sample_id=f"{session_id or 'reroute'}_{'_'.join(nets or selected_trace_ids)}",
            max_iterations=max_drc_iterations,
            selected_nets=nets,
            local_context=local_context,
            regenerate=_regenerate,
            status_callback=lambda message: _transport.send_status(message, session_id=session_id),
        )
    public_txt_path = ""
    import_lines_path = ""
    failed_txt_path = ""
    routed_internal_path = ""
    latest_filled_board_path = ""
    drc_passed: bool | None = None
    routed_internal_path = str(payload.get("routedBoardDataFilePath") or "")
    if not routed_internal_path and isinstance(payload.get("rerouteResult"), dict):
        routed_internal_path = str(payload["rerouteResult"].get("routedBoardDataFilePath") or "")
    if isinstance(payload.get("rerouteResult"), dict) and "drcPassed" in payload["rerouteResult"]:
        drc_passed = payload["rerouteResult"].get("drcPassed") is True
    latest_filled_board_path = _latest_reroute_filled_board_path(payload)

    drc_agent_board_path = routed_internal_path if drc_passed is True else latest_filled_board_path
    if drc_passed is not None:
        payload = _append_drc_agent_report(
            payload,
            board_path=drc_agent_board_path,
            output_dir=output_dir,
            session_id=session_id or "session",
            target_bga=_reroute_target_bga_for_drc_agent(user_data_obj, cached, local_context),
        )

    if drc_passed is True:
        try:
            public_txt_path, notes = _convert_internal_kicad_to_public_txt(
                kicad_path=routed_internal_path,
                output_dir=output_dir,
                session_id=session_id or "session",
            )
            conversion_notes.extend(notes)
            import_lines_path, import_notes = _write_reroute_incremental_import_file(
                patch_text=_model_patch_text(payload),
                board_text=original_board_data,
                output_dir=output_dir,
                session_id=session_id or "session",
            )
            conversion_notes.extend(import_notes)
            if not import_lines_path:
                payload = _append_reroute_check(
                    payload,
                    name="txt_output_conversion",
                    passed=False,
                    detail="DRC 已通过，但未生成轻量增量导入文件，因此不会调用 importLines。",
                )
        except Exception as exc:
            logger.warning("Failed converting reroute result to public txt: %s", exc)
            payload = _append_reroute_check(
                payload,
                name="txt_output_conversion",
                passed=False,
                detail=f"输出 txt 转换失败：{exc}",
            )
    elif drc_passed is False and latest_filled_board_path:
        try:
            failed_txt_path, notes = _convert_internal_kicad_to_public_txt(
                kicad_path=latest_filled_board_path,
                output_dir=output_dir,
                session_id=session_id or "session",
                output_subdir="failed_txt",
            )
            conversion_notes.extend(notes)
            if failed_txt_path:
                reroute_result = dict(payload.get("rerouteResult") or {})
                reroute_result["drcFailedLayoutTxtFilePath"] = failed_txt_path
                payload["rerouteResult"] = reroute_result
            else:
                payload = _append_reroute_check(
                    payload,
                    name="failed_txt_output_conversion",
                    passed=False,
                    detail="DRC 未通过，且最后一次模型回填结果未能转换成 failed_txt 输出。",
                )
        except Exception as exc:
            logger.warning("Failed converting failed reroute result to txt: %s", exc)
            payload = _append_reroute_check(
                payload,
                name="failed_txt_output_conversion",
                passed=False,
                detail=f"DRC 未通过，失败回填 txt 转换失败：{exc}",
            )
    explain_board_path = (
        routed_internal_path
        if drc_passed is True
        else latest_filled_board_path or routed_internal_path or str(original_board_path or dropped_board_path or "")
    )
    payload = _append_explainability_report(
        payload,
        internal_board_path=explain_board_path,
        public_txt_path=public_txt_path,
        import_lines_path=import_lines_path,
    )
    public_payload = _public_reroute_payload(payload, public_txt_path, import_lines_path)
    pending_fields = _pending_reroute_fields_for_frontend(public_payload, public_txt_path, import_lines_path)
    if pending_fields:
        _transport.set_pending_pcb_fields(pending_fields, session_id=session_id)
    return json.dumps(public_payload, ensure_ascii=False)


registry.register(
    name="reroute",
    toolset="pcb",
    schema={
        "name": "reroute",
        "description": (
            "基于 drop_net 的拆线后上下文生成局部拆线重布结果包。"
            "用于局部重布流程，不调用全局 BGA fanout router。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userData": {
                    "type": "string",
                    "description": (
                        "可选 JSON 字符串，可包含 selectedNets、selectedTraceIds、droppedBoardData、"
                        "droppedBoardDataFilePath、originalBoardDataFilePath、localContext、constraints。"
                    ),
                }
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: reroute(args.get("userData", ""), session_id=kwargs.get("session_id")),
    check_fn=lambda: True,
)


logger.info("PCB tools registered: getProjectData, getSelectedElements, GetSelectedElements, deleteTracesById, deleteTracesForRerouting, generateFanoutParams, route, drop_net, reroute")
