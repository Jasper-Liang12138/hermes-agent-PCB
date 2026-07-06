from __future__ import annotations

import argparse
import json
from pathlib import Path

from pcb_agent_langgraph.evaluation.trace import EvaluationTrace


# ====== 功能：读取 trace 并返回可回放状态。 ======
def replay_trace(path: str | Path) -> dict:
    trace = EvaluationTrace.load(path)
    return {"sample_id": trace.sample_id, "event_count": len(trace.events), "events": trace.events}


# ====== 功能：命令行入口函数。 ======
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    args = parser.parse_args()
    print(json.dumps(replay_trace(args.trace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

