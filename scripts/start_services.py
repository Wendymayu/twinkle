"""One-command launcher for the two Phase 0 processes.

Starts AgentServer and Gateway as subprocesses. Ctrl-C stops both.
The Vite dev server (web/) is started separately: `cd web && npm run dev`.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time

MODULES = ["twinkle.agentserver", "twinkle.gateway"]


def main() -> None:
    py = sys.executable
    procs: list[subprocess.Popen] = [subprocess.Popen([py, "-m", m]) for m in MODULES]
    print(f"[start_services] started {MODULES} (pids: {[p.pid for p in procs]})")

    def stop(*_):
        for p in procs:
            p.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    try:
        while True:
            for p in procs:
                if p.poll() is not None:
                    print(f"[start_services] process exited (code={p.returncode})")
                    stop()
            time.sleep(0.5)
    except SystemExit:
        pass


if __name__ == "__main__":
    main()
