"""Outbox 持久化与主动回合 E2E 集成测试。

验证基线：架构文档 12.3 + 12.4
  - 主动回合写入 Outbox
  - <NO_MESSAGE> 不写 Outbox
  - trigger_id 幂等
  - 重启后持久化
  - 分页无重复
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
from app.application.turn_runner import TurnRunner, ActiveTurnResult
from app.domain.models.trigger import TimerTrigger
from app.domain.models.turn import PreparedTurn, ChatMessage
from app.domain.models.chat_completion import ChatCompletionResponse, Choice
from app.domain.models.sample import SampleEnvelope
from app.domain.ports.sample_reader import AllSamples
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoriesData
from app.domain.models.working_state import WorkingStateData


def _make_samples():
    return AllSamples(
        identity=SampleEnvelope(
            sample_type="identity",
            version=1, source="sample", updated_at="2025-01-01T00:00:00Z",
            data=IdentityData(
                name="test", self_description="test",
                values=[], boundaries=[], relationship_definition="test",
            ),
        ),
        preferences=SampleEnvelope(
            sample_type="preferences",
            version=1, source="sample", updated_at="2025-01-01T00:00:00Z",
            data=PreferencesData(
                communication_preferences=[], stable_likes=[],
                stable_dislikes=[], interaction_rules=[],
            ),
        ),
        memories=SampleEnvelope(
            sample_type="memories",
            version=1, source="sample", updated_at="2025-01-01T00:00:00Z",
            data=MemoriesData(items=[]),
        ),
        working_state=SampleEnvelope(
            sample_type="working_state",
            version=1, source="sample", updated_at="2025-01-01T00:00:00Z",
            data=WorkingStateData(
                current_focus=[], pending_items=[],
                emotion_summary="calm", next_wake_at=None,
            ),
        ),
    )


def _make_response(content: str):
    return ChatCompletionResponse(
        id="resp-001", object="chat.completion", created=1700000000,
        model="deepseek-chat",
        choices=[Choice(index=0, message_role="assistant",
                        message_content=content, finish_reason="stop")],
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def _make_turn_runner(store, response_content="hello"):
    mock_reader = MagicMock()
    mock_reader.read_all.return_value = _make_samples()

    mock_builder = MagicMock()
    mock_builder.build.return_value = PreparedTurn(
        messages=[ChatMessage(role="system", content="ctx")],
        sample_versions={"identity": 1},
    )

    mock_model = MagicMock()
    mock_model.complete.return_value = _make_response(response_content)

    return TurnRunner(
        sample_reader=mock_reader,
        context_builder=mock_builder,
        model_client=mock_model,
        outbox_store=store,
    )


class TestActiveTurnWritesOutbox:
    """主动回合写入 Outbox。"""

    def test_writes_message(self, tmp_path):
        """正常文本 → 写入 Outbox。"""
        store = SQLiteOutboxStore(str(tmp_path / "e2e.db"))
        runner = _make_turn_runner(store, response_content="你好，这是主动消息。")

        trigger = TimerTrigger(
            trigger_id="timer:2025-01-01T00:00:00Z",
            fired_at="2025-01-01T00:00:00Z",
            instruction="check",
        )
        result = runner.run(trigger)

        assert result.outcome == "message_enqueued"
        assert result.event_id is not None

        page = store.list_after(0, 10)
        assert len(page.items) == 1
        assert page.items[0].content == "你好，这是主动消息。"
        assert page.items[0].trigger_id == "timer:2025-01-01T00:00:00Z"
        store.close()

    def test_no_message_skips_outbox(self, tmp_path):
        """<NO_MESSAGE> → 不写 Outbox。"""
        store = SQLiteOutboxStore(str(tmp_path / "e2e_no_msg.db"))
        runner = _make_turn_runner(store, response_content="<NO_MESSAGE>")

        trigger = TimerTrigger(
            trigger_id="timer:slot-no-msg",
            fired_at="2025-01-01T00:00:00Z",
            instruction="check",
        )
        result = runner.run(trigger)

        assert result.outcome == "no_message"
        page = store.list_after(0, 10)
        assert len(page.items) == 0
        store.close()


class TestTriggerIdIdempotent:
    """trigger_id 幂等。"""

    def test_same_trigger_id_one_row(self, tmp_path):
        """同一 trigger_id 两次调用 → Outbox 只有一条。"""
        store = SQLiteOutboxStore(str(tmp_path / "idem.db"))
        runner = _make_turn_runner(store, response_content="hello")

        trigger = TimerTrigger(
            trigger_id="timer:slot-idem",
            fired_at="2025-01-01T00:00:00Z",
            instruction="check",
        )

        r1 = runner.run(trigger)
        r2 = runner.run(trigger)

        page = store.list_after(0, 100)
        assert len(page.items) == 1
        # event_id 相同 (幂等)
        assert r1.event_id == r2.event_id == page.items[0].event_id
        store.close()


class TestOutboxPersistence:
    """重启后持久化。"""

    def test_persist_after_restart(self, tmp_path):
        """写入 3 条 → 关闭 → 重开 → 3 条仍在。"""
        db_path = str(tmp_path / "persist.db")
        store1 = SQLiteOutboxStore(db_path)

        for i in range(3):
            runner = _make_turn_runner(store1, response_content=f"msg-{i}")
            trigger = TimerTrigger(
                trigger_id=f"timer:slot-{i}",
                fired_at="2025-01-01T00:00:00Z",
                instruction="check",
            )
            runner.run(trigger)

        store1.close()

        store2 = SQLiteOutboxStore(db_path)
        page = store2.list_after(0, 100)
        assert len(page.items) == 3
        assert page.items[0].content == "msg-0"
        assert page.items[2].content == "msg-2"
        store2.close()


class TestOutboxPagination:
    """分页查询。"""

    def test_no_duplicates_no_gaps(self, tmp_path):
        """5 条消息, limit=2, 三次查询无重复无遗漏。"""
        db_path = str(tmp_path / "pagination.db")
        store = SQLiteOutboxStore(db_path)

        for i in range(5):
            runner = _make_turn_runner(store, response_content=f"msg-{i}")
            trigger = TimerTrigger(
                trigger_id=f"timer:page-{i}",
                fired_at="2025-01-01T00:00:00Z",
                instruction="check",
            )
            runner.run(trigger)

        page1 = store.list_after(0, 2)
        assert len(page1.items) == 2

        page2 = store.list_after(page1.next_cursor, 2)
        assert len(page2.items) == 2

        page3 = store.list_after(page2.next_cursor, 2)
        assert len(page3.items) == 1

        all_cursors = [m.cursor for m in page1.items + page2.items + page3.items]
        assert len(all_cursors) == 5
        assert len(set(all_cursors)) == 5
        store.close()

    def test_limit_clamped(self, tmp_path):
        """limit=999 → 至多 100 条 (SQLiteOutboxStore 内部 clamp)。"""
        store = SQLiteOutboxStore(str(tmp_path / "clamp.db"))
        # 只写 3 条, 验证 limit=999 不报错
        for i in range(3):
            runner = _make_turn_runner(store, response_content=f"msg-{i}")
            trigger = TimerTrigger(
                trigger_id=f"timer:clamp-{i}",
                fired_at="2025-01-01T00:00:00Z",
                instruction="check",
            )
            runner.run(trigger)

        page = store.list_after(0, 999)
        assert len(page.items) == 3
        store.close()

    def test_empty_page_keeps_cursor(self, tmp_path):
        """空页 → next_cursor = 传入的 after。"""
        store = SQLiteOutboxStore(str(tmp_path / "empty.db"))
        page = store.list_after(9999, 20)
        assert page.items == []
        assert page.next_cursor == 9999
        store.close()
