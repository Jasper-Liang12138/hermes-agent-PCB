from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Sequence, Tuple


CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]+)?\s*\n(.*?)```", re.DOTALL)
KICAD_S_EXPR_HINTS = (
    "(kicad_pcb",
    "(segment",
    "(via",
    "(arc",
    "(module",
    "(footprint",
    "(net ",
    "(gr_line",
    "(gr_arc",
    "(gr_text",
    "(zone",
)
KICAD_TOKEN_RE = re.compile(r"\(|\)|\"[^\"]*\"|[^\s()]+")
NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def extract_code_blocks(text: str) -> List[str]:
    return [match.strip() for match in CODE_FENCE_RE.findall(text or "") if match.strip()]


def strip_code_blocks(text: str) -> str:
    return CODE_FENCE_RE.sub(" ", text or "")


def looks_like_kicad(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in KICAD_S_EXPR_HINTS)


def extract_kicad_or_text(text: str) -> Tuple[str, bool, List[str]]:
    blocks = extract_code_blocks(text)
    kicad_blocks = [block for block in blocks if looks_like_kicad(block)]
    if kicad_blocks:
        return "\n\n".join(kicad_blocks), True, blocks
    if blocks:
        joined = "\n\n".join(blocks)
        return joined, looks_like_kicad(joined), blocks
    stripped = (text or "").strip()
    return stripped, looks_like_kicad(stripped), []


def extract_plain_text(text: str) -> str:
    stripped = strip_code_blocks(text)
    lines = [line.strip() for line in stripped.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def normalize_kicad(text: str) -> str:
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r";.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    compact = "\n".join(line for line in lines if line)

    def _normalize_number(match: re.Match[str]) -> str:
        value = float(match.group(0))
        if math.isclose(value, round(value), abs_tol=1e-9):
            return str(int(round(value)))
        return f"{value:.6f}".rstrip("0").rstrip(".")

    compact = NUMBER_RE.sub(_normalize_number, compact)
    compact = re.sub(r"\s*\(\s*", " (", compact)
    compact = re.sub(r"\s*\)\s*", ") ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact


def tokenize_kicad(text: str) -> List[str]:
    return KICAD_TOKEN_RE.findall(normalize_kicad(text).lower())


def extract_numeric_values(text: str) -> List[float]:
    values = []
    for item in NUMBER_RE.findall(text or ""):
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def extract_structural_features(text: str) -> Dict[str, int]:
    normalized = normalize_kicad(text).lower()
    features = {
        "segment": normalized.count("(segment"),
        "via": normalized.count("(via"),
        "arc": normalized.count("(arc"),
        "zone": normalized.count("(zone"),
        "net": normalized.count("(net "),
        "layer": normalized.count("(layer "),
        "start": normalized.count("(start "),
        "end": normalized.count("(end "),
        "width": normalized.count("(width "),
    }
    features["paren_balance_abs"] = abs(normalized.count("(") - normalized.count(")"))
    return features


def jaccard_similarity(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> float:
    set_a, set_b = set(tokens_a), set(tokens_b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def weighted_counter_similarity(tokens_a: Sequence[str], tokens_b: Sequence[str]) -> float:
    counter_a = Counter(tokens_a)
    counter_b = Counter(tokens_b)
    if not counter_a and not counter_b:
        return 1.0
    all_keys = set(counter_a) | set(counter_b)
    overlap = sum(min(counter_a[key], counter_b[key]) for key in all_keys)
    total = sum(max(counter_a[key], counter_b[key]) for key in all_keys)
    return overlap / total if total else 1.0


def number_similarity(values_a: Sequence[float], values_b: Sequence[float], tol: float = 1e-4) -> float:
    if not values_a and not values_b:
        return 1.0
    if not values_a or not values_b:
        return 0.0
    used = [False] * len(values_b)
    matched = 0
    for value_a in values_a:
        for idx, value_b in enumerate(values_b):
            if used[idx]:
                continue
            if abs(value_a - value_b) <= tol:
                used[idx] = True
                matched += 1
                break
    return (2 * matched) / (len(values_a) + len(values_b))


def feature_similarity(features_a: Dict[str, int], features_b: Dict[str, int]) -> float:
    keys = set(features_a) | set(features_b)
    if not keys:
        return 1.0
    penalties = []
    for key in keys:
        a = features_a.get(key, 0)
        b = features_b.get(key, 0)
        scale = max(abs(a), abs(b), 1)
        penalties.append(abs(a - b) / scale)
    return max(0.0, 1.0 - (sum(penalties) / len(penalties)))
