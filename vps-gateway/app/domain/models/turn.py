"""Turn 数据模型：ChatMessage, PreparedTurn, ModelCompletionInput。

数据合同来源：架构文档 6.3 PreparedTurn / 6.4 ModelCompletionInput。
"""
from __future__ import annotations

import dataclasses
from typing import Optional


@dataclasses.dataclass(frozen=True)
class ChatMessage:
    """OpenAI Chat 消息。"""

    role: str
    content: str


@dataclasses.dataclass(frozen=True)
class PreparedTurn:
    """ContextBuilder 的输出：准备好的消息序列 + Sample 版本号。"""

    messages: list[ChatMessage]
    sample_versions: dict[str, int]


@dataclasses.dataclass(frozen=True)
class ModelCompletionInput:
    """ModelClient.complete 的输入。"""

    messages: list[ChatMessage]
    temperature: Optional[float]
    max_output_tokens: Optional[int]
