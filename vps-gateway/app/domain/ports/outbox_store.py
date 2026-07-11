"""OutboxStore 端口接口。

数据合同来源：架构文档 6.2 OutboxStore。

端口接口隔离领域层和 IO 层。
SQLiteOutboxStore 是适配器实现。
"""
from __future__ import annotations

import abc

from app.domain.models.outbox import NewOutboxMessage, OutboxMessage, OutboxPage


class OutboxStore(abc.ABC):
    """主动回合消息持久化端口。

    指令:
      1. enqueue_once 幂等写入：同一 trigger_id 重复调用返回相同行
      2. list_after 游标查询：按 cursor 升序，不自动删除
      3. limit 被 clamp 到 1..100
      4. 空页时 next_cursor 等于传入的 after_cursor
    """

    @abc.abstractmethod
    def enqueue_once(self, message: NewOutboxMessage) -> OutboxMessage:
        """幂等写入一条消息。

        数据输入: NewOutboxMessage (不含 cursor)
        数据输出: OutboxMessage (含 cursor)
        指令:
          1. INSERT ... ON CONFLICT(trigger_id) DO NOTHING
          2. SELECT WHERE trigger_id = ? → 返回已存在或刚插入的行
          3. 事务提交成功后才返回
          4. 幂等: 同一 trigger_id 重复调用返回相同行
        """
        raise NotImplementedError

    @abc.abstractmethod
    def list_after(self, after_cursor: int, limit: int) -> OutboxPage:
        """游标查询消息。

        数据输入: after_cursor (游标), limit (数量)
        数据输出: OutboxPage { items, next_cursor }
        指令:
          1. safe_limit = clamp(limit, 1, 100)
          2. SELECT * WHERE cursor > after_cursor ORDER BY cursor ASC LIMIT safe_limit
          3. next_cursor = items.last.cursor if items else after_cursor
          4. 不删除消息
        """
        raise NotImplementedError
