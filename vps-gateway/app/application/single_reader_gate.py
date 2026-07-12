"""SingleReaderGate: 保证同时只有一个活动 Outbox 长轮询请求。

数据合同来源：架构文档 2.8 SingleReaderGate + 6.16 OutboxLongPollService。

职责：
  - 非阻塞获取；失败立即返回 False
  - 禁止等待 Gate
  - 释放后可再次获取
"""
from __future__ import annotations

import asyncio


class SingleReaderGate:
    """单读取者门控。

    数据输入: 非阻塞获取请求
    数据输出: bool (True=获取成功, False=已占用)
    指令:
      1. Gate 空闲：立即占用并返回 True
      2. Gate 已占用：立即返回 False
      3. 禁止等待 Gate
    """

    def __init__(self):
        self._acquired: bool = False

    async def try_acquire(self) -> bool:
        """非阻塞尝试获取。"""
        if self._acquired:
            return False
        self._acquired = True
        return True

    def release(self) -> None:
        """释放 Gate。"""
        self._acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._acquired
