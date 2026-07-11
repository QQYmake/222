"""Outbox 领域模型: NewOutboxMessage, OutboxMessage, OutboxPage.

数据合同来源：架构文档 5.9 OutboxMessage / 6.2 OutboxStore。

NewOutboxMessage — 待写入的消息（不含 cursor），由 TurnRunner 构造。
OutboxMessage   — 已保存的消息（含 cursor），由 OutboxStore 返回。
OutboxPage      — 查询结果页，含 items 和 next_cursor。
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any


@dataclasses.dataclass(frozen=True)
class NewOutboxMessage:
    """待写入的消息（不含 cursor）。

    数据输入: TurnRunner 构造此对象，传入 OutboxStore.enqueue_once
    字段:
      event_id    — 全局唯一 ID
      trigger_id  — 幂等键（同一定时触发至多一条消息）
      created_at  — ISO 8601
      content     — 模型生成的文本
      metadata    — { model, sample_versions, upstream_response_id }
    """

    event_id: str
    trigger_id: str
    created_at: str
    content: str
    metadata: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class OutboxMessage:
    """已保存的消息（含 cursor）。

    数据输出: OutboxStore.enqueue_once / list_after 返回此对象
    字段:
      cursor      — SQLite 自增游标（查询用）
      event_id    — 全局唯一 ID
      trigger_id  — 幂等键
      created_at  — ISO 8601
      content     — 模型生成的文本
      metadata    — dict
    """

    cursor: int
    event_id: str
    trigger_id: str
    created_at: str
    content: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """序列化为 HTTP 响应 dict。"""
        return {
            "cursor": self.cursor,
            "event_id": self.event_id,
            "trigger_id": self.trigger_id,
            "created_at": self.created_at,
            "content": self.content,
            "metadata": self.metadata,
        }


@dataclasses.dataclass(frozen=True)
class OutboxPage:
    """查询结果页。

    数据输出: OutboxStore.list_after 返回此对象
    字段:
      items        — 消息列表
      next_cursor  — 下一页查询起点（空页时等于传入的 after_cursor）
    """

    items: list[OutboxMessage]
    next_cursor: int
