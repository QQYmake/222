"""LLMBridge 适配模块——解耦 ebbingflow 监控层。

适配点：
  1. 使用 httpx 替代 openai SDK（vps-gateway 技术栈约束）
  2. 解耦监控层，改为可选回调 on_usage(category, input_tokens, output_tokens)
  3. 不设置 os.environ["NO_PROXY"]
  4. 配置通过构造注入，不引用 vps-gateway config.py
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class LLMBridgeConfig:
    """LLMBridge 配置（通过构造注入）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.7,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature


class LLMBridge:
    """提供统一的 LLM 访问接口，内置异常屏蔽和可选 usage 回调。

    适配自 ebbingflow bridge/llm.py：
    - openai SDK → httpx
    - 监控层 → 可选 on_usage 回调
    """

    def __init__(
        self,
        config: LLMBridgeConfig,
        category: str = "chat",
        on_usage: Optional[Callable[[str, int, int], None]] = None,
    ):
        self.config = config
        self.category = category
        self._on_usage = on_usage
        logger.debug(
            "LLMBridge initialized for %s | Model: %s | Base: %s",
            category, config.model, config.base_url,
        )

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """非流式 LLM 调用。失败返回 None。"""
        try:
            temp = temperature if temperature is not None else self.config.temperature
            payload: Dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": temp,
            }
            if response_format is not None:
                payload["response_format"] = response_format
            payload.update(kwargs)

            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{self.config.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()

            # 可选 usage 回调
            if self._on_usage and "usage" in data:
                u = data["usage"]
                self._on_usage(
                    self.category,
                    u.get("prompt_tokens", 0),
                    u.get("completion_tokens", 0),
                )

            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error("[LLMBridge] ChatCompletion Error: %s", e)
            return None

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]:
        """流式 LLM 调用。失败 yield 错误信息。"""
        try:
            temp = temperature if temperature is not None else self.config.temperature
            payload: Dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": temp,
                "stream": True,
            }
            payload.update(kwargs)

            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.config.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        import json
                        chunk = json.loads(data_str)
                        if chunk.get("choices"):
                            delta = chunk["choices"][0].get("delta", {}).get("content")
                            if delta:
                                yield delta
        except Exception as e:
            logger.error("[LLMBridge] Stream Error: %s", e)
            yield f"\n[AI 响应异常: {e}]"
