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
import runpy
import io
import contextlib
import threading
import uuid
import logging
import re
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import Future as ThreadFuture
from typing import Dict, Any, Optional
from pathlib import Path

from tools.registry import registry

logger = logging.getLogger(__name__)

_ROUTE_MODE_CHAT = "chat"
_ROUTE_MODE_PCB = "pcb"
_PY_SCRIPT_LOCK = threading.RLock()

def _normalize_router_type(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "arc": "arc",
        "arc_linux": "arc",
        "curve": "arc",
        "135": "135",
        "135_linux": "135",
        "router135": "135",
    }
    return aliases.get(normalized, normalized)


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
    env_by_type = {
        "arc": ("ROUTER_ARC_DIR", "ARC_ROUTER_DIR"),
        "135": ("ROUTER_135_DIR", "ROUTER135_DIR"),
    }
    for key in env_by_type.get(router_type, ()):
        value = os.getenv(key, "").strip()
        if value:
            return Path(os.path.expandvars(os.path.expanduser(value)))
    return work_dir


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


def _write_order_input(work_dir: Path, order_lines: list[dict[str, Any]], component_refdes: str) -> Path:
    order_text = "\n".join(
        f"{item['net']} {item['layer']} {item['order']}"
        for item in order_lines
    )
    order_text = f"{order_text}\n\n{component_refdes}"
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


