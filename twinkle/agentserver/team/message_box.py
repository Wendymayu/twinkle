"""MessageBox — member 私有信箱,包 asyncio.Queue 提供 drain 便捷方法。

纯 FIFO(put/drain),不带持久化/优先级/审计(那些是 TeamMessageStore 编排语义,YAGNI 不做)。
对齐 spec §1.3 / §5.4:leader send_member → box.put;member run 循环每步 box.drain。
"""
from __future__ import annotations

import asyncio


class MessageBox:
    """Member 私有信箱:包 asyncio.Queue 提供 drain 便捷方法。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def put(self, content: str) -> None:
        """非阻塞投递一条消息(member run 时 drain)。"""
        self._queue.put_nowait(content)

    def drain(self) -> list[str]:
        """非阻塞排空,返回所有未读消息(无则空 list)。"""
        out: list[str] = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out

    def empty(self) -> bool:
        return self._queue.empty()
