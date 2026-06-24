"""Shared normalization for PCB workflow control utterances."""

from __future__ import annotations

import re
from typing import Optional

_CONFIRM_RE = re.compile(r"(确认|继续|开始|执行|\b(?:go|yes|ok)\b)", re.IGNORECASE)
_REJECT_RE = re.compile(
    r"(拒绝|放弃|跳过|不导入|不要导入|不要|取消执行|declin|reject|skip)",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"(取消|退出|中止|停止|不做了|结束当前流程|cancel|abort|exit)", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"(回到上一步|上一步|rollback|回退)", re.IGNORECASE)
_REROUTE_AGAIN_RE = re.compile(r"(再\s*(?:reroute|重布|拆线重布)|重新\s*(?:reroute|重布|拆线重布))", re.IGNORECASE)
_IMPORT_RE = re.compile(r"(导入|import)", re.IGNORECASE)

_EXACT_CONFIRM_TOKENS = {"queren", "quren", "jixu"}
_EXACT_REJECT_TOKENS = {"jvjue", "jujue", "buyao"}


def _compact(text: str) -> str:
    return re.sub(r"[\s，。！？?!（）()、,.:;`'\"_\-]+", "", str(text or "").strip().lower())


def matches_confirm_signal(text: str) -> bool:
    raw = str(text or "")
    if _CONFIRM_RE.search(raw):
        return True
    return _compact(raw) in _EXACT_CONFIRM_TOKENS


def matches_reject_signal(text: str) -> bool:
    raw = str(text or "")
    if _REJECT_RE.search(raw):
        return True
    return _compact(raw) in _EXACT_REJECT_TOKENS


def matches_cancel_signal(text: str) -> bool:
    return bool(_CANCEL_RE.search(str(text or "")))


def matches_rollback_signal(text: str) -> bool:
    return bool(_ROLLBACK_RE.search(str(text or "")))


def normalize_control_action(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if _REROUTE_AGAIN_RE.search(raw):
        return "reroute_again"
    if matches_rollback_signal(raw):
        return "rollback_checkpoint"
    if matches_reject_signal(raw):
        if _IMPORT_RE.search(raw):
            return "reject_import"
        return "reject_route"
    if matches_cancel_signal(raw):
        return "cancel_flow"
    if matches_confirm_signal(raw):
        compact = _compact(raw)
        if compact in {"jixu"} or re.search(r"(继续|next)", raw, re.IGNORECASE):
            return "continue_flow"
        return "confirm_route"
    return None
