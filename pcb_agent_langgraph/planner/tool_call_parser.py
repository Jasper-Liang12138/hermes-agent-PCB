from __future__ import annotations

import json
import re
from typing import Any


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
CONTENT_RE = re.compile(r"<content>\s*(.*?)\s*</content>", re.DOTALL | re.IGNORECASE)


# ====== 功能：解析模型输出中的工具调用标记。 ======
def parse_tool_call_markup(text: str) -> tuple[list[dict[str, Any]], str]:
    calls: list[dict[str, Any]] = []
    for match in TOOL_CALL_RE.finditer(text or ""):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            calls.append(parsed)
        elif isinstance(parsed, list):
            calls.extend(item for item in parsed if isinstance(item, dict))

    content_match = CONTENT_RE.search(text or "")
    content = content_match.group(1).strip() if content_match else re.sub(TOOL_CALL_RE, "", text or "").strip()
    return calls, content

