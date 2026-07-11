"""测试 OpenAIUpstreamClient 适配器。"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from app.domain.models.errors import UpstreamError, UpstreamTimeout
from app.domain.models.turn import ModelCompletionInput, ChatMessage
from app.domain.models.chat_completion import ChatCompletionResponse
from app.adapters.models.openai_upstream_client import OpenAIUpstreamClient
from app.infrastructure.config import Config


def _make_config(**overrides):
    defaults = dict(
        gateway_host="0.0.0.0",
        gateway_port=8000,
        gateway_api_key="gw-key",
        upstream_base_url="https://upstream.example.com",
        upstream_api_key="upstream-key",
        upstream_model="gpt-4-test",
        upstream_timeout_seconds=30,
        upstream_token_limit_field="max_completion_tokens",
        sample_directory="./samples",
        memory_char_budget=12000,
        outbox_database_path="./data/outbox.sqlite3",
        active_turn_enabled=False,
        active_turn_interval_minutes=60,
        active_turn_instruction="检查",
        default_temperature=0.7,
        default_max_output_tokens=1200,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestComplete:
    def test_request_construction(self):
        config = _make_config()
        client = OpenAIUpstreamClient(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp-1",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4-test",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello back"},
                "finish_reason": "stop",
            }],
        }

        with patch("app.adapters.models.openai_upstream_client.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_httpx.Client.return_value.__enter__.return_value = mock_client

            inp = ModelCompletionInput(
                messages=[ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")],
                temperature=0.5,
                max_output_tokens=500,
            )
            result = client.complete(inp)

        assert isinstance(result, ChatCompletionResponse)
        assert result.id == "resp-1"

        # Verify request was constructed correctly
        call_args = mock_client.post.call_args
        body = call_args.kwargs["json"]
        assert body["model"] == "gpt-4-test"
        assert body["stream"] is False
        assert body["temperature"] == 0.5
        assert body["max_completion_tokens"] == 500
        assert len(body["messages"]) == 2

        headers = call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer upstream-key"

    def test_uses_default_temperature(self):
        config = _make_config()
        client = OpenAIUpstreamClient(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4-test",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
        }

        with patch("app.adapters.models.openai_upstream_client.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_httpx.Client.return_value.__enter__.return_value = mock_client

            inp = ModelCompletionInput(
                messages=[ChatMessage(role="system", content="sys")],
                temperature=None,
                max_output_tokens=None,
            )
            client.complete(inp)

        call_args = mock_client.post.call_args
        body = call_args.kwargs["json"]
        assert body["temperature"] == 0.7
        assert body["max_completion_tokens"] == 1200

    def test_token_field_mapping_max_tokens(self):
        config = _make_config(upstream_token_limit_field="max_tokens")
        client = OpenAIUpstreamClient(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "resp-1",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
        }

        with patch("app.adapters.models.openai_upstream_client.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_httpx.Client.return_value.__enter__.return_value = mock_client

            inp = ModelCompletionInput(
                messages=[ChatMessage(role="system", content="sys")],
                temperature=None,
                max_output_tokens=300,
            )
            client.complete(inp)

        body = mock_client.post.call_args.kwargs["json"]
        assert body["max_tokens"] == 300
        assert "max_completion_tokens" not in body

    def test_upstream_error_on_non_2xx(self):
        config = _make_config()
        client = OpenAIUpstreamClient(config)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("app.adapters.models.openai_upstream_client.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_httpx.Client.return_value.__enter__.return_value = mock_client

            inp = ModelCompletionInput(
                messages=[ChatMessage(role="system", content="sys")],
                temperature=None,
                max_output_tokens=None,
            )
            with pytest.raises(UpstreamError) as exc_info:
                client.complete(inp)

        assert exc_info.value.status_code == 500

    def test_upstream_timeout(self):
        import httpx as real_httpx
        config = _make_config()
        client = OpenAIUpstreamClient(config)

        with patch("app.adapters.models.openai_upstream_client.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_client.post.side_effect = real_httpx.ReadTimeout("timeout")
            mock_httpx.Client.return_value.__enter__.return_value = mock_client
            mock_httpx.ReadTimeout = real_httpx.ReadTimeout
            mock_httpx.ConnectTimeout = real_httpx.ConnectTimeout
            mock_httpx.PoolTimeout = real_httpx.PoolTimeout

            inp = ModelCompletionInput(
                messages=[ChatMessage(role="system", content="sys")],
                temperature=None,
                max_output_tokens=None,
            )
            with pytest.raises(UpstreamTimeout):
                client.complete(inp)

    def test_url_construction(self):
        config = _make_config(upstream_base_url="https://api.example.com")
        client = OpenAIUpstreamClient(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "r",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
        }

        with patch("app.adapters.models.openai_upstream_client.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_httpx.Client.return_value.__enter__.return_value = mock_client

            inp = ModelCompletionInput(
                messages=[ChatMessage(role="system", content="sys")],
                temperature=None,
                max_output_tokens=None,
            )
            client.complete(inp)

        url = mock_client.post.call_args.args[0]
        assert url == "https://api.example.com/v1/chat/completions"
