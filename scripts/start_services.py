"""双进程一键启动器。

先启动 AgentServer，等待其 stderr 出现就绪信号（"listening on"）后再启动
Gateway（后者在启动时连接 AgentServer）。用日志就绪信号而非 TCP 端口探测：
裸 TCP 探测会让 websockets server 把探测连接当作握手失败打 ERROR 堆栈
（connect 后不发字节即 EOF，parse 阶段报 InvalidMessage）——属噪音但会
误导排查（HTTP/1.0 与 HTTP/1.1 探测在 websockets 16 同样报 ERROR）。
读日志零连接、零噪音，且不向 AgentServer 引入 HTTP 健康端点（保持其
channel-agnostic）。

Vite dev server（web/）需单独启动：`cd web && npm run dev`。
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time

# (module, ready-marker-substring) — marker 为 None 表示不等（立即启动下一个）。
# marker 在子进程 stderr 日志行中出现即视为就绪。
# AgentServer: server.py:245 `log.info("AgentServer listening on %s:%s", ...)`
ORDERED = [
    ("twinkle.agentserver", "listening on"),
    ("twinkle.gateway", None),
]

_READY_TIMEOUT = 10.0


def _stream_stderr(proc: subprocess.Popen, marker: str | None, ready: threading.Event) -> None:
    """持续读子进程 stderr，原样转发到本进程 stderr；看到 marker 即置 ready。

    整个进程生命周期持续转发（不止就绪前），使子进程日志与原来直接继承
    stderr 时一样可见。daemon 线程，主进程退出时自动结束。
    """
    assert proc.stderr is not None
    for raw in iter(proc.stderr.readline, ""):
        if not raw:
            break
        line = raw.rstrip("\n")
        print(line, file=sys.stderr, flush=True)
        if marker and not ready.is_set() and marker in line:
            ready.set()


def _wait_for_ready(proc: subprocess.Popen, module: str, marker: str | None,
                    timeout: float = _READY_TIMEOUT) -> bool:
    """启动 stderr 转发线程；marker 非空时等其出现就绪信号。

    stderr 线程必须始终启动（即便 marker 为 None，如 gateway）：否则子进程
    stderr PIPE 缓冲写满后，其 logging.emit 会阻塞，拖垮事件循环。marker
    为 None 时不等待，立即返回。
    """
    ready = threading.Event()
    threading.Thread(
        target=_stream_stderr, args=(proc, marker, ready), daemon=True
    ).start()
    if marker is None:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.wait(timeout=0.2):
            print(f"[start_services] {module} ready (signal: {marker!r})")
            return True
        if proc.poll() is not None:
            print(f"[start_services] {module} exited before ready")
            return False
    print(f"[start_services] {module} did not signal {marker!r} in time")
    return False


def main() -> None:
    # 前台/后台均按行刷新 stdout，使启动状态行及时可见（pipe 默认 block-buffered）。
    sys.stdout.reconfigure(line_buffering=True)
    py = sys.executable
    procs: list[subprocess.Popen] = []

    def stop(*_):
        for p in procs:
            p.terminate()
        raise SystemExit(0)

    for module, marker in ORDERED:
        proc = subprocess.Popen(
            [py, "-m", module],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        if not _wait_for_ready(proc, module, marker):
            stop()
    print(f"[start_services] started {[m for m, _ in ORDERED]} (pids: {[p.pid for p in procs]})")

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
