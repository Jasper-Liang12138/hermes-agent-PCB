from __future__ import annotations

import asyncio
import signal
import sys


def main() -> int:
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print("PCB Agent Gateway")
        print("")
        print("Usage:")
        print("  agent.exe")
        print("  agent.exe --help")
        return 0

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    from gateway.run import start_gateway

    print("Starting PCB Agent Gateway...")
    asyncio.run(start_gateway())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
