"""双进程一键启动器。

先启动 AgentServer，等待其端口监听就绪后再启动 Gateway（后者在启动时连接
AgentServer）。避免并发启动竞态：若 AgentServer 尚未绑定端口，Gateway 会
遇到 ConnectionRefused。Vite dev server（web/）需单独启动：`cd web && npm run dev`。
"""
from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time

# (module, port-to-wait-for) — port 为 None 表示不等待（立即启动下一个）。
ORDERED = [
    ("twinkle.agentserver", 18000),
    ("twinkle.gateway", None),
]


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.15)
    return False


def main() -> None:
    py = sys.executable
    procs: list[subprocess.Popen] = []
    for module, wait_port in ORDERED:
        proc = subprocess.Popen([py, "-m", module])
        procs.append(proc)
        if wait_port is not None:
            if not _wait_for_port(wait_port):
                print(f"[start_services] {module} did not open :{wait_port} in time")
                stop()
            print(f"[start_services] {module} listening on :{wait_port}")
    print(f"[start_services] started {[m for m, _ in ORDERED]} (pids: {[p.pid for p in procs]})")

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
