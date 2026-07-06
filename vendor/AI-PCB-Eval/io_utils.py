from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Iterable


def dump_jsonl(path: Path, rows: Iterable[object]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            if is_dataclass(row):
                payload = asdict(row)
            else:
                payload = row
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
