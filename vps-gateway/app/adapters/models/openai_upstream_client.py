"""OpenAIUpstreamClient: 将请求发送到固定 OpenAI Chat 兼容上游。

数据合同来源：架构文档 6.4 OpenAIUpstreamClient。

职责：构造上游请求、使用服务器端 API Key、固定 stream=false、校验响应。
不透传前端凭据给上游。
"""
from __future__ import annotations

import httpx

from app.domain.ports.model_client import ModelClient
from app.domain.models.turn import ModelCompletionInput
from app.domain.models.chat_completion import ChatCompletionResponse, validate_chat_completion_response
from app.domain.models.errors import UpstreamError, UpstreamTimeout
from app.infrastructure.config import Config
from app.infrastructure.logging import get_logger


class OpenAIUpstreamClient(ModelClient):
    """OpenAI Chat 兼容上游适配器。"""

    def __init__(self, config: Config):
        self._base_url = config.upstream_base_url
        self._api_key = config.upstream_api_key
        self._model = config.upstream_model
        self._timeout = config.upstream_timeout_seconds
        self._token_field = config.upstream_token_limit_field
        self._default_temperature = config.default_temperature
        self._default_max_tokens = config.default_max_output_tokens
        self._logger = get_logger("openai_upstream_client")

    def complete(self, input: ModelCompletionInput) -> ChatCompletionResponse:
        """调用上游模型。

        指令:
          1. 构造 request_body: model, messages, temperature, stream=false
          2. 设置 token_field: request_body[token_field] = max_output_tokens ?? default
          3. POST {base_url}/v1/chat/completions
          4. headers: Authorization: Bearer {api_key}
          5. timeout: UPSTREAM_TIMEOUT_SECONDS
          6. 非 2xx → UpstreamError(status, body)
          7. 超时 → UpstreamTimeout
          8. 校验响应 → validate_chat_completion_response
          9. 不记录 API Key
        """
        request_body: dict = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in input.messages],
            "temperature": (
                input.temperature
                if input.temperature is not None
                else self._default_temperature
            ),
            "stream": False,
        }
        request_body[self._token_field] = (
            input.max_output_tokens
            if input.max_output_tokens is not None
            else self._default_max_tokens
        )

        # base_url 可能已包含 /v1 或不包含，统一处理
        base = self._base_url.rstrip("/")
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        self._logger.info("upstream_request_started",
                          extra={"url": url, "model": self._model, "message_count": len(input.messages)})

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, json=request_body, headers=headers)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            self._logger.error("upstream_timeout", extra={"error": str(e)})
            raise UpstreamTimeout(str(e))
        except httpx.HTTPError as e:
            self._logger.error("upstream_http_error", extra={"error": str(e)})
            raise UpstreamError(0, str(e))

        if response.status_code < 200 or response.status_code >= 300:
            self._logger.error("upstream_non_2xx",
                               extra={"status_code": response.status_code})
            raise UpstreamError(response.status_code, response.text)

        parsed = response.json()
        result = validate_chat_completion_response(parsed)

        self._logger.info("upstream_response_received",
                          extra={"response_id": result.id, "model": result.model})

        return result
