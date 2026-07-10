"""PcbRouter local route completion adapter."""

from __future__ import annotations

import configparser
import csv
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
# ====== 功能：描述 pcbrouter 局部布线运行后的输出文件和报告。 ======
class PcbRouterRunOutputs:
    routing_result_path: Path
    import_lines_path: Path | None
    output_csv_path: Path | None
    report: str
    input_board_path: Path
    input_csv_path: Path
    stdout_path: Path
    stderr_path: Path


@dataclass
class LocalRouteCsvRow:
    net: str
    route_layer: str = ""
    target_x: str = ""
    target_y: str = ""
    target_layer: str = ""
    target_unit: str = ""


# ====== 功能：定位当前工具脚本所在项目根目录。 ======
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# ====== 功能：返回局部布线器配置文件候选路径。 ======
def _config_paths() -> list[Path]:
    paths: list[Path] = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).resolve().parent / "config.ini")
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        paths.append(Path(bundled) / "config.ini")
    paths.append(_repo_root() / "config.ini")
    return paths


# ====== 功能：读取局部布线器配置。 ======
def _load_router_config() -> tuple[configparser.ConfigParser, Path | None]:
    parser = configparser.ConfigParser()
    for path in _config_paths():
        if path.is_file():
            parser.read(path, encoding="utf-8-sig")
            return parser, path.parent.resolve()
    return parser, None


# ====== 功能：展开配置中的相对路径。 ======
def _expand_path(value: str, base_dir: Path | None = None) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(str(value or "").strip())))
    if expanded.is_absolute():
        return expanded
    if base_dir is not None:
        return (base_dir / expanded).resolve()
    return expanded.resolve()


# ====== 功能：列出 pcbrouter 可执行文件候选名称。 ======
def _candidate_binary_names() -> list[str]:
    names: list[str] = []
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        if machine in {"aarch64", "arm64"}:
            names.extend(["pcbrouter_aarch64", "pcbrouter_arm64"])
        elif machine in {"x86_64", "amd64"}:
            names.append("pcbrouter_x86_64")
    names.extend(["pcbrouter", "pcbrouter.exe"])
    deduped: list[str] = []
    for name in names:
        if name not in deduped:
            deduped.append(name)
    return deduped


# ====== 功能：从目录或文件路径解析可执行文件。 ======
def _binary_from_dir_or_file(path: Path) -> Path:
    if path.is_file():
        return path
    for name in _candidate_binary_names():
        for candidate in (path / name, path / "bin" / name):
            if candidate.is_file():
                return candidate
    return path / ("pcbrouter.exe" if sys.platform == "win32" else "pcbrouter")


# ====== 功能：定位 pcbrouter 可执行文件。 ======
def resolve_pcbrouter_binary() -> Path:
    for key in ("PCBROUTER_BIN", "PCB_ROUTER_BIN", "PCB_LOCAL_ROUTER_BIN"):
        value = os.getenv(key, "").strip()
        if value:
            return _expand_path(value)
    for key in ("PCBROUTER_DIR", "PCB_ROUTER_DIR", "PCB_LOCAL_ROUTER_DIR"):
        value = os.getenv(key, "").strip()
        if value:
            return _binary_from_dir_or_file(_expand_path(value))

    parser, config_base = _load_router_config()
    if parser.has_section("router"):
        for key in ("pcbrouter_bin", "pcb_local_router_bin"):
            raw = parser.get("router", key, fallback="").strip()
            if raw:
                return _expand_path(raw, base_dir=config_base)
        for key in ("pcbrouter_dir", "pcb_local_router_dir"):
            raw = parser.get("router", key, fallback="").strip()
            if raw:
                return _binary_from_dir_or_file(_expand_path(raw, base_dir=config_base))

    default_dirs = [
        _repo_root() / "tools" / "reroute_helper",
        _repo_root() / "vendor" / "pcbrouter" / "bin",
    ]
    for default_dir in default_dirs:
        for name in _candidate_binary_names():
            candidate = default_dir / name
            if candidate.is_file():
                return candidate
    fallback_dir = default_dirs[0]
    return fallback_dir / ("pcbrouter.exe" if sys.platform == "win32" else "pcbrouter")


