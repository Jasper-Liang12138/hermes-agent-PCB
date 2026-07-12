from dataclasses import dataclass, field
from typing import Optional, Dict, Any


@dataclass
class Issue:
    rule: str
    severity: str
    message: str

    # 原有字段
    obj1: str = ""
    obj2: str = ""
    net: str = ""
    layer: str = ""
    x: Optional[float] = None
    y: Optional[float] = None
    category: str = ""
    suggestion: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    # 新增统一字段
    issue_id: str = ""
    component: str = ""
    pad_id: str = ""

    def normalized_issue_id(self) -> str:
        if self.issue_id:
            return self.issue_id

        key_parts = [
            self.rule or "",
            self.component or "",
            self.pad_id or self.obj1 or "",
            self.net or "",
            self.layer or "",
        ]
        key = "_".join(p.strip().replace(" ", "_") for p in key_parts if p)
        return key or "UNKNOWN_ISSUE"

    def to_dict(self):
        return {
            "issue_id": self.normalized_issue_id(),
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "obj1": self.obj1,
            "obj2": self.obj2,
            "net": self.net,
            "layer": self.layer,
            "x": self.x,
            "y": self.y,
            "category": self.category,
            "suggestion": self.suggestion,
            "component": self.component or self.extra.get("component", ""),
            "pad_id": self.pad_id or self.extra.get("pad_id", ""),
            "extra": self.extra,
        }