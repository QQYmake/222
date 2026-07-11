"""测试 TurnRunner 编排逻辑。"""
import pytest
from unittest.mock import MagicMock

from app.application.turn_runner import TurnRunner
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.sample import SampleReadError, SampleEnvelope
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.memories import MemoriesData
from app.domain.models.working_state import WorkingStateData
from app.domain.models.turn import PreparedTurn, ChatMessage
from app.domain.models.chat_completion import ChatCompletionResponse, Choice
from app.domain.models.errors import UpstreamError
from app.domain.ports.sample_reader import AllSamples


def _make_samples():
    identity_data = IdentityData(
        name="沉", self_description="desc",
        values=[], boundaries=[], relationship_definition="rel",
    )
    preferences_data = PreferencesData(
        communication_preferences=[], stable_likes=[],
        stable_dislikes=[], interaction_rules=[],
    )
    memories_data = MemoriesData(items=[])
    working_state_data = WorkingStateData(
        current_focus=[], emotion_summary="",
        pending_items=[], next_wake_at=None,
    )

    def env(data, v=1):
        return SampleEnvelope(
            sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00",
            source="sample", data=data,
        )

    return AllSamples(
        identity=env(identity_data, 1),
        preferences=env(preferences_data, 1),
        memories=env(memories_data, 1),
        working_state=env(working_state_data, 1),
    )


def _make_response(content="hello"):
    return ChatCompletionResponse(
        id="resp-1",
        object="chat.completion",
        created=1234567890,
        model="gpt-4",
        choices=[Choice(index=0, message_role="assistant", message_content=content, finish_reason="stop")],
        usage=None,
    )


class TestRunPassiveTurn:
    def test_calls_in_order(self):
        mock_reader = MagicMock()
        mock_reader.read_all.return_value = _make_samples()

        mock_builder = MagicMock()
        mock_builder.build.return_value = PreparedTurn(
            messages=[ChatMessage(role="system", content="sys")],
            sample_versions={"identity": 1, "preferences": 1, "memories": 1, "working_state": 1},
        )

        mock_model = MagicMock()
        mock_model.complete.return_value = _make_response()

        runner = TurnRunner(mock_reader, mock_builder, mock_model)
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )

        result = runner.run(trigger)

        mock_reader.read_all.assert_called_once()
        mock_builder.build.assert_called_once()
        mock_model.complete.assert_called_once()
        assert isinstance(result, ChatCompletionResponse)

    def test_sample_read_error_propagates(self):
        mock_reader = MagicMock()
        mock_reader.read_all.side_effect = SampleReadError("identity", "missing")

        mock_builder = MagicMock()
        mock_model = MagicMock()

        runner = TurnRunner(mock_reader, mock_builder, mock_model)
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )

        with pytest.raises(SampleReadError):
            runner.run(trigger)

        mock_builder.build.assert_not_called()
        mock_model.complete.assert_not_called()

    def test_upstream_error_propagates(self):
        mock_reader = MagicMock()
        mock_reader.read_all.return_value = _make_samples()

        mock_builder = MagicMock()
        mock_builder.build.return_value = PreparedTurn(
            messages=[ChatMessage(role="system", content="sys")],
            sample_versions={"identity": 1, "preferences": 1, "memories": 1, "working_state": 1},
        )

        mock_model = MagicMock()
        mock_model.complete.side_effect = UpstreamError(500, "server error")

        runner = TurnRunner(mock_reader, mock_builder, mock_model)
        trigger = UserTrigger(
            request_id="r1",
            chat_request={"model": "gpt-4", "messages": [
                {"role": "user", "content": "hi"},
            ]},
        )

        with pytest.raises(UpstreamError):
            runner.run(trigger)

    def test_temperature_from_request(self):
        mock_reader = MagicMock()
        mock_reader.read_all.return_value = _make_samples()

        mock_builder = MagicMock()
        mock_builder.build.return_value = PreparedTurn(
            messages=[ChatMessage(role="system", content="sys")],
            sample_versions={"identity": 1, "preferences": 1, "memories": 1, "working_state": 1},
        )

        mock_model = MagicMock()
        mock_model.complete.return_value = _make_response()

        runner = TurnRunner(mock_reader, mock_builder, mock_model)
        trigger = UserTrigger(
            request_id="r1",
            chat_request={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.3,
            },
        )

        runner.run(trigger)

        call_args = mock_model.complete.call_args
        assert call_args.args[0].temperature == 0.3

    def test_max_output_tokens_from_request(self):
        mock_reader = MagicMock()
        mock_reader.read_all.return_value = _make_samples()

        mock_builder = MagicMock()
        mock_builder.build.return_value = PreparedTurn(
            messages=[ChatMessage(role="system", content="sys")],
            sample_versions={"identity": 1, "preferences": 1, "memories": 1, "working_state": 1},
        )

        mock_model = MagicMock()
        mock_model.complete.return_value = _make_response()

        runner = TurnRunner(mock_reader, mock_builder, mock_model)
        trigger = UserTrigger(
            request_id="r1",
            chat_request={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 500,
            },
        )

        runner.run(trigger)

        call_args = mock_model.complete.call_args
        assert call_args.args[0].max_output_tokens == 500


class TestRunTimerTurn:
    def test_active_turn_requires_outbox_store(self):
        """主动回合在 outbox_store=None 时应拒绝执行。"""
        mock_reader = MagicMock()
        mock_builder = MagicMock()
        mock_model = MagicMock()

        runner = TurnRunner(mock_reader, mock_builder, mock_model, outbox_store=None)
        trigger = TimerTrigger(
            trigger_id="timer:t1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查",
        )

        with pytest.raises(RuntimeError, match="outbox_store"):
            runner.run(trigger)