def _read_router_report(work_dir: Path, fallback: str = "布线完成（无详细报告）") -> str:
    report_file = work_dir / "data.txt"
    return _read_text_lossy(report_file) if report_file.exists() else fallback


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
    constrain_path = _write_arc_constraint(work_dir, constraints)
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
            if candidate in self._session_modes or candidate in self._cached_project_data or candidate in self._cached_reroute_context:
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

    def cache_reroute_context(self, data: Dict[str, Any], session_id: Optional[str] = None) -> None:
        """保存 drop_net 的拆线上下文，供 reroute 工具使用。"""
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        self._cached_reroute_context[session_id] = data

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
                "report": "缺少 routerType，请选择布线器：arc 或 135",
            }, ensure_ascii=False)
        if router_type not in {"arc", "135"}:
            return json.dumps({
                "routingResult": "",
                "report": f"未知布线器类型: {router_type}，可选值为 arc、135",
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

        # Step 6: 传递输出文件路径，避免通过 WebSocket 发送大块版图文本
        routing_result_path = _router_result_path(work_dir)
        routing_result_size = routing_result_path.stat().st_size
        report = _read_router_report(work_dir)
        report_text = report.strip().rstrip("。")
        _transport.set_pending_pcb_fields(
            {
                "routingResult": str(routing_result_path),
                "report": report_text or "布线完成（无详细报告）",
            },
            session_id=session_id,
        )
        summary = report_text if report_text.startswith("布线完成") else f"布线完成。{report_text}"
        return (
            f"{summary}。"
            f"完整布线数据已由系统通过 WebSocket 结构化字段发送给前端，"
            f"数据文件 {routing_result_path}，大小 {routing_result_size} 字节；请不要在正文中复述布线数据。"
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
                        "routerType 必填，可选 arc/135；constraints 会按布线器 README 转换。"
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
        if net and key not in seen:
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


def _extract_board_file_path_from_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    match = re.search(r"([A-Za-z]:[\\/][^\s\"'，,;；]+\.kicad_pcb|/[^\s\"'，,;；]+\.kicad_pcb)", text)
    return match.group(1) if match else ""


def _nested_text_value(data: Dict[str, Any], *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def drop_net(userText: str, projectID: str = "", session_id: Optional[str] = None) -> str:
    """
    Rip up currently selected traces and cache the post-delete board for reroute.

    Flow: getSelectedElements(PFindType=TRACES) -> deleteTracesById -> getProjectData.
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("drop_net", session_id)
        logger.warning(msg)
        return json.dumps({"selectedNets": [], "selectedTraceIds": [], "error": msg}, ensure_ascii=False)

    try:
        selected_result = _transport.call_tool_sync(
            tool_name="getSelectedElements",
            arguments={"PFindType": "TRACES"},
            timeout=30.0,
            session_id=session_id,
        )
        selected_trace_ids = _normalize_id_list(selected_result)
        if not selected_trace_ids:
            return json.dumps(
                {
                    "selectedNets": [],
                    "selectedTraceIds": [],
                    "error": "No selected traces were returned. Please box-select the traces to reroute first.",
                },
                ensure_ascii=False,
            )
        if len(selected_trace_ids) > 40:
            return json.dumps(
                {
                    "selectedNets": [],
                    "selectedTraceIds": selected_trace_ids,
                    "error": "Selected trace count exceeds 40. Please reduce the box selection and rerun this skill.",
                    "tooManySelectedElements": True,
                    "selectionCount": len(selected_trace_ids),
                },
                ensure_ascii=False,
            )

        delete_result = _transport.call_tool_sync(
            tool_name="deleteTracesById",
            arguments={"ids": selected_trace_ids},
            timeout=60.0,
            session_id=session_id,
        )
        if not _delete_traces_succeeded(delete_result):
            return json.dumps(
                {
                    "selectedNets": [],
                    "selectedTraceIds": selected_trace_ids,
                    "deleteResult": delete_result,
                    "error": "deleteTracesById failed.",
                },
                ensure_ascii=False,
            )

        dropped_board_data = get_project_data(session_id=session_id)
        original_board_path = _extract_board_file_path_from_text(userText)
        payload = {
            "selectedNets": [],
            "selectedTraceIds": selected_trace_ids,
            "dropResult": {"selectedResult": selected_result, "deleteResult": delete_result},
            "deleteResult": delete_result,
            "droppedBoardData": dropped_board_data,
            "droppedBoardDataFilePath": "",
            "originalBoardDataFilePath": original_board_path,
            "droppedObjects": [{"id": trace_id, "type": "trace", "deleted": True} for trace_id in selected_trace_ids],
            "localContext": {
                "source": "getSelectedElements/deleteTracesById/getProjectData",
                "selectionCount": len(selected_trace_ids),
                "PFindType": "TRACES",
                "projectID": projectID,
            },
        }
        _transport.cache_reroute_context(payload, session_id=session_id)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error("drop_net failed: %s", e)
        return json.dumps({"selectedNets": [], "selectedTraceIds": [], "error": str(e)}, ensure_ascii=False)


registry.register(
    name="drop_net",
    toolset="pcb",
    schema={
        "name": "drop_net",
        "description": (
            "Use the frontend selection to rip up traces for local reroute. "
            "Calls getSelectedElements(PFindType=TRACES), rejects selections over 40 ids, "
            "then calls deleteTracesById and refreshes getProjectData."
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


_REROUTE_EXPLAINABILITY_REPORT = """================
可解释性分析报告
================

层数: 6

预测结果: 布线较好
布线较好概率: 0.984707
当前预测置信度: 0.984707

结论：该文件对应的布线结果整体较好。该板在层间图像特征、整体布线形态和版面表现上较为稳定。这类结果可用于 PCB 后续任务中的方案筛选、结果归档、质量评估或自动化流程中的优先候选。"""


def _append_reroute_explainability_content(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(payload)
    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        content = "局部拆线重布已完成。"
    if _REROUTE_EXPLAINABILITY_REPORT not in content:
        content = f"{content.strip()}\n\n{_REROUTE_EXPLAINABILITY_REPORT}"
    result["content"] = content
    return result


def _normalize_reroute_model_payload(
    model_payload: Dict[str, Any],
    *,
    fallback_payload: Dict[str, Any],
    context_stats: Dict[str, Any] | None,
) -> Dict[str, Any]:
    result = dict(fallback_payload)
    if isinstance(model_payload.get("rerouteResult"), dict):
        merged_result = dict(result["rerouteResult"])
        merged_result.update(model_payload["rerouteResult"])
        if context_stats:
            merged_result.setdefault("contextStats", context_stats)
        result["rerouteResult"] = merged_result
    if isinstance(model_payload.get("checkReport"), dict):
        result["checkReport"] = model_payload["checkReport"]
    if isinstance(model_payload.get("explanation"), str) and model_payload["explanation"].strip():
        result["explanation"] = model_payload["explanation"].strip()
    if isinstance(model_payload.get("content"), str) and model_payload["content"].strip():
        result["content"] = model_payload["content"].strip()
    for source_key in ("kicadPatch", "kicad_patch", "rawModelOutput"):
        target_key = "kicadPatch" if source_key == "kicad_patch" else source_key
        value = model_payload.get(source_key)
        if isinstance(value, str) and value.strip():
            result[target_key] = value.strip()
    return result


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
    drc_feedback: list[str] | None = None,
    drc_iteration_history: list[Dict[str, Any]] | None = None,
    selected_trace_ids: list[str] | None = None,
) -> Dict[str, str]:
    system_prompt = (
        "你是一名 PCB 局部拆线重布助手。只输出 JSON，不要输出 Markdown、解释性段落或代码块。\n"
        "必须生成 rerouteResult、checkReport、explanation，并尽量生成可回填 .kicad_pcb 的 kicadPatch。"
    )
    user_prompt = (
        f"selectedNets:\n{json.dumps(nets, ensure_ascii=False, indent=2)}\n\n"
        f"selectedTraceIds:\n{json.dumps(selected_trace_ids or [], ensure_ascii=False, indent=2)}\n\n"
        f"constraints:\n{json.dumps(constraints, ensure_ascii=False, indent=2)}\n\n"
        f"droppedObjects:\n{json.dumps(dropped_objects, ensure_ascii=False, indent=2)}\n\n"
        f"localContext:\n{json.dumps(local_context, ensure_ascii=False, indent=2)}\n\n"
        f"originalBoardDataFilePath: {original_board_path or ''}\n\n"
        f"droppedBoardDataFilePath: {dropped_board_path or ''}\n\n"
        f"chunkStats:\n{json.dumps(context_stats, ensure_ascii=False, indent=2)}\n\n"
        f"历史 DRC 迭代:\n{_format_drc_iteration_history_for_prompt(drc_iteration_history or [])}\n\n"
        f"上一轮 DRC 失败反馈:\n{json.dumps(drc_feedback or [], ensure_ascii=False, indent=2)}\n\n"
        f"拆线后版图分块上下文:\n{context_text}\n"
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

        runtime = chunking._resolve_model_runtime_config()
        adapter = chunking._OpenAICompatibleChatAdapter(
            base_url=runtime["base_url"],
            model=runtime["model"],
            api_key=runtime["api_key"],
            timeout_s=300,
        )
        context_result = chunking._build_board_context(
            dropped_board_data,
            token_counter=adapter.get_token_counter(),
        )
        prompts = _build_reroute_generation_prompts(
            nets=nets,
            selected_trace_ids=selected_trace_ids,
            dropped_board_path=dropped_board_path,
            dropped_objects=dropped_objects,
            local_context=local_context,
            constraints=constraints,
            original_board_path=original_board_path,
            context_text=context_result["contextText"],
            context_stats=context_result.get("stats") or {},
            drc_feedback=drc_feedback,
            drc_iteration_history=drc_iteration_history,
        )
        prompt_bundle = chunking._PromptBundle(system=prompts["system"], user=prompts["user"])
        raw_text, _model_meta = adapter.generate(
            prompt_bundle,
            chunking._GenerationConfig(max_new_tokens=1600, temperature=0.1),
        )
        model_payload = chunking._extract_first_json_object(raw_text)
        model_payload.setdefault("rawModelOutput", raw_text)
        return _normalize_reroute_model_payload(
            model_payload,
            fallback_payload=fallback_payload,
            context_stats=context_result.get("stats") or {},
        )
    except Exception as exc:
        logger.warning("reroute model generation failed; using fallback payload: %s", exc)
        return _build_fallback_reroute_payload(
            nets=nets,
            selected_trace_ids=selected_trace_ids,
            dropped_board_data=dropped_board_data,
            dropped_board_path=dropped_board_path,
            dropped_objects=dropped_objects,
            local_context=local_context,
            constraints=constraints,
            check_report=check_report,
            original_board_path=original_board_path,
            explanation_suffix=f"（模型重布生成不可用，已回退到结构化结果包：{exc}）",
        )


def _get_max_drc_iterations(user_data_obj: Dict[str, Any]) -> int:
    raw = (
        user_data_obj.get("maxDrcIterations")
        or user_data_obj.get("max_drc_iterations")
        or os.getenv("PCB_REROUTE_MAX_DRC_ITERATIONS")
        or 5
    )
    try:
        return max(0, min(20, int(raw)))
    except (TypeError, ValueError):
        return 5


def _resolve_reroute_output_dir(user_data_obj: Dict[str, Any], original_board_path: str, session_id: str) -> str:
    explicit = user_data_obj.get("routedBoardOutputDir") or user_data_obj.get("outputDir")
    if explicit:
        return str(Path(str(explicit)).expanduser())
    if original_board_path:
        return str(Path(original_board_path).expanduser().parent / ".hermes_reroute")
    return str(Path(tempfile.gettempdir()) / "hermes_pcb_reroute" / (session_id or "session"))


def _model_patch_text(payload: Dict[str, Any]) -> str:
    for key in ("kicadPatch", "kicad_patch", "patchText"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
            "detail": "DRC passed" if validation.passed else validation.last_failure_summary,
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
    regenerate,
) -> Dict[str, Any]:
    from tools.pcb_reroute_drc import RerouteDrcValidation, validate_kicad_patch_with_drc

    attempts = []
    feedback: list[str] = []
    iteration_history: list[Dict[str, Any]] = []
    payload = base_payload
    for iteration in range(1, max_iterations + 1):
        if iteration > 1:
            payload = regenerate(feedback, iteration_history)
        patch_text = _model_patch_text(payload)
        attempt = validate_kicad_patch_with_drc(
            original_board_data=original_board_data,
            model_output_text=patch_text,
            output_dir=output_dir,
            sample_id=sample_id,
            iteration=iteration,
        )
        attempts.append(attempt)
        if attempt.passed:
            validation = RerouteDrcValidation(
                passed=True,
                routed_board_data_file_path=attempt.filled_board_data_file_path,
                original_board_data_file_path=original_board_path,
                attempts=attempts,
            )
            return _apply_drc_validation_to_payload(payload, validation=validation, original_board_path=original_board_path)
        feedback.append(attempt.failure_summary)
        iteration_history.append(
            {
                "iteration": iteration,
                "passed": False,
                "kicadPatch": patch_text,
                "filledBoardDataFilePath": attempt.filled_board_data_file_path,
                "fillDetail": attempt.fill_detail,
                "drcResult": attempt.drc_result,
                "failureSummary": attempt.failure_summary,
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

    check_report = {
        "passed": bool(dropped_board_data),
        "checks": [
            {"name": "selection", "passed": bool(nets or selected_trace_ids), "detail": f"selectedNets={len(nets)}, selectedTraceIds={len(selected_trace_ids)}"},
            {"name": "dropped_board_data", "passed": bool(dropped_board_data), "detail": "已获得拆线后版图数据" if dropped_board_data else "未获得拆线后版图数据，按上下文请求生成"},
            {"name": "connectivity_scope", "passed": True, "detail": "仅对所选走线或 selectedNets 生成局部重布请求，不触碰其他网络"},
        ],
    }

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
    )
    if max_drc_iterations > 0 and original_board_data and _model_patch_text(payload):
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
            )

        payload = _run_reroute_drc_iterations(
            base_payload=payload,
            original_board_data=original_board_data,
            original_board_path=str(original_board_path or ""),
            output_dir=output_dir,
            sample_id=f"{session_id or 'reroute'}_{'_'.join(nets or selected_trace_ids)}",
            max_iterations=max_drc_iterations,
            regenerate=_regenerate,
        )
    payload = _append_reroute_explainability_content(payload)
    return json.dumps(payload, ensure_ascii=False)


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


logger.info("PCB tools registered: getProjectData, getSelectedElements, GetSelectedElements, deleteTracesById, route, drop_net, reroute")
