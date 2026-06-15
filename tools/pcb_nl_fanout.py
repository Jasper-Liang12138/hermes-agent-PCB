"""Natural-language helpers for BGA fanout layer/order requests."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional


_LAYER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:SIG\s*0?\d+[A-Za-z0-9_-]*|ART\s*0?\d+[A-Za-z0-9_-]*|L\s*0?\d+[A-Za-z0-9_-]*|"
    r"F\.Cu|B\.Cu|Top|Bottom|顶层|底层|第\s*\d+\s*层)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_GENERIC_NET_RE = re.compile(
    r"(?<![A-Za-z0-9_./+-])(?:NET[A-Za-z0-9_.+\-/]*|N\d+[A-Za-z0-9_.+\-/]*|"
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_.+\-/]+|GND|VCC|VDD|VSS)(?![A-Za-z0-9_./+-])",
    re.IGNORECASE,
)
_CLAUSE_SEPARATOR_RE = re.compile(r"[\n;；。]")
_GROUP_SEPARATOR_RE = re.compile(r"[\n,，;；。]")
_WIDTH_RE = re.compile(r"(?:线宽|linewidth|line\s*width|width)\s*(?:为|是|=|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:mil)?", re.IGNORECASE)
_SPACING_RE = re.compile(r"(?:线距|间距|spacing|line\s*spacing)\s*(?:为|是|=|:|：)?\s*(\d+(?:\.\d+)?)\s*(?:mil)?", re.IGNORECASE)
_EXCLUDE_NET_TOKENS = {
    "arc",
    "rl",
    "bga",
    "pcb",
    "top",
    "bottom",
    "f.cu",
    "b.cu",
}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def _positive_number(value: str) -> Optional[float | int]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(number) if number.is_integer() else number


def parse_fanout_constraints_from_text(text: Any) -> Dict[str, Any]:
    """Extract explicitly stated LineWidth/LineSpacing constraints from text."""
    source = _as_text(text)
    constraints: Dict[str, Any] = {}
    width = _WIDTH_RE.search(source)
    if width:
        number = _positive_number(width.group(1))
        if number is not None:
            constraints["LineWidth"] = number
    spacing = _SPACING_RE.search(source)
    if spacing:
        number = _positive_number(spacing.group(1))
        if number is not None:
            constraints["LineSpacing"] = number
    return constraints


def _normalize_layer(raw: str) -> str:
    value = re.sub(r"\s+", "", str(raw or "").strip())
    lowered = value.lower()
    if value == "顶层":
        return "Top"
    if value == "底层":
        return "Bottom"
    if lowered == "top":
        return "Top"
    if lowered == "bottom":
        return "Bottom"
    if lowered in {"f.cu", "b.cu"}:
        return "F.Cu" if lowered.startswith("f") else "B.Cu"
    ordinal = re.fullmatch(r"第(\d+)层", value)
    if ordinal:
        return f"SIG{int(ordinal.group(1)):02d}"
    for prefix in ("SIG", "ART", "L"):
        if lowered.startswith(prefix.lower()):
            suffix = value[len(prefix):]
            digits = re.match(r"0*(\d+)(.*)", suffix)
            if digits:
                return f"{prefix}{int(digits.group(1)):02d}{digits.group(2)}"
            return prefix + suffix
    return value


def _iter_fallback_nets(order_lines: Any) -> Iterable[str]:
    if isinstance(order_lines, str) and order_lines.strip():
        try:
            order_lines = json.loads(order_lines)
        except json.JSONDecodeError:
            return []
    if not isinstance(order_lines, list):
        return []
    nets: list[str] = []
    for item in order_lines:
        if not isinstance(item, dict):
            continue
        net = str(item.get("net") or "").strip()
        if net:
            nets.append(net)
    return nets


def _known_net_names(fallback_order_lines: Any = None, allowed_nets: Optional[set[str]] = None) -> list[str]:
    names: list[str] = []
    for net in _iter_fallback_nets(fallback_order_lines):
        if net.casefold() not in {item.casefold() for item in names}:
            names.append(net)
    if allowed_nets:
        for net in sorted(allowed_nets, key=len, reverse=True):
            text = str(net or "").strip()
            if text and text.casefold() not in {item.casefold() for item in names}:
                names.append(text)
    return sorted(names, key=len, reverse=True)


def _net_boundary_pattern(net: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![A-Za-z0-9_./+-])" + re.escape(net) + r"(?![A-Za-z0-9_./+-])",
        re.IGNORECASE,
    )


def _net_occurrences(text: str, known_nets: list[str]) -> list[tuple[int, int, str]]:
    occurrences: list[tuple[int, int, str]] = []
    if known_nets:
        for net in known_nets:
            for match in _net_boundary_pattern(net).finditer(text):
                occurrences.append((match.start(), match.end(), net))
    else:
        for match in _GENERIC_NET_RE.finditer(text):
            token = match.group(0).strip()
            lowered = token.casefold()
            if lowered in _EXCLUDE_NET_TOKENS:
                continue
            if re.fullmatch(r"[A-Za-z]{1,6}\d{1,5}", token):
                continue
            if _LAYER_RE.fullmatch(token):
                continue
            occurrences.append((match.start(), match.end(), token))
    occurrences.sort(key=lambda item: (item[0], item[1]))
    deduped: list[tuple[int, int, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for item in occurrences:
        span = (item[0], item[1])
        if span in seen_spans:
            continue
        seen_spans.add(span)
        deduped.append(item)
    return deduped


def _clause_start(text: str, pos: int) -> int:
    start = 0
    for match in _CLAUSE_SEPARATOR_RE.finditer(text[:pos]):
        start = match.end()
    return start


def _clause_end(text: str, pos: int) -> int:
    match = _CLAUSE_SEPARATOR_RE.search(text[pos:])
    return pos + match.start() if match else len(text)


def _group_start(text: str, pos: int) -> int:
    start = 0
    for match in _GROUP_SEPARATOR_RE.finditer(text[:pos]):
        start = match.end()
    return start


def _group_end(text: str, pos: int) -> int:
    match = _GROUP_SEPARATOR_RE.search(text[pos:])
    return pos + match.start() if match else len(text)


def _normalize_order_lines(order_lines: Any) -> list[Dict[str, Any]]:
    if isinstance(order_lines, str) and order_lines.strip():
        try:
            order_lines = json.loads(order_lines)
        except json.JSONDecodeError:
            order_lines = []
    if not isinstance(order_lines, list):
        return []
    normalized: list[Dict[str, Any]] = []
    for index, item in enumerate(order_lines):
        if not isinstance(item, dict):
            continue
        net = str(item.get("net") or "").strip()
        layer = str(item.get("layer") or "").strip()
        if not net or not layer:
            continue
        try:
            order = int(item.get("order", index + 1))
        except (TypeError, ValueError):
            order = index + 1
        normalized.append({"net": net, "layer": layer, "order": max(1, order)})
    normalized.sort(key=lambda item: item.get("order", 0))
    for index, item in enumerate(normalized, start=1):
        item["order"] = index
    return normalized


def parse_natural_language_order_lines(
    text: Any,
    fallback_order_lines: Any = None,
    *,
    allowed_nets: Optional[set[str]] = None,
) -> list[Dict[str, Any]]:
    """Parse explicit net-to-layer assignments from Chinese/English user text."""
    source = _as_text(text)
    if not source.strip() or not _LAYER_RE.search(source):
        return []

    known_nets = _known_net_names(fallback_order_lines, allowed_nets=allowed_nets)
    occurrences = _net_occurrences(source, known_nets)
    if not occurrences:
        return []

    layer_matches = list(_LAYER_RE.finditer(source))
    parsed: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, layer_match in enumerate(layer_matches):
        layer = _normalize_layer(layer_match.group(0))
        clause_start = _clause_start(source, layer_match.start())
        clause_end = _clause_end(source, layer_match.end())
        group_start = _group_start(source, layer_match.start())
        group_end = _group_end(source, layer_match.end())
        window_start = max(clause_start, group_start)
        window_end = min(clause_end, group_end)

        window_nets = [item for item in occurrences if window_start <= item[0] < window_end]
        if not window_nets:
            window_nets = [item for item in occurrences if clause_start <= item[0] < clause_end]
        for _start, _end, net in window_nets:
            key = net.casefold()
            if key in seen:
                continue
            seen.add(key)
            parsed.append({"net": net, "layer": layer, "order": len(parsed) + 1})

    return parsed


def merge_explicit_order_lines(fallback_order_lines: Any, explicit_order_lines: Any) -> list[Dict[str, Any]]:
    """Overlay explicit user assignments first, then append remaining fallback nets."""
    explicit = _normalize_order_lines(explicit_order_lines)
    fallback = _normalize_order_lines(fallback_order_lines)
    if not explicit:
        return fallback

    fallback_by_key = {str(item.get("net") or "").casefold(): item for item in fallback}
    merged: list[Dict[str, Any]] = []
    used: set[str] = set()
    for item in explicit:
        key = str(item.get("net") or "").casefold()
        base = fallback_by_key.get(key, {})
        net = str(base.get("net") or item.get("net") or "").strip()
        layer = str(item.get("layer") or base.get("layer") or "").strip()
        if not net or not layer or key in used:
            continue
        used.add(key)
        merged.append({"net": net, "layer": layer, "order": len(merged) + 1})

    for item in fallback:
        key = str(item.get("net") or "").casefold()
        if key in used:
            continue
        used.add(key)
        merged.append({"net": item["net"], "layer": item["layer"], "order": len(merged) + 1})
    return merged
