from __future__ import annotations

import re
from typing import Any


_REF_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]{1,3}\d{1,4})(?![A-Za-z0-9_])")


# ====== 功能：从用户文本中抽取 BGA、router 类型和线宽线距约束。 ======
def extract_fanout_entities(text: Any) -> dict[str, Any]:
    source = str(text or "")
    targets = _all_refdes(source)
    constraints = _fanout_constraints(source)
    router_type = normalize_router_type(source)
    entities: dict[str, Any] = {}
    if targets:
        entities["selectedBGA"] = targets[0]
        entities["targetBGAs"] = targets
    if constraints:
        entities["constraints"] = constraints
    if router_type:
        entities["routerType"] = router_type
    return entities


# ====== 功能：把用户描述归一化为内部 router 类型。 ======
def normalize_router_type(value: Any) -> str:
    source = str(value or "").strip().lower()
    if not source:
        return ""
    if source in {"rule_135", "135_rule"}:
        return "rule_135"
    if source in {"rule_arc", "arc_rule", "arc"}:
        return "rule_arc"
    if re.search(r"\barc\b|弧形|圆弧|曲线", source, re.IGNORECASE):
        return "rule_arc"
    if re.search(r"135|折角|规则|rule", source, re.IGNORECASE):
        return "rule_135"
    return ""


# ====== 功能：提取文本中出现的器件编号。 ======
def _all_refdes(text: str) -> list[str]:
    values: list[str] = []
    for match in _REF_RE.finditer(text):
        value = match.group(1).upper()
        if value in {"L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8"}:
            continue
        if value not in values:
            values.append(value)
    return values


# ====== 功能：提取 fanout 相关数值约束。 ======
def _fanout_constraints(text: str) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    width = _number_after(text, [r"线宽", r"width", r"LineWidth", r"trace\s*width"])
    spacing = _number_after(text, [r"线距", r"间距", r"spacing", r"LineSpacing", r"clearance"])
    if width is not None:
        constraints["LineWidth"] = width
    if spacing is not None:
        constraints["LineSpacing"] = spacing
    return constraints


# ====== 功能：读取指定标签后的数字参数。 ======
def _number_after(text: str, labels: list[str]) -> int | float | None:
    for label in labels:
        pattern = rf"{label}\s*[:=：]?\s*(\d+(?:\.\d+)?)\s*(?:mil|mm|毫米)?"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            return int(value) if value.is_integer() else value
    return None
