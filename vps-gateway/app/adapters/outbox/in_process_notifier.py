"""InProcessNotifier: 进程内通知器实现。

数据合同来源：架构文档 6.15 OutboxNotifier。

职责：
  - 只保存进程内通知版本，不保存消息正文
  - commit 成功后才 notify
  - 唤醒当前长轮询等待者

服务重启时通知丢失不影响数据；长轮询首先查询 SQLite，可以发现重启前的 pending 消息。
"""
from __future__ import annotations

import asyncio
from app.infrastructure.logging import get_logger


class InProcessNotifier:
    """进程内通知器。

    数据输入: cursor (已成功提交的消息 cursor)
    数据输出: 通知信号（通过 asyncio.Event / version 比对）
    指令:
      1. notify_committed 递增 version 并 set event
      2. wait_for_change 比对 version，不同则立即返回 True，相同则等待 event
      3. 超时返回 False
    """

    def __init__(self):
        self._version: int = 0
        self._event = asyncio.Event()
        self._logger = get_logger("outbox_notifier")

    def current_version(self) -> int:
        """返回当前通知版本。"""
        return self._version

    def notify_committed(self, cursor: int) -> None:
        """通知已有新消息提交。

        数据输入: cursor (已 commit 的消息 cursor)
        指令:
          1. 递增 version
          2. set event 唤醒所有等待者
        """
        self._version += 1
        self._event.set()
        self._event = asyncio.Event()  # 重置 event 供下一轮等待
        self._logger.debug("outbox_notified", extra={"cursor": cursor, "version": self._version})

    async def wait_for_change(self, after_version: int, timeout_seconds: int) -> bool:
        """等待通知版本变化。

        数据输入: after_version, timeout_seconds
        数据输出: bool (True=被唤醒, False=超时)
        指令:
          1. 如果 current_version > after_version: 立即返回 True
          2. 否则等待 event 或超时
        """
        if self._version > after_version:
            return True

        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False