# ====== 功能：检查当前系统是否可直接运行该二进制。 ======
def _native_binary_usable(binary_path: Path) -> bool:
    if not binary_path.is_file():
        return False
    try:
        header = binary_path.read_bytes()[:4]
    except OSError:
        return False
    if header == b"\x7fELF":
        return sys.platform.startswith("linux") and os.access(binary_path, os.X_OK)
    if header[:2] == b"MZ":
        return sys.platform == "win32" and os.access(binary_path, os.X_OK)
    return True


# ====== 功能：判断 pcbrouter 是否可用。 ======
def pcbrouter_available() -> bool:
    return _native_binary_usable(resolve_pcbrouter_binary())


# ====== 功能：构造 pcbrouter 命令行参数。 ======
def _pcbrouter_binary_args(binary_path: Path, *args: str) -> list[str]:
    header = binary_path.read_bytes()[:4]
    if header == b"\x7fELF":
        if not sys.platform.startswith("linux"):
            raise RuntimeError(
                f"{binary_path} 是 Linux ELF pcbrouter，当前 {sys.platform} 不能直接运行；"
                "请在 Linux/天翼云环境执行，或配置本平台可执行版本。"
            )
        return [str(binary_path), *args]
    if header[:2] == b"MZ":
        if sys.platform == "win32":
            return [str(binary_path), *args]
        raise RuntimeError(f"{binary_path} 是 Windows 可执行文件，当前 {sys.platform} 不能直接运行。")
    return [sys.executable, str(binary_path), *args]


# ====== 功能：清理并重建局部布线工作目录。 ======
def _reset_work_dir(work_dir: Path) -> None:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)


