"""TurnRunner 主动回合分支单元测试。

验证:
  - <NO_MESSAGE> → outcome="no_message", 不写 Outbox
  - 正常文本 → outcome="message_enqueued", 写入 Outbox
  - trigger_id 幂等: 同一 trigger_id 两次调用只写一条
  - outbox_store=None 时主动回合拒绝执行
  - 上游失败 → 异常传播

数据流:
  TimerTrigger → read_all → context_builder.build → model_client.complete
    → content == "<NO_MESSAGE>" → ActiveTurnResult(no_message)
    → content (normal) → outbox_store.enqueue_once → ActiveTurnResult(message_enqueued)
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from app.application.turn_runner import TurnRunner, ActiveTurnResult
from app.domain.models.trigger import TimerTrigger
from app.domain.models.turn import PreparedTurn, ChatMessage
from app.domain.models.chat_completion import ChatCompletionResponse, Choice
from app.domain.models.sample import SampleEnvelope, SampleType
from app.domain.models.outbox import NewOutboxMessage, OutboxMessage
from app.domain.ports.sample_reader import AllSamples
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoriesData
from app.domain.models.working_state import WorkingStateData
from datetime import datetime, timezone


def _make_samples():
    """构造 AllSamples mock。"""
    return AllSamples(
        identity=SampleEnvelope(
            sample_type="identity",
            version=1, source="sample", updated_at="2025-01-01T00:00:00Z",
            data=IdentityData(
                name="test",
                self_description="test",
                values=[],
                boundaries=[],
                relationship_definition="test",
            ),
        ),
        preferences=SampleEnvelope(
            sample_type="preferences",
            version=1, source="sample", updated_at="2025-01-01T00:00:00Z",
            data=PreferencesData(
                communication_preferences=[],
                stable_likes=[],
                stable_dislikes=[],
                interaction_rules=[],
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
    """构造 ChatCompletionResponse mock。"""
    return ChatCompletionResponse(
        id="resp-001",
        object="chat.completion",
        created=1700000000,
        model="deepseek-chat",
        choices=[
            Choice(
                index=0,
                message_role="assistant",
                message_content=content,
                finish_reason="stop",
            )
        ],
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    )


def _make_turn_runner(outbox_store=None, response_content="hello"):
    """构造 TurnRunner with mocked deps。"""
    mock_sample_reader = MagicMock()
    mock_sample_reader.read_all.return_value = _make_samples()

    mock_context_builder = MagicMock()
    mock_context_builder.build.return_value = PreparedTurn(
        messages=[ChatMessage(role="system", content="ctx"), ChatMessage(role="user", content="go")],
        sample_versions={"identity": 1, "preferences": 1, "memories": 1, "working_state": 1},
    )

    mock_model_client = MagicMock()
    mock_model_client.complete.return_value = _make_response(response_content)

    runner = TurnRunner(
        sample_reader=mock_sample_reader,
        context_builder=mock_context_builder,
        model_client=mock_model_client,
        outbox_store=outbox_store,
    )
    return runner


class TestActiveTurnNoMessage:
    """<NO_MESSAGE> → outcome="no_message"。"""

    def test_no_message_skips_outbox(self):
        """模型返回 <NO_MESSAGE> → 不写 Outbox。"""
        mock_outbox = MagicMock()
        runner = _make_turn_runner(outbox_store=mock_outbox, response_content="<NO_MESSAGE>")

        trigger = TimerTrigger(
            trigger_id="timer:2025-01-01T00:00:00Z",
            fired_at="2025-01-01T00:00:00Z",
            instruction="check and respond if needed",
        )

        result = runner.run(trigger)

        assert isinstance(result, ActiveTurnResult)
        assert result.outcome == "no_message"
        assert result.trigger_id == "timer:2025-01-01T00:00:00Z"
        # outbox_store.enqueue_once 不应被调用
        mock_outbox.enqueue_once.assert_not_called()


class TestActiveTurnEnqueuesMessage:
    """正常文本 → outcome="message_enqueued"。"""

    def test_normal_text_enqueues(self):
        """模型返回正常文本 → 写入 Outbox。"""
        mock_outbox = MagicMock()
        saved_msg = OutboxMessage(
            cursor=1, event_id="evt-001",
            trigger_id="timer:slot-1", created_at="2025-01-01T00:05:00Z",
            content="你好", metadata={},
        )
        mock_outbox.enqueue_once.return_value = saved_msg

        runner = _make_turn_runner(outbox_store=mock_outbox, response_content="你好")

        trigger = TimerTrigger(
            trigger_id="timer:slot-1",
            fired_at="2025-01-01T00:00:00Z",
            instruction="check",
        )

        result = runner.run(trigger)

        assert result.outcome == "message_enqueued"
        assert result.event_id == "evt-001"
        assert result.trigger_id == "timer:slot-1"

        # 验证 enqueue_once 被调用
        mock_outbox.enqueue_once.assert_called_once()
        arg = mock_outbox.enqueue_once.call_args[0][0]
        assert isinstance(arg, NewOutboxMessage)
        assert arg.trigger_id == "timer:slot-1"
        assert arg.content == "你好"
        assert arg.metadata["model"] == "deepseek-chat"
        assert arg.metadata["upstream_response_id"] == "resp-001"

    def test_whitespace_only_content_still_enqueues(self):
        """只有空格的 content 不是 <NO_MESSAGE>, 应写入。"""
        mock_outbox = MagicMock()
        saved_msg = OutboxMessage(
            cursor=1, event_id="e", trigger_id="t",
            created_at="c", content="  ", metadata={},
        )
        mock_outbox.enqueue_once.return_value = saved_msg

        runner = _make_turn_runner(outbox_store=mock_outbox, response_content="  ")

        trigger = TimerTrigger(
            trigger_id="timer:ws-1", fired_at="2025-01-01T00:00:00Z", instruction="x",
        )
        result = runner.run(trigger)
        # "  ".strip() = "" which is not "<NO_MESSAGE>", so it enqueues
        assert result.outcome == "message_enqueued"


class TestActiveTurnIdempotent:
    """trigger_id 幂等。"""

    def test_same_trigger_id_twice(self):
        """同一 trigger_id 调用两次 → Outbox 只写一次 (幂等由 OutboxStore 保证)。"""
        mock_outbox = MagicMock()
        saved_msg = OutboxMessage(
            cursor=1, event_id="evt-001", trigger_id="timer:slot-1",
            created_at="c", content="hello", metadata={},
        )
        mock_outbox.enqueue_once.return_value = saved_msg

        runner = _make_turn_runner(outbox_store=mock_outbox, response_content="hello")

        trigger = TimerTrigger(
            trigger_id="timer:slot-1", fired_at="2025-01-01T00:00:00Z", instruction="x",
        )

        r1 = runner.run(trigger)
        r2 = runner.run(trigger)

        # TurnRunner 调用 enqueue_once 两次, 但 OutboxStore 保证幂等
        assert mock_outbox.enqueue_once.call_count == 2
        assert r1.event_id == r2.event_id == "evt-001"
        assert r1.outcome == r2.outcome == "message_enqueued"


class TestActiveTurnNoOutboxStore:
    """outbox_store=None 时主动回合拒绝执行。"""

    def test_raises_runtime_error(self):
        """outbox_store=None → RuntimeError。"""
        runner = _make_turn_runner(outbox_store=None, response_content="hello")

        trigger = TimerTrigger(
            trigger_id="timer:slot-1", fired_at="2025-01-01T00:00:00Z", instruction="x",
        )

        with pytest.raises(RuntimeError, match="outbox_store is required"):
            runner.run(trigger)


class TestActiveTurnUpstreamError:
    """上游失败 → 异常传播。"""

    def test_upstream_error_propagates(self):
        """UpstreamError → 异常传播。"""
        from app.domain.models.errors import UpstreamError

        mock_outbox = MagicMock()
        mock_sample_reader = MagicMock()
        mock_sample_reader.read_all.return_value = _make_samples()

        mock_context_builder = MagicMock()
        mock_context_builder.build.return_value = PreparedTurn(
            messages=[ChatMessage(role="user", content="go")],
            sample_versions={},
        )

        mock_model_client = MagicMock()
        mock_model_client.complete.side_effect = UpstreamError(500, "provider down")

        runner = TurnRunner(
            sample_reader=mock_sample_reader,
            context_builder=mock_context_builder,
            model_client=mock_model_client,
            outbox_store=mock_outbox,
        )

        trigger = TimerTrigger(
            trigger_id="timer:slot-err", fired_at="2025-01-01T00:00:00Z", instruction="x",
        )

        with pytest.raises(UpstreamError):
            runner.run(trigger)
