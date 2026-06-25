"""Live entrypoint for evaluating SWSD intent loops with the real model."""

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from evaluate_pcb_intent_agent_loops_impl import main


if __name__ == "__main__":
    raise SystemExit(main())
