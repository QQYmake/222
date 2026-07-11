"""测试 ChatCompletion 请求解析和响应校验。"""
import pytest
from app.domain.models.chat_completion import (
    parse_chat_request,
    ChatCompletionRequest,
    validate_chat_completion_response,
    ChatCompletionResponse,
    UnsupportedStreamError,
    ConflictingTokenFieldsError,
    to_internal_max_output_tokens,
)


class TestParseChatRequest:
    def test_valid_minimal(self):
        raw = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }
        req = parse_chat_request(raw)
        assert req.model == "gpt-4"
        assert len(req.messages) == 1

    def test_valid_with_system(self):
        raw = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
        }
        req = parse_chat_request(raw)
        assert len(req.messages) == 2

    def test_empty_messages_rejected(self):
        with pytest.raises(ValueError):
            parse_chat_request({"model": "gpt-4", "messages": []})

    def test_missing_messages_rejected(self):
        with pytest.raises(ValueError):
            parse_chat_request({"model": "gpt-4"})

    def test_invalid_role_rejected(self):
        with pytest.raises(ValueError):
            parse_chat_request({
                "model": "gpt-4",
                "messages": [{"role": "tool", "content": "hi"}],
            })

    def test_empty_content_rejected(self):
        with pytest.raises(ValueError):
            parse_chat_request({
                "model": "gpt-4",
                "messages": [{"role": "user", "content": ""}],
            })

    def test_stream_true_rejected(self):
        with pytest.raises(UnsupportedStreamError):
            parse_chat_request({
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            })

    def test_stream_false_ok(self):
        req = parse_chat_request({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        })
        assert req.stream is False

    def test_stream_none_ok(self):
        req = parse_chat_request({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert req.stream is None

    def test_dual_token_fields_rejected(self):
        with pytest.raises(ConflictingTokenFieldsError):
            parse_chat_request({
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 100,
                "max_tokens": 200,
            })

    def test_max_completion_tokens_alone_ok(self):
        req = parse_chat_request({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 100,
        })
        assert req.max_completion_tokens == 100

    def test_max_tokens_alone_ok(self):
        req = parse_chat_request({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 200,
        })
        assert req.max_tokens == 200

    def test_temperature_parsed(self):
        req = parse_chat_request({
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
        })
        assert req.temperature == 0.5


class TestToInternalMaxOutputTokens:
    def test_max_completion_tokens_preferred(self):
        req = ChatCompletionRequest(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            temperature=None,
            max_completion_tokens=100,
            max_tokens=None,
            stream=None,
        )
        assert to_internal_max_output_tokens(req) == 100

    def test_max_tokens_fallback(self):
        req = ChatCompletionRequest(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            temperature=None,
            max_completion_tokens=None,
            max_tokens=200,
            stream=None,
        )
        assert to_internal_max_output_tokens(req) == 200

    def test_none_when_both_absent(self):
        req = ChatCompletionRequest(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            temperature=None,
            max_completion_tokens=None,
            max_tokens=None,
            stream=None,
        )
        assert to_internal_max_output_tokens(req) is None


class TestValidateChatCompletionResponse:
    def test_valid_response(self):
        raw = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        resp = validate_chat_completion_response(raw)
        assert resp.id == "chatcmpl-123"
        assert resp.choices[0].message_content == "hello"
        assert resp.choices[0].message_role == "assistant"

    def test_empty_choices_rejected(self):
        with pytest.raises(ValueError):
            validate_chat_completion_response({
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [],
            })

    def test_missing_id_rejected(self):
        with pytest.raises(ValueError):
            validate_chat_completion_response({
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }],
            })

    def test_missing_model_rejected(self):
        with pytest.raises(ValueError):
            validate_chat_completion_response({
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }],
            })

    def test_to_dict(self):
        raw = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
        }
        resp = validate_chat_completion_response(raw)
        d = resp.to_dict()
        assert d["id"] == "chatcmpl-123"
        assert d["choices"][0]["message"]["content"] == "hello"
