from __future__ import annotations

import os
from pathlib import Path
from typing import List


def load_example_pool(repo_root: Path) -> List[dict]:
    configured = os.getenv("REROUTE_EXAMPLE_DIRS", "")
    if not configured.strip():
        return []
    pool: List[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw_dir in configured.split(","):
        item = raw_dir.strip()
        if not item:
            continue
        root = Path(item)
        if not root.is_absolute():
            root = repo_root / root
        for routing_path in sorted(root.glob("*.kicad_patch")):
            routing = routing_path.read_text(encoding="utf-8", errors="replace").strip()
            if "(segment" not in routing.lower() and "(via" not in routing.lower():
                continue
            key = (routing_path.stem, routing)
            if key in seen:
                continue
            seen.add(key)
            pool.append(
                {
                    "task_id": routing_path.stem,
                    "routing": routing,
                    "path_length": 0.0,
                    "via_count": routing.lower().count("(via"),
                }
            )
    pool.sort(key=lambda value: (value["via_count"], value["path_length"], value["task_id"]))
    return pool


def select_examples(pool: List[dict], task_id: str, shots: int) -> List[dict]:
    if shots <= 0:
        return []
    selected: List[dict] = []
    used_tasks: set[str] = set()
    for item in pool:
        if item["task_id"] == task_id or item["task_id"] in used_tasks:
            continue
        selected.append(item)
        used_tasks.add(item["task_id"])
        if len(selected) >= shots:
            break
    return selected


def format_examples(examples: List[dict]) -> str:
    if not examples:
        return ""
    parts = [
        "参考样例：下面是已通过验证的 KiCad 走线对象。只学习输出格式和路径组织方式，"
        "不要复制样例中的 net id、坐标或层名到当前任务。"
    ]
    for idx, item in enumerate(examples, start=1):
        parts.append(
            "\n".join(
                [
                    f"样例 {idx}：",
                    "<answer>",
                    item["routing"],
                    "</answer>",
                ]
            )
        )
    return "\n\n".join(parts)


def candidate_quality(metrics) -> tuple[int, int, float, int, float]:
    if getattr(metrics, "status", "") != "ok":
        return (-1, -1_000_000, -1_000_000.0, -1_000_000, -1_000_000.0)
    return (
        int(metrics.success),
        -metrics.drc_violation,
        metrics.drc_backend_score,
        -metrics.via_count,
        -metrics.path_length,
    )
