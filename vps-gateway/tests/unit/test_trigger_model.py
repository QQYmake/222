"""测试 Trigger 和 Turn 数据模型。"""
import pytest
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.turn import PreparedTurn, ChatMessage, ModelCompletionInput


class TestUserTrigger:
    def test_construct(self):
        trigger = UserTrigger(
            request_id="req_001",
            chat_request={"model": "gpt-4", "messages": []},
        )
        assert trigger.type == "user"
        assert trigger.request_id == "req_001"

    def test_immutable(self):
        trigger = UserTrigger(
            request_id="req_001",
            chat_request={"model": "gpt-4", "messages": []},
        )
        with pytest.raises(Exception):
            trigger.request_id = "changed"


class TestTimerTrigger:
    def test_construct(self):
        trigger = TimerTrigger(
            trigger_id="timer:2026-07-12T09:00:00+08:00",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查当前状态",
        )
        assert trigger.type == "timer"
        assert trigger.trigger_id.startswith("timer:")

    def test_immutable(self):
        trigger = TimerTrigger(
            trigger_id="timer:slot1",
            fired_at="2026-07-12T09:00:00+08:00",
            instruction="检查",
        )
        with pytest.raises(Exception):
            trigger.trigger_id = "changed"


class TestChatMessage:
    def test_construct_system(self):
        msg = ChatMessage(role="system", content="hello")
        assert msg.role == "system"
        assert msg.content == "hello"

    def test_construct_user(self):
        msg = ChatMessage(role="user", content="hi")
        assert msg.role == "user"

    def test_immutable(self):
        msg = ChatMessage(role="user", content="hi")
        with pytest.raises(Exception):
            msg.content = "changed"


class TestPreparedTurn:
    def test_construct(self):
        messages = [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="hi"),
        ]
        versions = {"identity": 1, "preferences": 2, "memories": 0, "working_state": 0}
        turn = PreparedTurn(messages=messages, sample_versions=versions)
        assert len(turn.messages) == 2
        assert turn.sample_versions["identity"] == 1
        assert turn.messages[0].role == "system"

    def test_immutable(self):
        messages = [ChatMessage(role="system", content="sys")]
        versions = {"identity": 1}
        turn = PreparedTurn(messages=messages, sample_versions=versions)
        with pytest.raises(Exception):
            turn.messages = []


class TestModelCompletionInput:
    def test_construct_full(self):
        messages = [ChatMessage(role="system", content="sys")]
        inp = ModelCompletionInput(
            messages=messages,
            temperature=0.5,
            max_output_tokens=1000,
        )
        assert inp.temperature == 0.5
        assert inp.max_output_tokens == 1000

    def test_construct_none(self):
        messages = [ChatMessage(role="system", content="sys")]
        inp = ModelCompletionInput(
            messages=messages,
            temperature=None,
            max_output_tokens=None,
        )
        assert inp.temperature is None
        assert inp.max_output_tokens is None
