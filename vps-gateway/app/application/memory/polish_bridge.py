"""PolishBridge：润色器。

将记忆引擎 LLM 的原始 @d 内容润色为适合注入 system message 的文本。
数据合同来源：V3 架构文档 6.2 _execute_retrieval_pipeline R7。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PolishBridge:
    """润色器。

    依赖注入：
    - llm_bridge: LLMBridge（使用 MEM_POLISH 模型配置）
    """

    def __init__(self, llm_bridge: Any | None = None) -> None:
        self._llm_bridge = llm_bridge

    async def polish(self, raw_content: str, context: str = "") -> str:
        """润色原始 @d 内容。

        输入: raw_content（记忆引擎 LLM 原始输出）, context（用户查询上下文）
        输出: 润色后的文本
        异常处理: LLM 不可用或异常时返回原始文本
        """
        if not raw_content:
            return ""

        if self._llm_bridge is None:
            logger.debug("polish_skipped: no llm_bridge, returning raw content")
            return raw_content

        try:
            polished = await self._llm_bridge.generate(
                prompt=f"请将以下记忆内容润色为简洁自然的文本，保持事实准确：\n\n{raw_content}",
                context=context,
            )
            if polished and polished.strip():
                return polished.strip()
            return raw_content
        except Exception as e:
            logger.warning("polish_error: %s, returning raw content", e)
            return raw_content
