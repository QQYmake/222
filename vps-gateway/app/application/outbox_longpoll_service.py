"""OutboxLongPollService: 单读取者 Outbox 长轮询服务。

数据合同来源：架构文档 6.16 OutboxLongPollService + 5.7 LongPollRequest/Response。

职责：
  1. 非阻塞获取 SingleReaderGate；失败返回 reader_busy
  2. 计算绝对 deadline = monotonic_now + timeout
  3. 循环：先读取 notification_version
  4. 再执行 claim_one；有消息立即返回
  5. 无消息：计算 remaining
  6. remaining <= 0：返回超时空结果
  7. remaining > 0：wait_for_change
  8. 被通知但没有符合 after 的 pending 消息：继续循环
  9. 等待期间不持有 SQLite 连接或事务
 10. 释放 SingleReaderGate
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from app.domain.models.outbox import OutboxMessage
from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
from app.adapters.outbox.in_process_notifier import InProcessNotifier
from app.application.single_reader_gate import SingleReaderGate
from app.infrastructure.logging import get_logger


@dataclass(frozen=True)
class LongPollResponse:
    """长轮询响应。

    数据合同来源：架构文档 5.7 LongPollResponse。
    """
    item: Optional[OutboxMessage]
    next_cursor: int
    timed_out: bool
    reader_busy: bool = False


class OutboxLongPollService:
    """Outbox 长轮询服务。

    数据输入: after, timeout_seconds, reader_id
    数据输出: LongPollResponse
    指令:
      1. 非阻塞获取 Gate；失败返回 reader_busy
      2. deadline = monotonic_now + timeout
      3. 循环：先读 version → claim_one → 有消息返回 / 无消息 wait_for_change
      4. 等待期间不持有连接
      5. 释放 Gate
    """

    def __init__(
        self,
        store: SQLiteOutboxStore,
        notifier: InProcessNotifier,
        gate: SingleReaderGate,
        default_timeout_seconds: int = 30,
    ):
        self._store = store
        self._notifier = notifier
        self._gate = gate
        self._default_timeout = default_timeout_seconds
        self._logger = get_logger("outbox_longpoll")

    async def long_poll(
        self,
        after: int,
        timeout_seconds: int,
        reader_id: str,
    ) -> LongPollResponse:
        """执行长轮询。

        数据输入: after (cursor), timeout_seconds, reader_id
        数据输出: LongPollResponse
        指令:
          1. try_acquire Gate
          2. 循环 claim → wait → claim
          3. 释放 Gate
        """
        # 非阻塞获取 Gate
        if not await self._gate.try_acquire():
            self._logger.debug("longpoll_reader_busy", extra={"reader_id": reader_id})
            return LongPollResponse(
                item=None,
                next_cursor=after,
                timed_out=False,
                reader_busy=True,
            )

        try:
            deadline = time.monotonic() + timeout_seconds

            while True:
                # 先读取通知版本
                notification_version = self._notifier.current_version()

                # 短连接 claim_one（不持有连接等待）
                claimed = await self._store.claim_one(after_cursor=after, reader_id=reader_id)

                if claimed is not None:
                    self._logger.debug("longpoll_claimed", extra={
                        "cursor": claimed.cursor,
                        "reader_id": reader_id,
                    })
                    return LongPollResponse(
                        item=claimed,
                        next_cursor=claimed.cursor,
                        timed_out=False,
                    )

                # 无消息：计算剩余时间
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return LongPollResponse(
                        item=None,
                        next_cursor=after,
                        timed_out=True,
                    )

                # 等待通知（不持有 SQLite 连接）
                woken = await self._notifier.wait_for_change(
                    notification_version,
                    timeout_seconds=int(remaining) + 1,
                )

                if not woken:
                    # 超时
                    return LongPollResponse(
                        item=None,
                        next_cursor=after,
                        timed_out=True,
                    )

                # 被唤醒，继续循环重新 claim_one
                # 不重置 deadline
                continue

        finally:
            self._gate.release()
