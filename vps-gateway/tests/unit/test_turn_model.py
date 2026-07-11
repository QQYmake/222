"""测试 PreparedTurn / ChatMessage / ModelCompletionInput 数据模型。"""
import pytest
from app.domain.models.turn import PreparedTurn, ChatMessage, ModelCompletionInput


class TestChatMessage:
    def test_system(self):
        msg = ChatMessage(role="system", content="sys prompt")
        assert msg.role == "system"
        assert msg.content == "sys prompt"

    def test_user(self):
        msg = ChatMessage(role="user", content="hello")
        assert msg.role == "user"

    def test_assistant(self):
        msg = ChatMessage(role="assistant", content="reply")
        assert msg.role == "assistant"

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
    def test_full(self):
        messages = [ChatMessage(role="system", content="sys")]
        inp = ModelCompletionInput(messages=messages, temperature=0.5, max_output_tokens=1000)
        assert inp.temperature == 0.5
        assert inp.max_output_tokens == 1000

    def test_none_values(self):
        messages = [ChatMessage(role="system", content="sys")]
        inp = ModelCompletionInput(messages=messages, temperature=None, max_output_tokens=None)
        assert inp.temperature is None
        assert inp.max_output_tokens is None
