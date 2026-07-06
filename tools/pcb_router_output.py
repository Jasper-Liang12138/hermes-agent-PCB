from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


# ====== 功能：描述 router 输出转换后的文件路径和统计信息。 ======
@dataclass(slots=True)
class RouterOutputConversion:
    routing_input_path: Path
    routed_kicad_path: Path | None
    import_lines_path: Path
    wire_count: int
    notes: list[str]


# ====== 功能：以容错编码读取 router 输出文本。 ======
def _read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ====== 功能：把 mil 字符串转换为 PCB Builder 内部整数坐标。 ======
def _scale_mils_to_int(value: str) -> int:
    try:
        scaled = Decimal(str(value).strip()) * Decimal("100")
        return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return 0


# ====== 功能：把 router 层名归一化为 PCB Builder layout 层名。 ======
def _normalize_conductor_layer(layer: str) -> str:
    layer = (layer or "").strip()
    if not layer:
        return "Conductor/Unknown"
    if layer.startswith("Conductor/"):
        return layer
    aliases = {"TOP": "Top", "BOTTOM": "Bottom"}
    label = aliases.get(layer.upper(), layer.lower().capitalize())
    return f"Conductor/{label}"


# ====== 功能：把 line.out/ARC_output.txt 中的 LINE 记录转换为 layout 的 wires 块。 ======
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


# ====== 功能：查找 S 表达式中与左括号匹配的右括号位置。 ======
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


# ====== 功能：替换 layout 中已有 group，若不存在则插入到指定 group 前。 ======
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


# ====== 功能：动态加载当前项目根目录下的 convert.py 工具模块。 ======
def _load_convert_module(project_root: Path):
    module_path = project_root / "convert.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"convert.py not found: {module_path}")
    spec = importlib.util.spec_from_file_location("_pcb_agent_langgraph_convert", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load convert.py from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_pcb_agent_langgraph_convert"] = module
    spec.loader.exec_module(module)
    return module


# ====== 功能：把 PCB Builder layout txt 转换为 KiCad PCB 文件。 ======
def _txt_to_kicad(project_root: Path, txt_path: Path, output_dir: Path) -> Path:
    convert_mod = _load_convert_module(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = convert_mod.convert_one("txt_to_kicad", txt_path, output_dir, None)
    output_path = Path(str(result.get("output") or ""))
    if not output_path.is_file():
        raise RuntimeError(f"txt_to_kicad did not create output: {txt_path}")
    return output_path


# ====== 功能：把 router 原始输出合成为完整 layout 和可选 KiCad 文件。 ======
def convert_router_output_to_layout(
    *,
    project_root: Path,
    original_board_text: str,
    work_dir: Path,
    router_type: str,
    import_lines_path: str | Path,
) -> RouterOutputConversion:
    work_dir.mkdir(parents=True, exist_ok=True)
    import_path = Path(import_lines_path)
    if not import_path.is_file() or import_path.stat().st_size <= 0:
        raise FileNotFoundError(f"router import lines file missing: {import_path}")

    routing_input = work_dir / "routing_input.txt"
    notes: list[str] = []
    wire_blocks, wire_count = _build_wire_blocks_from_line_output(import_path)
    if wire_count <= 0:
        raise RuntimeError(f"{import_path.name} 未包含可转换的 LINE 走线段")

    if routing_input.is_file() and routing_input.stat().st_size > 0:
        content = _read_text_lossy(routing_input)
        notes.append("reuse_existing_routing_input")
    else:
        content = str(original_board_text or "")
        notes.append("routing_input_from_original_board")
    if not content.strip():
        raise RuntimeError("缺少原始 layout 文本，无法生成 routing_input.txt")

    content = _replace_or_insert_group(content, "wires", wire_blocks, "vias")
    routing_input.write_text(content, encoding="utf-8")
    notes.append(f"wire_count:{wire_count}")

    routed_kicad: Path | None = None
    if re.search(r"(?is)^\s*\(\s*layout\b|Pcb-Design_Version|layermanager|conductives", content):
        routed_kicad = _txt_to_kicad(project_root, routing_input, work_dir / "kicad")
        notes.append(f"converted_txt_to_kicad:{routed_kicad}")
    elif re.search(r"(?is)^\s*\(\s*kicad_pcb\b", content):
        routed_kicad = work_dir / "routing_input.kicad_pcb"
        routed_kicad.write_text(content, encoding="utf-8")
        notes.append("routing_input_already_kicad")

    return RouterOutputConversion(
        routing_input_path=routing_input,
        routed_kicad_path=routed_kicad,
        import_lines_path=import_path,
        wire_count=wire_count,
        notes=notes,
    )
