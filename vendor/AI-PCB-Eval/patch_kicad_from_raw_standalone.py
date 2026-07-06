#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单文件「模型原文 → 抽取 segment/via → 回填挖空 .kicad_pcb」脚本。

- 仅需 Python 标准库，可复制到任意目录单独使用，不依赖 pcb_llm 包。
- 逻辑与仓库内 pcb_llm/kicad_patch.py + pcb_llm/apply_raw_output.py 保持一致；若上游有改动请同步更新本文件。

用法:
  python patch_kicad_from_raw_standalone.py \\
    --raw ./_out/raw_output_npu.txt \\
    --incomplete ./xxx_incomplete.kicad_pcb \\
    --out ./_out/xxx_filled.kicad_pcb

  python patch_kicad_from_raw_standalone.py --raw - --incomplete ... --out ...  < raw.txt

Python 中 import（与其它代码集成时推荐）:

  from patch_kicad_from_raw_standalone import fill_incomplete_board_from_raw_text

  result = fill_incomplete_board_from_raw_text(raw_text, incomplete_pcb_text)
  Path("out.kicad_pcb").write_text(result.filled_pcb_text, encoding="utf-8")
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

__all__ = [
    "ExtractedKicadObjects",
    "PatchFromRawResult",
    "extract_kicad_objects_from_text",
    "apply_patch_objects",
    "patch_kicad_file",
    "fill_incomplete_board_from_raw_text",
]


# --- kicad_patch (inlined) ---


@dataclass(frozen=True)
class ExtractedKicadObjects:
    segments: List[str]
    vias: List[str]
    other: List[str]


_RE_SEGMENT_LINE = re.compile(r"^\s*\(\s*segment\b", re.IGNORECASE)
_RE_VIA_LINE = re.compile(r"^\s*\(\s*via\b", re.IGNORECASE)


def _normalize_kicad_object(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    if not t.endswith(")"):
        raise ValueError(f"KiCad object does not end with ')': {t[:80]}...")
    return t + "\n"


def extract_kicad_objects_from_text(model_text: str) -> ExtractedKicadObjects:
    lines = model_text.splitlines()
    segs: List[str] = []
    vias: List[str] = []
    other: List[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if _RE_SEGMENT_LINE.match(line) or _RE_VIA_LINE.match(line):
            buf = [line.rstrip()]
            i += 1
            while i < len(lines):
                buf.append(lines[i].rstrip())
                if lines[i].strip() == ")":
                    i += 1
                    break
                if buf[-1].strip().endswith(")") and len(buf) == 1:
                    i += 1
                    break
                i += 1
            obj = "\n".join(buf)
            norm = _normalize_kicad_object(obj)
            if _RE_SEGMENT_LINE.match(line):
                segs.append(norm)
            else:
                vias.append(norm)
            continue

        if line.strip():
            other.append(line.rstrip())
        i += 1

    return ExtractedKicadObjects(segments=segs, vias=vias, other=other)


def _find_insertion_index(board_text: str) -> int:
    idx = board_text.rfind("\n)")
    if idx == -1:
        idx = board_text.rfind(")")
        if idx == -1:
            raise ValueError("Invalid .kicad_pcb: cannot find closing ')'")
        return idx
    return idx + 1


def apply_patch_objects(
    incomplete_board_text: str,
    objects: Sequence[str],
    *,
    ensure_unique: bool = True,
) -> str:
    insert_at = _find_insertion_index(incomplete_board_text)
    prefix = incomplete_board_text[:insert_at]
    suffix = incomplete_board_text[insert_at:]

    payload_lines: List[str] = []
    for obj in objects:
        norm = _normalize_kicad_object(obj)
        if ensure_unique and norm.strip() and norm.strip() in incomplete_board_text:
            continue
        payload_lines.append(norm.rstrip("\n"))

    payload = ""
    if payload_lines:
        payload = "\n" + "\n\n".join(payload_lines) + "\n"

    return prefix + payload + suffix


def patch_kicad_file(
    *,
    incomplete_path: str | Path,
    out_path: str | Path,
    objects: Sequence[str],
    ensure_unique: bool = True,
) -> None:
    inc = Path(incomplete_path).expanduser().resolve()
    out = Path(out_path).expanduser().resolve()
    text = inc.read_text(encoding="utf-8")
    patched = apply_patch_objects(text, objects, ensure_unique=ensure_unique)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(patched, encoding="utf-8")


@dataclass(frozen=True)
class PatchFromRawResult:
    """fill_incomplete_board_from_raw_text 的返回值。"""

    filled_pcb_text: str
    segments_count: int
    vias_count: int
    other_lines_count: int


def fill_incomplete_board_from_raw_text(
    raw_model_text: str,
    incomplete_board_text: str,
    *,
    ensure_unique: bool = True,
) -> PatchFromRawResult:
    """
    从模型原文抽取 (segment)/(via)，合并进挖空板字符串，返回补全后的 .kicad_pcb 全文。

    不读写磁盘；供 import 后在其它 Python 代码中调用。
    """
    if not (raw_model_text or "").strip():
        raise ValueError("raw_model_text 为空。")
    extracted = extract_kicad_objects_from_text(raw_model_text)
    objects = extracted.segments + extracted.vias
    if not objects:
        raise ValueError(
            "未抽取到任何 (segment ...) 或 (via ...)。请确认模型输出含 (segment 或 (via 开头的片段。"
        )
    filled = apply_patch_objects(incomplete_board_text, objects, ensure_unique=ensure_unique)
    return PatchFromRawResult(
        filled_pcb_text=filled,
        segments_count=len(extracted.segments),
        vias_count=len(extracted.vias),
        other_lines_count=len(extracted.other),
    )


# --- CLI ---


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="从模型原文抽取 (segment)/(via) 并回填到挖空 .kicad_pcb（单文件版，无需安装本仓库）。"
    )
    p.add_argument("--raw", required=True, help="模型输出文本路径；填 - 表示从标准输入读")
    p.add_argument("--incomplete", required=True, help="*_incomplete.kicad_pcb 路径")
    p.add_argument("--out", required=True, help="输出补全后的 .kicad_pcb 路径")
    p.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="即使挖空板里已有相同片段也再次插入（默认会跳过重复）",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    inc_path = Path(args.incomplete).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if not inc_path.exists():
        raise FileNotFoundError(f"--incomplete 不存在: {inc_path.as_posix()}")
    if inc_path.suffix != ".kicad_pcb":
        raise ValueError(f"--incomplete 须为 .kicad_pcb: {inc_path.as_posix()}")

    if args.raw == "-":
        raw_text = sys.stdin.read()
    else:
        raw_path = Path(args.raw).expanduser().resolve()
        if not raw_path.exists():
            raise FileNotFoundError(f"--raw 不存在: {raw_path.as_posix()}")
        raw_text = raw_path.read_text(encoding="utf-8")

    inc_text = inc_path.read_text(encoding="utf-8")
    result = fill_incomplete_board_from_raw_text(
        raw_text,
        inc_text,
        ensure_unique=not args.allow_duplicate,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.filled_pcb_text, encoding="utf-8")

    print(f"已写入: {out_path.as_posix()}")
    print(f"抽取 segment: {result.segments_count}; via: {result.vias_count}")
    if result.other_lines_count:
        print(f"未应用的非 KiCad 行数: {result.other_lines_count}（见原文）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
