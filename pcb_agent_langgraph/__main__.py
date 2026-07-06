from __future__ import annotations

from pcb_agent_langgraph.websocket.server import main


# ====== 功能：作为 PyInstaller exe 和 python -m pcb_agent_langgraph 的统一入口。 ======
if __name__ == "__main__":
    main()
