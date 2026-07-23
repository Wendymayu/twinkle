"""End-to-end smoke: connect to the running gateway (:19000), send a
multi-step query that should trigger todo_create, and print every event
(chat.delta / chat.final / todo.update) received. Proves the full
backend->gateway->browser wire for todo progress."""
import asyncio
import json
import time

import websockets


async def main():
    uri = "ws://127.0.0.1:19000/"
    query = (
        "请帮我完成一个多步任务,并先用 todo_create 拆成子任务再逐步执行:"
        "1) 用 web_search 搜索 \"大语言模型\"; 2) 用 web_fetch 抓第一个结果页面;"
        "3) 给出一句总结。每完成一步用 todo_complete 标记。"
    )
    req = {
        "type": "req",
        "id": "smoke-1",
        "method": "chat.send",
        "params": {"query": query, "session_id": "sess_smoke"},
    }
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps(req))
        print(f"[sent] {query[:60]}...")
        deadline = time.time() + 60
        saw_todo = False
        n_delta = 0
        final_text = None
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining(deadline))
            except asyncio.TimeoutError:
                break
            frame = json.loads(raw)
            if frame.get("type") == "res":
                print(f"[ack ] ok={frame.get('ok')}")
                continue
            ev = frame.get("event")
            if ev == "chat.delta":
                n_delta += 1
                if n_delta <= 3 or n_delta % 20 == 0:
                    print(f"[delta#{n_delta}] {frame.get('payload', {}).get('content', '')[:40]}")
            elif ev == "chat.final":
                final_text = frame.get("payload", {}).get("content", "")
                print(f"[final] {final_text[:120]}")
                break
            elif ev == "todo.update":
                saw_todo = True
                p = frame.get("payload", {})
                print(f"[todo ] remaining={p.get('remaining')}/{p.get('total')} "
                      f"tasks={[ (t.get('idx'), t.get('title'), t.get('status')) for t in p.get('tasks', []) ]}")
            else:
                print(f"[? ] {frame}")
        print(f"\n=== summary: deltas={n_delta} saw_todo_update={saw_todo} "
              f"final={'yes' if final_text else 'no'} ===")


def remaining(deadline):
    return max(0.1, deadline - time.time())


asyncio.run(main())
