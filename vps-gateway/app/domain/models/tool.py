"""Tool 领域模型：ToolDefinition, ToolCall, ToolResult, ToolExecutor, ToolExecutionContext。

数据合同来源：架构文档 5.2 ToolDefinition + 5.3 ToolCall/ToolResult + 6.3/6.4。
"""
from __future__ import annotations

import abc
import dataclasses
from typing import Any, Optional


@dataclasses.dataclass(frozen=True)
class ToolDefinition:
    """工具定义。

    数据合同来源：架构文档 5.2 ToolDefinition。
    """
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    enabled_in_production: bool
    timeout_seconds: int
    max_result_chars: int


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """模型返回的工具调用。

    数据合同来源：架构文档 5.3 ToolCall。
    """
    id: str  # 上游 LLM 返回的 tool_call_id
    name: str
    arguments_json: str  # 原始 JSON 字符串


@dataclasses.dataclass(frozen=True)
class ToolResult:
    """工具执行结果。

    数据合同来源：架构文档 5.3 ToolResult。

    错误也转换成 ToolResult，并以 tool message 回灌模型。
    """
    tool_call_id: str  # 与 ToolCall.id 相同
    ok: bool
    content: str  # 返回模型的字符串
    error_code: Optional[str] = None  # tool_not_found | invalid_arguments | tool_timeout | tool_failed
    truncated: bool = False
    duration_ms: int = 0


@dataclasses.dataclass(frozen=True)
class ToolExecutionContext:
    """工具执行上下文。"""
    turn_id: str
    trigger_type: str  # "user" | "wake"
    trigger_id: str


class ToolExecutor(abc.ABC):
    """工具执行器端口。

    数据合同来源：架构文档 6.4 ToolExecutor。

    指令:
      1. 只执行一个具体工具的领域动作
      2. 不查 Registry、不解析原始 JSON、不自行重试
      3. 返回具体结果或抛出内部工具异常
    """

    @abc.abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> Any:
        """执行工具，返回原始结果（将被转换为字符串）。"""
