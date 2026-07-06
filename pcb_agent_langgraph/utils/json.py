from __future__ import annotations

import json
from typing import Any


# ====== 功能：以中文友好的方式序列化 JSON。 ======
def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# ====== 功能：尝试把字符串解析为 JSON，否则返回原值。 ======
def loads_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


# ====== 功能：把结构化数据压缩为限定长度文本。 ======
def compact(data: Any, max_chars: int = 4000) -> str:
    text = dumps(data)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "...<truncated>"

