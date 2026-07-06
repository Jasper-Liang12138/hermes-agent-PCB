from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
# ====== 功能：保存 live-eval 执行过程中的事件轨迹。 ======
class EvaluationTrace:
    sample_id: str
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)

    # ====== 功能：向评测 trace 添加事件。 ======
    def add(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append({"ts": time.time(), "type": event_type, "payload": payload})

    # ====== 功能：保存评测 trace 到文件。 ======
    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"sample_id": self.sample_id, "started_at": self.started_at, "events": self.events}, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    # ====== 功能：从文件加载评测 trace。 ======
    def load(cls, path: str | Path) -> "EvaluationTrace":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        trace = cls(sample_id=data["sample_id"], started_at=float(data.get("started_at") or time.time()))
        trace.events = list(data.get("events") or [])
        return trace

