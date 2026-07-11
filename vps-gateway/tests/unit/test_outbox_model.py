"""OutboxMessage 领域模型单元测试。

验证:
  - NewOutboxMessage 构造与字段
  - OutboxMessage 含 cursor + to_dict 序列化
  - OutboxPage 含 items + next_cursor
  - 不可变性
"""
from __future__ import annotations

import json
import pytest
import dataclasses

from app.domain.models.outbox import NewOutboxMessage, OutboxMessage, OutboxPage


class TestNewOutboxMessage:
    """NewOutboxMessage — 待写入的消息。"""

    def test_construct(self):
        """正常构造，字段完整。"""
        msg = NewOutboxMessage(
            event_id="evt-001",
            trigger_id="timer:2025-01-01T00:00:00Z",
            created_at="2025-01-01T00:05:00Z",
            content="你好，这是主动消息。",
            metadata={"model": "deepseek-chat", "sample_versions": {"identity": 1}},
        )
        assert msg.event_id == "evt-001"
        assert msg.trigger_id.startswith("timer:")
        assert msg.content == "你好，这是主动消息。"
        assert msg.metadata["model"] == "deepseek-chat"

    def test_metadata_can_be_empty(self):
        """metadata 可以是空 dict。"""
        msg = NewOutboxMessage(
            event_id="evt-002",
            trigger_id="timer:slot-2",
            created_at="2025-01-01T00:05:00Z",
            content="",
            metadata={},
        )
        assert msg.metadata == {}

    def test_immutable(self):
        """frozen=True, 不可修改。"""
        msg = NewOutboxMessage(
            event_id="evt-003",
            trigger_id="timer:slot-3",
            created_at="2025-01-01T00:05:00Z",
            content="test",
            metadata={},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.content = "modified"


class TestOutboxMessage:
    """OutboxMessage — 已保存的消息（含 cursor）。"""

    def test_construct_with_cursor(self):
        """包含 cursor 字段。"""
        msg = OutboxMessage(
            cursor=42,
            event_id="evt-001",
            trigger_id="timer:slot-1",
            created_at="2025-01-01T00:05:00Z",
            content="hello",
            metadata={"model": "deepseek-chat"},
        )
        assert msg.cursor == 42

    def test_to_dict(self):
        """to_dict 序列化为 HTTP 响应格式。"""
        msg = OutboxMessage(
            cursor=1,
            event_id="evt-001",
            trigger_id="timer:slot-1",
            created_at="2025-01-01T00:05:00Z",
            content="你好",
            metadata={"model": "deepseek-chat", "sample_versions": {"identity": 1}},
        )
        d = msg.to_dict()
        assert d["cursor"] == 1
        assert d["event_id"] == "evt-001"
        assert d["trigger_id"] == "timer:slot-1"
        assert d["content"] == "你好"
        assert d["metadata"]["model"] == "deepseek-chat"
        # 确保可 JSON 序列化
        json.dumps(d)

    def test_immutable(self):
        """frozen=True, 不可修改。"""
        msg = OutboxMessage(
            cursor=1,
            event_id="e",
            trigger_id="t",
            created_at="c",
            content="x",
            metadata={},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.cursor = 99


class TestOutboxPage:
    """OutboxPage — 查询结果页。"""

    def test_construct_with_items(self):
        """包含 items 和 next_cursor。"""
        msg = OutboxMessage(
            cursor=1,
            event_id="e",
            trigger_id="t",
            created_at="c",
            content="x",
            metadata={},
        )
        page = OutboxPage(items=[msg], next_cursor=1)
        assert len(page.items) == 1
        assert page.next_cursor == 1

    def test_empty_page(self):
        """空页: items=[], next_cursor=传入的 after_cursor。"""
        page = OutboxPage(items=[], next_cursor=9999)
        assert page.items == []
        assert page.next_cursor == 9999

    def test_immutable(self):
        """frozen=True, 不可修改。"""
        page = OutboxPage(items=[], next_cursor=0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            page.next_cursor = 5
