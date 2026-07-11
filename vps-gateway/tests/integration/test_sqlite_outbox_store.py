"""SQLiteOutboxStore 集成测试。

验证基线：架构文档 12.3
  - enqueue_once 正常写入 + 幂等
  - list_after 游标分页 + limit clamp + 空页
  - 重启后持久化
  - trigger_id 唯一约束
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app.domain.models.outbox import NewOutboxMessage
from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore


@pytest.fixture
def store(tmp_path):
    """临时 SQLite 文件。"""
    db_path = str(tmp_path / "test_outbox.db")
    s = SQLiteOutboxStore(db_path)
    yield s
    s.close()


def _make_message(trigger_id: str, content: str = "hello", event_id: str = None):
    import uuid
    return NewOutboxMessage(
        event_id=event_id or str(uuid.uuid4()),
        trigger_id=trigger_id,
        created_at="2025-01-01T00:05:00Z",
        content=content,
        metadata={"model": "deepseek-chat", "sample_versions": {"identity": 1}},
    )


class TestEnqueueOnce:
    """enqueue_once: 幂等写入。"""

    def test_normal_write(self, store):
        """正常写入 → 返回 OutboxMessage 含 cursor。"""
        msg = _make_message("timer:slot-1", "你好")
        result = store.enqueue_once(msg)
        assert result.cursor >= 1
        assert result.event_id == msg.event_id
        assert result.trigger_id == "timer:slot-1"
        assert result.content == "你好"
        assert result.metadata["model"] == "deepseek-chat"

    def test_idempotent_same_trigger_id(self, store):
        """同一 trigger_id 调用两次 → 返回相同行（幂等）。"""
        msg1 = _make_message("timer:slot-1", "first", event_id="evt-001")
        result1 = store.enqueue_once(msg1)

        # 第二次用不同的 event_id, 但相同 trigger_id
        msg2 = _make_message("timer:slot-1", "second", event_id="evt-002")
        result2 = store.enqueue_once(msg2)

        # 幂等: 返回第一条
        assert result2.cursor == result1.cursor
        assert result2.event_id == result1.event_id
        assert result2.content == "first"  # 不覆盖

    def test_different_trigger_ids(self, store):
        """不同 trigger_id → 各自独立。"""
        msg1 = _make_message("timer:slot-1", "first")
        msg2 = _make_message("timer:slot-2", "second")
        r1 = store.enqueue_once(msg1)
        r2 = store.enqueue_once(msg2)
        assert r1.cursor != r2.cursor
        assert r1.content == "first"
        assert r2.content == "second"

    def test_metadata_json_roundtrip(self, store):
        """metadata dict → JSON → dict 往返不丢失。"""
        msg = _make_message("timer:slot-meta")
        msg = NewOutboxMessage(
            event_id=msg.event_id,
            trigger_id=msg.trigger_id,
            created_at=msg.created_at,
            content=msg.content,
            metadata={"model": "deepseek-chat", "versions": {"a": 1, "b": 2}, "list": [1, 2, 3]},
        )
        result = store.enqueue_once(msg)
        assert result.metadata["versions"]["a"] == 1
        assert result.metadata["list"] == [1, 2, 3]


class TestListAfter:
    """list_after: 游标查询。"""

    def test_empty_outbox(self, store):
        """空表查询 → items=[], next_cursor=0。"""
        page = store.list_after(0, 20)
        assert page.items == []
        assert page.next_cursor == 0

    def test_returns_all_after_cursor(self, store):
        """写入 3 条, list_after(0) 返回全部。"""
        for i in range(3):
            store.enqueue_once(_make_message(f"timer:slot-{i}", f"msg-{i}"))

        page = store.list_after(0, 20)
        assert len(page.items) == 3
        assert page.items[0].content == "msg-0"
        assert page.items[2].content == "msg-2"

    def test_pagination_no_duplicates(self, store):
        """分页: 5 条消息, limit=2, 三次查询无重复无遗漏。"""
        for i in range(5):
            store.enqueue_once(_make_message(f"timer:slot-{i}", f"msg-{i}"))

        page1 = store.list_after(0, 2)
        assert len(page1.items) == 2
        assert page1.next_cursor == page1.items[-1].cursor

        page2 = store.list_after(page1.next_cursor, 2)
        assert len(page2.items) == 2
        assert page2.next_cursor == page2.items[-1].cursor

        page3 = store.list_after(page2.next_cursor, 2)
        assert len(page3.items) == 1

        # 无重复
        all_cursors = [m.cursor for m in page1.items + page2.items + page3.items]
        assert len(all_cursors) == len(set(all_cursors))
        assert len(all_cursors) == 5

    def test_limit_clamped_to_100(self, store):
        """limit=999 → 至多返回 100 条。"""
        for i in range(5):
            store.enqueue_once(_make_message(f"timer:slot-{i}", f"msg-{i}"))

        page = store.list_after(0, 999)
        assert len(page.items) == 5  # 只有 5 条, 不超过 100

    def test_limit_clamped_to_min_1(self, store):
        """limit=0 → 至少返回 1 条。"""
        store.enqueue_once(_make_message("timer:slot-1", "msg"))

        page = store.list_after(0, 0)
        assert len(page.items) == 1

    def test_empty_page_keeps_cursor(self, store):
        """空页时 next_cursor = 传入的 after_cursor。"""
        page = store.list_after(9999, 20)
        assert page.items == []
        assert page.next_cursor == 9999

    def test_cursor_ascending_order(self, store):
        """消息按 cursor 升序。"""
        for i in range(5):
            store.enqueue_once(_make_message(f"timer:slot-{i}", f"msg-{i}"))

        page = store.list_after(0, 100)
        cursors = [m.cursor for m in page.items]
        assert cursors == sorted(cursors)


class TestPersistence:
    """持久化验证。"""

    def test_restart_persists(self, tmp_path):
        """关闭后重新打开 → 数据仍在。"""
        db_path = str(tmp_path / "persist.db")

        # 写入 3 条
        s1 = SQLiteOutboxStore(db_path)
        for i in range(3):
            s1.enqueue_once(_make_message(f"timer:slot-{i}", f"msg-{i}"))
        s1.close()

        # 重新打开
        s2 = SQLiteOutboxStore(db_path)
        page = s2.list_after(0, 100)
        assert len(page.items) == 3
        assert page.items[0].content == "msg-0"
        assert page.items[2].content == "msg-2"

        # cursor 连续
        cursors = [m.cursor for m in page.items]
        assert cursors == list(range(1, 4))
        s2.close()

    def test_idempotent_after_restart(self, tmp_path):
        """重启后同一 trigger_id 仍然幂等。"""
        db_path = str(tmp_path / "idem.db")

        s1 = SQLiteOutboxStore(db_path)
        msg = _make_message("timer:slot-1", "original")
        r1 = s1.enqueue_once(msg)
        s1.close()

        s2 = SQLiteOutboxStore(db_path)
        msg2 = _make_message("timer:slot-1", "different", event_id="different-evt")
        r2 = s2.enqueue_once(msg2)
        assert r2.cursor == r1.cursor
        assert r2.content == "original"
        assert r2.event_id == r1.event_id
        s2.close()
