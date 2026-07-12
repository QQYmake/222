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
        config: Any,
        category: str = "chat",
        on_usage: Optional[Callable[[str, int, int], None]] = None,
    ):
        # 支持 LLMBridgeConfig 或 MemoryLLMConfig（从 _config_stub 传入时）
        # category 决定使用 MemoryLLMConfig 中的哪组配置
        if isinstance(config, LLMBridgeConfig):
            self.config = config
        elif hasattr(config, f"{category}_base_url"):
            # MemoryLLMConfig — 根据 category 提取对应配置
            base_url = getattr(config, f"{category}_base_url", "") or getattr(config, "gen_base_url", "")
            api_key = getattr(config, f"{category}_api_key", "") or getattr(config, "gen_api_key", "")
            model = getattr(config, f"{category}_model", "") or getattr(config, "gen_model", "")
            self.config = LLMBridgeConfig(base_url=base_url, api_key=api_key, model=model)
        elif hasattr(config, "gen_base_url"):
            # MemoryLLMConfig fallback — 使用 gen 配置
            self.config = LLMBridgeConfig(
                base_url=config.gen_base_url,
                api_key=config.gen_api_key,
                model=config.gen_model,
            )
        else:
            # 假设是 LLMBridgeConfig-like
            self.config = config
        self.category = category
        self._on_usage = on_usage
        # 嵌入配置（从 MemoryLLMConfig 提取，供 embed() 使用）
        self._embed_type = getattr(config, "embed_type", "api")
        self._embed_model = getattr(config, "embed_model", "")
        logger.debug(
            "LLMBridge initialized for %s | Model: %s | Base: %s",
            category, self.config.model, self.config.base_url,
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

    async def classify_intent(self, text: str) -> Optional[str]:
        """意图分类 LLM 调用。返回 LLM 输出的 label 文本，失败返回 None。"""
        messages = [
            {"role": "system", "content": "你是意图分类器。只回复一个词：query、no_query 或 unknown。"},
            {"role": "user", "content": text},
        ]
        return await self.chat_completion(messages, temperature=0.0)

    async def generate(
        self,
        messages: Optional[List[Dict[str, str]]] = None,
        *,
        prompt: Optional[str] = None,
        context: Optional[str] = None,
        temperature: Optional[float] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """通用文本生成。支持 messages 列表或 prompt/context 快捷参数。"""
        if messages is None:
            msg_list: List[Dict[str, str]] = []
            if context:
                msg_list.append({"role": "system", "content": context})
            if prompt:
                msg_list.append({"role": "user", "content": prompt})
            if not msg_list:
                return None
            messages = msg_list
        return await self.chat_completion(messages, temperature=temperature)

    async def embed(self, text: str) -> Optional[List[float]]:
        """文本嵌入。

        优先使用本地 Sentence-Transformers（当 embed_type == 'local'）。
        如果本地不可用，回退到 API embedding 端点。
        两者均失败时返回 None（调用方需降级处理）。
        """
        # 尝试本地嵌入
        embed_type = getattr(self, "_embed_type", "api")
        embed_model = getattr(self, "_embed_model", "")
        if embed_type == "local" and embed_model:
            try:
                from sentence_transformers import SentenceTransformer
                if not hasattr(self, "_st_model"):
                    self._st_model = SentenceTransformer(embed_model)
                vec = self._st_model.encode(text)
                return vec.tolist()
            except ImportError:
                logger.debug("[LLMBridge] sentence_transformers not installed, falling back to API embed")
            except Exception as e:
                logger.warning("[LLMBridge] Local embed error: %s", e)

        # 回退到 API embedding
        try:
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            }
            payload = {"model": self.config.model, "input": text}
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{self.config.base_url}/embeddings",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            logger.warning("[LLMBridge] Embed error: %s", e)
            return None
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
