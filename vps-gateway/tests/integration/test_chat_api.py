"""HTTP 层集成测试: Chat API 端到端流程。

验证基线：架构文档 12.2
- 合法请求返回 200 + OpenAI 兼容响应
- 鉴权失败返回 401
- stream=true 返回 400
- 请求体不合法返回 400
- turn_runner 调用正确

数据流:
  HTTP POST /v1/chat/completions
    → authenticate_gateway_request(headers, key)
    → parse_chat_request(body)
    → UserTrigger(request_id, body)
    → TurnRunner.run(trigger)
    → response.to_dict()
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.http.chat_controller import create_chat_router


@pytest.fixture
def mock_turn_runner():
    """Mock TurnRunner，返回一个标准的 ChatCompletionResponse。

    v2: run_user_turn 为异步方法。
    """
    runner = MagicMock()

    async def fake_run_user_turn(trigger):
        response = MagicMock()
        response.to_dict.return_value = {
            "id": "chatcmpl-fake-001",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "你好，我是网关助手。",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }
        return response

    runner.run_user_turn = AsyncMock(side_effect=fake_run_user_turn)
    return runner


@pytest.fixture
def app_and_client(mock_turn_runner):
    app = FastAPI()
    router = create_chat_router(mock_turn_runner, "test-gateway-key")
    app.include_router(router)
    client = TestClient(app)
    return app, client


class TestChatCompletions:
    """Chat API 集成测试。"""

    def test_success(self, app_and_client):
        """合法请求 → 200 + OpenAI 兼容响应。"""
        _, client = app_and_client
        body = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "user", "content": "你好"}
            ],
        }
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["finish_reason"] == "stop"

    def test_missing_auth(self, app_and_client):
        """无 Authorization header → 401。"""
        _, client = app_and_client
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post("/v1/chat/completions", json=body)
        assert resp.status_code == 401
        assert resp.json()["error"]["type"] == "invalid_api_key"

    def test_wrong_key(self, app_and_client):
        """错误 key → 401。"""
        _, client = app_and_client
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    def test_stream_true(self, app_and_client):
        """stream=true → 400。"""
        _, client = app_and_client
        body = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "unsupported_stream"

    def test_invalid_json_body(self, app_and_client):
        """无效 JSON → 400。"""
        _, client = app_and_client
        resp = client.post(
            "/v1/chat/completions",
            data="not json",
            headers={"Authorization": "Bearer test-gateway-key",
                     "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request"

    def test_missing_messages(self, app_and_client):
        """缺少 messages → 400。"""
        _, client = app_and_client
        body = {"model": "deepseek-chat"}
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 400

    def test_conflicting_token_fields(self, app_and_client):
        """同时存在 max_tokens 和 max_completion_tokens → 400。"""
        _, client = app_and_client
        body = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 100,
            "max_completion_tokens": 200,
        }
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "conflicting_token_fields"

    def test_turn_runner_called(self, app_and_client, mock_turn_runner):
        """确认 TurnRunner 被调用，且 trigger 携带正确 request_id。"""
        _, client = app_and_client
        body = {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "你好"}],
        }
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 200
        mock_turn_runner.run_user_turn.assert_called_once()
        trigger = mock_turn_runner.run_user_turn.call_args[0][0]
        assert trigger.request_id  # UUID 生成
        assert trigger.chat_request["model"] == "deepseek-chat"

    def test_sample_read_error_returns_503(self, app_and_client, mock_turn_runner):
        """SampleReadError → 503 state_unavailable。"""
        _, client = app_and_client
        from app.domain.models.sample import SampleReadError

        mock_turn_runner.run_user_turn.side_effect = SampleReadError(
            sample_type="identity",
            reason="file_not_found",
        )
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["type"] == "state_unavailable"

    def test_upstream_timeout_returns_504(self, app_and_client, mock_turn_runner):
        """UpstreamTimeout → 504。"""
        _, client = app_and_client
        from app.domain.models.errors import UpstreamTimeout

        mock_turn_runner.run_user_turn.side_effect = UpstreamTimeout("timeout")
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 504
        assert resp.json()["error"]["type"] == "upstream_timeout"

    def test_upstream_error_returns_502(self, app_and_client, mock_turn_runner):
        """UpstreamError → 502。"""
        _, client = app_and_client
        from app.domain.models.errors import UpstreamError

        mock_turn_runner.run_user_turn.side_effect = UpstreamError(
            status_code=500, message="provider error",
        )
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert resp.status_code == 502
        assert resp.json()["error"]["type"] == "upstream_error"
