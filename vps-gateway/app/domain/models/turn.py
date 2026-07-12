"""Turn 数据模型：ChatMessage, PreparedTurn, ModelCompletionInput, TurnContext。

数据合同来源：架构文档 5.4 TurnContext + 6.3 PreparedTurn / 6.4 ModelCompletionInput。
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone
from typing import Optional, Any


@dataclasses.dataclass(frozen=True)
class ChatMessage:
    """OpenAI Chat 消息。

    v2 扩展：支持 tool_calls (assistant) 和 tool_call_id (tool)。
    """

    role: str
    content: Optional[str]
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None

    def to_dict(self) -> dict:
        """转换为 OpenAI API 格式。"""
        result: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls is not None:
            result["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        return result


@dataclasses.dataclass(frozen=True)
class PreparedTurn:
    """ContextBuilder 的输出：准备好的消息序列 + Sample 版本号。"""

    messages: list[ChatMessage]
    sample_versions: dict[str, int]


@dataclasses.dataclass(frozen=True)
class ModelCompletionInput:
    """ModelClient.complete 的输入。

    v2 扩展：支持 tools 和 tool_choice。
    """

    messages: list[ChatMessage]
    temperature: Optional[float]
    max_output_tokens: Optional[int]
    tools: Optional[list[dict]] = None
    tool_choice: Optional[str] = None


@dataclasses.dataclass
class TurnContext:
    """单个回合的临时上下文。

    数据合同来源：架构文档 5.4 TurnContext。

    生命周期只覆盖当前回合。回合结束后销毁，不写入 Sample。
    每个回合拥有独立 TurnContext，不得共享 messages、tool history 或调用计数。
    """

    turn_id: str
    trigger_type: str  # "user" | "wake"
    trigger_id: str  # request_id | wake_id
    messages: list[ChatMessage]
    sample_versions: dict[str, int]
    tool_round: int = 0  # 0..5
    total_tool_calls: int = 0  # 0..10
    started_at: str = ""

    @classmethod
    def create(
        cls,
        trigger_type: str,
        trigger_id: str,
        messages: list[ChatMessage],
        sample_versions: dict[str, int],
    ) -> "TurnContext":
        """创建一个新的 TurnContext。"""
        return cls(
            turn_id=str(uuid.uuid4()),
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            messages=list(messages),  # 独立副本
            sample_versions=dict(sample_versions),
            tool_round=0,
            total_tool_calls=0,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

    def increment_tool_round(self) -> None:
        """递增工具轮次。"""
        self.tool_round += 1

    def add_tool_calls(self, count: int) -> None:
        """增加工具调用次数。"""
        self.total_tool_calls += count