# ====== 功能：从板数据中提取层名别名。 ======
def _layer_aliases(project_data: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    pattern = re.compile(
        r'\(\s*\d+\s+"([^"]+\.Cu)"\s+(?:signal|power|mixed|jumper)\s*(?:"([^"]+)")?',
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(project_data or ""):
        canonical = match.group(1).strip()
        display = (match.group(2) or "").strip()
        aliases[canonical.casefold()] = canonical
        aliases[canonical.replace(".Cu", "").casefold()] = canonical
        if display:
            aliases[display.casefold()] = canonical
    aliases.setdefault("top", "F.Cu")
    aliases.setdefault("f.cu", "F.Cu")
    aliases.setdefault("bottom", "B.Cu")
    aliases.setdefault("b.cu", "B.Cu")
    return aliases


# ====== 功能：归一化局部布线层名。 ======
def _normalize_route_layer(value: Any, aliases: dict[str, str]) -> str:
    layer = str(value or "").strip()
    if not layer:
        return ""
    mapped = aliases.get(layer.casefold())
    if mapped:
        return mapped
    if re.fullmatch(r"In\d+(?:\.Cu)?", layer, flags=re.IGNORECASE):
        number = re.search(r"\d+", layer)
        return f"In{number.group(0)}.Cu" if number else layer
    if layer.casefold() in {"f.cu", "b.cu"}:
        return layer[0].upper() + ".Cu"
    if layer.endswith(".Cu"):
        return layer
    return ""


def _csv_number(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _number_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _format_mm(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _kicad_board_bbox_mm(project_data: str) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    text = project_data or ""
    for block in _sexpr_blocks(text, "gr_line"):
        if "Edge.Cuts" not in block:
            continue
        for match in re.finditer(r"\(\s*(?:start|end)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block):
            points.append((float(match.group(1)), float(match.group(2))))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _sexpr_blocks(text: str, head: str) -> list[str]:
    blocks: list[str] = []
    pattern = re.compile(r"\(\s*" + re.escape(head) + r"\b", re.IGNORECASE)
    for match in pattern.finditer(text or ""):
        depth = 0
        for pos in range(match.start(), len(text)):
            char = text[pos]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.start() : pos + 1])
                    break
    return blocks


def _unit_from_route_item(item: dict[str, Any], end: dict[str, Any]) -> str:
    for source in (end, item):
        for key in ("unit", "coordUnit", "coordinateUnit"):
            value = str(source.get(key) or "").strip().lower()
            if value:
                return value
    return ""


def _point_inside_bbox(x: float, y: float, bbox: tuple[float, float, float, float], margin: float = 2.0) -> bool:
    min_x, min_y, max_x, max_y = bbox
    return min_x - margin <= x <= max_x + margin and min_y - margin <= y <= max_y + margin


def _convert_target_point_to_mm(x: float, y: float, unit: str, bbox: tuple[float, float, float, float] | None) -> tuple[float, float, str]:
    normalized = unit.strip().lower()
    if normalized in {"mm", "millimeter", "millimeters"}:
        return x, y, "mm"
    if normalized in {"mil", "mils"}:
        return x * 0.0254, y * 0.0254, "mil"
    if normalized in {"dbu", "0.01mil", "pcb_dbu"}:
        return x * 0.000254, y * 0.000254, "dbu"
    if bbox is None:
        return (x * 0.0254, y * 0.0254, "mil-auto") if max(abs(x), abs(y)) > 100 else (x, y, "mm-auto")

    raw_inside = _point_inside_bbox(x, y, bbox)
    mil_x, mil_y = x * 0.0254, y * 0.0254
    dbu_x, dbu_y = x * 0.000254, y * 0.000254
    mil_inside = _point_inside_bbox(mil_x, mil_y, bbox)
    dbu_inside = _point_inside_bbox(dbu_x, dbu_y, bbox)
    max_board_coord = max(abs(bbox[0]), abs(bbox[1]), abs(bbox[2]), abs(bbox[3]), 1.0)

    if raw_inside and max(abs(x), abs(y)) <= max_board_coord + 5:
        return x, y, "mm-auto"
    if mil_inside:
        return mil_x, mil_y, "mil-auto"
    if dbu_inside:
        return dbu_x, dbu_y, "dbu-auto"
    return x, y, "unknown"


def _local_route_row_from_item(item: dict[str, Any], aliases: dict[str, str], board_bbox: tuple[float, float, float, float] | None) -> LocalRouteCsvRow | None:
    net = str(item.get("net") or item.get("net_name") or item.get("netName") or item.get("name") or "").strip()
    if not net:
        return None
    route_layer = _normalize_route_layer(item.get("route_layer") or item.get("layer"), aliases)
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    raw_x = _number_value(item.get("target_x") or item.get("targetX") or item.get("target_x_mm") or item.get("targetXMm") or end.get("x"))
    raw_y = _number_value(item.get("target_y") or item.get("targetY") or item.get("target_y_mm") or item.get("targetYMm") or end.get("y"))
    target_x = target_y = target_unit = ""
    if raw_x is not None and raw_y is not None:
        x_mm, y_mm, target_unit = _convert_target_point_to_mm(raw_x, raw_y, _unit_from_route_item(item, end), board_bbox)
        if board_bbox is not None and not _point_inside_bbox(x_mm, y_mm, board_bbox, margin=5.0):
            raise ValueError(f"重布线终点坐标无法转换为 KiCad mm: net={net} raw=({raw_x},{raw_y}) converted=({x_mm:.6f},{y_mm:.6f}) unit={target_unit}")
        target_x = _format_mm(x_mm)
        target_y = _format_mm(y_mm)
    target_layer = _normalize_route_layer(
        item.get("target_layer") or item.get("targetLayer") or end.get("layer") or item.get("route_layer") or item.get("layer"),
        aliases,
    )
    if not (target_x and target_y):
        target_x = ""
        target_y = ""
        target_layer = ""
    return LocalRouteCsvRow(net=net, route_layer=route_layer, target_x=target_x, target_y=target_y, target_layer=target_layer, target_unit=target_unit)


# ====== 功能：整理局部布线 CSV 行顺序。 ======
def _ordered_local_route_rows(route_params: dict[str, Any], project_data: str) -> list[LocalRouteCsvRow]:
    aliases = _layer_aliases(project_data)
    board_bbox = _kicad_board_bbox_mm(project_data)
    rows: list[LocalRouteCsvRow] = []
    seen: set[str] = set()
    order_lines = route_params.get("orderLines") or []
    for item in order_lines:
        if not isinstance(item, dict):
            continue
        row = _local_route_row_from_item(item, aliases, board_bbox)
        if row is None or row.net in seen:
            continue
        seen.add(row.net)
        rows.append(row)

    for key in ("nets", "selectedNets", "localRouteNets", "pcbrouterNets"):
        value = route_params.get(key)
        if not isinstance(value, list):
            continue
        for raw_net in value:
            if isinstance(raw_net, dict):
                row = _local_route_row_from_item(raw_net, aliases, board_bbox)
            else:
                net = str(raw_net or "").strip()
                row = LocalRouteCsvRow(net=net) if net else None
            if row is None or row.net in seen:
                continue
            seen.add(row.net)
            rows.append(row)
    return rows


# ====== 功能：读取用户指定的 CSV 覆盖路径。 ======
def _csv_override_path(route_params: dict[str, Any]) -> str:
    keys = ("pcbrouterCsvPath", "localRouteCsvPath", "bgaLocalRouteCsvPath", "csvPath")
    for key in keys:
        value = str(route_params.get(key) or "").strip()
        if value:
            return value
    return ""


# ====== 功能：写出 pcbrouter 需要的局部布线 CSV。 ======
def write_local_route_csv(
    *,
    route_params: dict[str, Any],
    project_data: str,
    work_dir: Path,
) -> Path:
    override = _csv_override_path(route_params)
    target = work_dir / "local_route_input.csv"
    if override:
        source = Path(os.path.expandvars(os.path.expanduser(override)))
        if not source.is_file():
            raise FileNotFoundError(f"pcbrouter CSV 输入不存在: {source}")
        shutil.copyfile(source, target)
        return target

    rows = _ordered_local_route_rows(route_params, project_data)
    if not rows:
        raise ValueError("局部布线完善缺少可写入 CSV 的目标 net")
    has_target = any(row.target_x and row.target_y for row in rows)
    has_route_layer = any(row.route_layer for row in rows)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if has_target:
            writer.writerow(["net", "target_x_mm", "target_y_mm", "target_layer"])
        else:
            writer.writerow(["net", "route_layer"] if has_route_layer else ["net"])
        for row in rows:
            if has_target:
                writer.writerow([row.net, row.target_x, row.target_y, row.target_layer])
            elif has_route_layer:
                writer.writerow([row.net, row.route_layer])
            else:
                writer.writerow([row.net])
    return target


# ====== 功能：准备 pcbrouter 输入板文件。 ======
def _prepare_board_input(
    *,
    project_data: str,
    source_board_path: str,
    work_dir: Path,
) -> tuple[Path, Path | None]:
    source = Path(os.path.expandvars(os.path.expanduser(source_board_path))) if source_board_path else None
    sidecar_path: Path | None = None
    if source and source.is_file() and source.suffix.lower() == ".kicad_pcb":
        target = work_dir / source.name
        shutil.copyfile(source, target)
        sidecar = source.with_suffix(".kicad_dru")
        if sidecar.is_file():
            sidecar_path = target.with_suffix(".kicad_dru")
            shutil.copyfile(sidecar, sidecar_path)
        return target, sidecar_path

    if not str(project_data or "").strip():
        raise ValueError("局部布线完善缺少 KiCad PCB 输入内容")
    target = work_dir / "local_route_input.kicad_pcb"
    target.write_text(project_data, encoding="utf-8")
    return target, None


# ====== 功能：运行外部进程并返回 CompletedProcess。 ======
def _run_process(args: list[str], work_dir: Path, timeout: int) -> subprocess.CompletedProcess:
    logger.info("Executing pcbrouter local route completion: %s in %s", args, work_dir)
    return subprocess.run(
        args,
        cwd=work_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# ====== 功能：写出外部进程 stdout/stderr 日志。 ======
def _write_process_logs(work_dir: Path, proc: subprocess.CompletedProcess) -> tuple[Path, Path]:
    stdout_path = work_dir / "pcbrouter.stdout.log"
    stderr_path = work_dir / "pcbrouter.stderr.log"
    stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")
    return stdout_path, stderr_path


# ====== 功能：查找非空候选输出文件。 ======
def _nonempty_candidates(work_dir: Path, patterns: list[str], excluded: set[Path]) -> list[Path]:
    for pattern in patterns:
        candidates: list[Path] = []
        for path in work_dir.rglob(pattern):
            resolved = path.resolve()
            if resolved in excluded or not path.is_file() or path.stat().st_size <= 0:
                continue
            candidates.append(path)
        candidates.sort(key=lambda item: (item.stat().st_mtime, item.stat().st_size), reverse=True)
        if candidates:
            return candidates
    return []


# ====== 功能：解析 pcbrouter 生成的布线结果文件。 ======
def _resolve_routing_result_path(work_dir: Path, input_board_path: Path) -> Path:
    excluded = {input_board_path.resolve()}
    candidates = _nonempty_candidates(
        work_dir,
        [
            "output_routed/*.kicad_pcb",
            "output/*.kicad_pcb",
            "*bga_local*drvpost*.kicad_pcb",
            "*bga_local*.kicad_pcb",
            "*afterPostProcessing*.kicad_pcb",
            "*drvpost*.kicad_pcb",
            "*.kicad_pcb",
            "output_bga_local.afterPostProcessing",
            "output_bga_local.afterPostProcessing.",
            "output.afterPostProcessing",
            "output.afterPostProcessing.",
        ],
        excluded,
    )
    if candidates:
        return candidates[0].resolve()
    raise FileNotFoundError("pcbrouter 未生成可识别的局部布线结果文件")


# ====== 功能：解析 pcbrouter 生成的输出 CSV。 ======
def _resolve_output_csv_path(work_dir: Path, input_csv_path: Path) -> Path | None:
    excluded = {input_csv_path.resolve()}
    candidates = _nonempty_candidates(
        work_dir,
        [
            "output_routed/*.csv",
            "output/*.csv",
            "*bga_local*drvpost*.csv",
            "*drvpost*.csv",
            "*afterPostProcessing*.csv",
            "*.csv",
        ],
        excluded,
    )
    return candidates[0].resolve() if candidates else None


# ====== 功能：读取局部布线器超时时间。 ======
def _timeout_seconds() -> int:
    value = os.getenv("PCBROUTER_TIMEOUT_SECONDS") or os.getenv("PCB_LOCAL_ROUTER_TIMEOUT_SECONDS")
    if value:
        try:
            parsed = int(float(value))
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    parser, _ = _load_router_config()
    if parser.has_section("router"):
        raw = parser.get("router", "pcbrouter_timeout_seconds", fallback="").strip()
        if raw:
            try:
                parsed = int(float(raw))
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
    return 300


# ====== 功能：执行 pcbrouter 局部规则布线主流程。 ======
def run_pcbrouter_local_route(
    *,
    project_data: str,
    route_params: dict[str, Any],
    work_dir: Path,
    source_board_path: str = "",
    timeout: int | None = None,
) -> PcbRouterRunOutputs:
    """Run PcbRouter local route mode in an isolated subdirectory."""
    if not isinstance(route_params, dict):
        raise ValueError("route_params 必须是 JSON 对象")

    run_dir = work_dir / "pcbrouter_local_completion"
    _reset_work_dir(run_dir)
    for name in ("output", "log", "output_routed"):
        (run_dir / name).mkdir(parents=True, exist_ok=True)

    binary_path = resolve_pcbrouter_binary()
    if not _native_binary_usable(binary_path):
        raise FileNotFoundError(f"pcbrouter 二进制不可用或当前平台不能执行: {binary_path}")

    input_board_path, sidecar_path = _prepare_board_input(
        project_data=project_data,
        source_board_path=source_board_path,
        work_dir=run_dir,
    )
    board_text = input_board_path.read_text(encoding="utf-8", errors="replace")
    input_csv_path = write_local_route_csv(
        route_params=route_params,
        project_data=board_text,
        work_dir=run_dir,
    )

    args = _pcbrouter_binary_args(
        binary_path,
        input_board_path.name,
        "-bga_local_route",
        input_csv_path.name,
    )
    proc = _run_process(args, run_dir, timeout or _timeout_seconds())
    stdout_path, stderr_path = _write_process_logs(run_dir, proc)
    if proc.returncode != 0:
        output = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
        raise RuntimeError(f"pcbrouter 局部布线完善执行失败 (exit {proc.returncode}):\n{output[:1600]}")

    routing_result_path = _resolve_routing_result_path(run_dir, input_board_path)
    output_csv_path = _resolve_output_csv_path(run_dir, input_csv_path)
    row_count = max(0, sum(1 for _ in input_csv_path.open("r", encoding="utf-8", errors="replace")) - 1)
    sidecar_note = f"，已使用规则文件 {sidecar_path.name}" if sidecar_path else "，未发现同名 .kicad_dru，pcbrouter 将使用默认规则"
    csv_note = f"，统计报告 {output_csv_path.name}" if output_csv_path else "，未生成统计 CSV"
    report = (
        f"pcbrouter 局部布线完善完成：目标 net {row_count} 个，"
        f"输出 {routing_result_path.name}{csv_note}{sidecar_note}。"
    )
    return PcbRouterRunOutputs(
        routing_result_path=routing_result_path,
        import_lines_path=None,
        output_csv_path=output_csv_path,
        report=report,
        input_board_path=input_board_path.resolve(),
        input_csv_path=input_csv_path.resolve(),
        stdout_path=stdout_path.resolve(),
        stderr_path=stderr_path.resolve(),
    )
