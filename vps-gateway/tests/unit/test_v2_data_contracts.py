"""测试 v2 数据合同扩展：ChatMessage tool_calls/tool_call_id, Choice tool_calls。

验证基线：架构文档 5.1 ChatMessage, 5.3 ToolCall。
"""
import pytest
from app.domain.models.turn import ChatMessage, ModelCompletionInput
from app.domain.models.chat_completion import (
    Choice,
    ChatCompletionResponse,
    validate_chat_completion_response,
)


class TestChatMessageExtension:
    def test_chat_message_defaults_no_tool_fields(self):
        """ChatMessage 默认不含 tool_calls/tool_call_id。"""
        msg = ChatMessage(role="user", content="hello")
        assert msg.tool_calls is None
        assert msg.tool_call_id is None

    def test_chat_message_with_tool_calls(self):
        """assistant 消息可携带 tool_calls。"""
        msg = ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_server_time", "arguments": "{}"}}],
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1

    def test_chat_message_tool_with_tool_call_id(self):
        """tool 消息携带 tool_call_id。"""
        msg = ChatMessage(
            role="tool",
            content="2025-01-01T00:00:00Z",
            tool_call_id="call_1",
        )
        assert msg.tool_call_id == "call_1"


class TestModelCompletionInputExtension:
    def test_model_completion_input_with_tools(self):
        """ModelCompletionInput 可携带 tools 和 tool_choice。"""
        inp = ModelCompletionInput(
            messages=[ChatMessage(role="user", content="hi")],
            temperature=0.5,
            max_output_tokens=500,
            tools=[{"type": "function", "function": {"name": "get_server_time"}}],
            tool_choice="auto",
        )
        assert inp.tools is not None
        assert len(inp.tools) == 1
        assert inp.tool_choice == "auto"

    def test_model_completion_input_without_tools(self):
        """不传 tools 时默认 None。"""
        inp = ModelCompletionInput(
            messages=[ChatMessage(role="user", content="hi")],
            temperature=None,
            max_output_tokens=None,
        )
        assert inp.tools is None
        assert inp.tool_choice is None


class TestChoiceExtension:
    def test_choice_defaults_no_tool_calls(self):
        """Choice 默认不含 tool_calls。"""
        ch = Choice(index=0, message_role="assistant", message_content="hi", finish_reason="stop")
        assert ch.tool_calls is None

    def test_choice_with_tool_calls(self):
        """Choice 可携带 tool_calls。"""
        ch = Choice(
            index=0,
            message_role="assistant",
            message_content=None,
            finish_reason="tool_calls",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_server_time", "arguments": "{}"}}],
        )
        assert ch.tool_calls is not None
        assert len(ch.tool_calls) == 1


class TestValidateResponseWithToolCalls:
    def test_validate_response_parses_tool_calls(self):
        """validate_chat_completion_response 解析 tool_calls。"""
        parsed = {
            "id": "resp-1",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_server_time", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        result = validate_chat_completion_response(parsed)
        assert result.choices[0].tool_calls is not None
        assert result.choices[0].tool_calls[0]["id"] == "call_1"

    def test_validate_response_without_tool_calls(self):
        """无 tool_calls 的响应正常解析。"""
        parsed = {
            "id": "resp-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
        }
        result = validate_chat_completion_response(parsed)
        assert result.choices[0].tool_calls is None
        assert result.choices[0].message_content == "hello"

    def test_response_to_dict_includes_tool_calls(self):
        """to_dict() 输出 tool_calls。"""
        ch = Choice(
            index=0,
            message_role="assistant",
            message_content=None,
            finish_reason="tool_calls",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_server_time", "arguments": "{}"}}],
        )
        resp = ChatCompletionResponse(
            id="r1", object="chat.completion", created=0, model="m",
            choices=[ch], usage=None,
        )
        d = resp.to_dict()
        assert d["choices"][0]["message"]["tool_calls"] is not None

    def test_first_assistant_tool_calls(self):
        """first_assistant_tool_calls() 返回 tool_calls。"""
        ch = Choice(
            index=0,
            message_role="assistant",
            message_content=None,
            finish_reason="tool_calls",
            tool_calls=[{"id": "call_1"}],
        )
        resp = ChatCompletionResponse(
            id="r1", object="chat.completion", created=0, model="m",
            choices=[ch], usage=None,
        )
        assert resp.first_assistant_tool_calls() is not None
        assert resp.first_assistant_tool_calls()[0]["id"] == "call_1"

    def test_first_assistant_tool_calls_none(self):
        """无 tool_calls 时返回 None。"""
        ch = Choice(index=0, message_role="assistant", message_content="hi", finish_reason="stop")
        resp = ChatCompletionResponse(
            id="r1", object="chat.completion", created=0, model="m",
            choices=[ch], usage=None,
        )
        assert resp.first_assistant_tool_calls() is None
