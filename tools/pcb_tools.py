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
    if "net_list.txt" in detail:
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


def _run_arc_router(work_dir: Path, router_dir: Path, component_refdes: str, constraints: Any) -> None:
    _copy_runtime_support_files(router_dir, work_dir)
    layout_path = work_dir / "layout_input.txt"
    if not layout_path.exists():
        raise FileNotFoundError("缺少版图输入文件 layout_input.txt")
    _remove_file_if_exists(work_dir / f"{component_refdes}_pins.csv")
    _write_component_input(work_dir, component_refdes)
    constrain_path = _write_arc_constraint(work_dir, constraints)

    a_out = _copy_runtime_file(router_dir, work_dir, "a.out")
    b_out = _copy_runtime_file(router_dir, work_dir, "b.out")
    c_out = _copy_runtime_file(router_dir, work_dir, "c.out")
    turn_script = _copy_runtime_file(router_dir, work_dir, "Turn_QYF.py")

    _require_success(_run_process(_router_binary_args(a_out, layout_path.name, "component_input.txt"), work_dir), "arc a.out")
    _validate_component_pins(work_dir, component_refdes, (work_dir / "版图信息.txt").read_text(encoding="utf-8"), "arc a.out")
    _require_success(_run_process(_router_binary_args(b_out, "layer_input.txt", layout_path.name), work_dir), "arc b.out")
    _ensure_nonempty_file(work_dir, "layer_input.txt", "arc b.out")
    _require_success(_run_process(_router_binary_args(c_out, "order_input.txt", layout_path.name, constrain_path.name), work_dir), "arc c.out")
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
            if candidate in self._session_modes or candidate in self._cached_project_data:
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
# Tool 2: GetSelectedElements
# ============================================================================

def get_selected_elements(projectID: str, session_id: Optional[str] = None) -> str:
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
        msg = _session_mode_error("GetSelectedElements", session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        logger.info("GetSelectedElements start: projectID=%s", projectID)
        result = _transport.call_tool_sync(
            tool_name="GetSelectedElements",
            arguments={"projectID": projectID},
            timeout=30.0,
            session_id=session_id,
        )
        data = result if isinstance(result, str) else json.dumps(result)
        logger.info("GetSelectedElements success: %d chars", len(data))
        return data
    except Exception as e:
        logger.error(f"GetSelectedElements failed: {e}")
        return json.dumps({"error": str(e)})


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
            _run_arc_router(work_dir, _router_profile_dir("arc", work_dir), component_refdes, constraints)
        else:
            _run_135_router(work_dir, _router_profile_dir("135", work_dir), component_refdes, constraints)

        # Step 6: 传递输出文件路径，避免通过 WebSocket 发送大块版图文本
        routing_result_path = _router_result_path(work_dir)
        routing_result_size = routing_result_path.stat().st_size
        report = _read_router_report(work_dir)
        _transport.set_pending_pcb_fields({"routingResult": str(routing_result_path)}, session_id=session_id)

        report_text = report.strip().rstrip("。")
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


logger.info("PCB tools registered: getProjectData, GetSelectedElements, route")
