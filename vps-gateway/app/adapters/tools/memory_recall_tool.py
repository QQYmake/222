"""memory_recall 工具：仅在主动唤醒回合暴露。

数据合同来源：V3 架构文档 7.4 工具边界 + 6.3 ToolExecutor。
"""
from __future__ import annotations

import logging
from typing import Any

from app.domain.models.tool import ToolDefinition, ToolExecutionContext, ToolExecutor

logger = logging.getLogger(__name__)


MEMORY_RECALL_DEF = ToolDefinition(
    name="memory_recall",
    description="检索沉的记忆。当需要回忆过去的对话内容、用户偏好或事件时调用此工具。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索查询关键词或问题。",
            },
        },
        "required": ["query"],
    },
    enabled_in_production=True,
    timeout_seconds=15,
    max_result_chars=4000,
)


class MemoryRecallExecutor(ToolExecutor):
    """memory_recall 工具执行器。

    指令:
      1. 调用 MemoryPort.recall_as_tool(query, turn_id) 触发 @4 查询路径
      2. 返回润色后的 @d 文本
      3. 降级时返回空字符串
    """

    def __init__(self, memory_port: Any) -> None:
        self._memory_port = memory_port

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        query = arguments.get("query", "")
        logger.info(
            "memory_recall_tool_called",
            extra={"turn_id": context.turn_id, "trigger_id": context.trigger_id, "query_len": len(query)},
        )
        # Bug 10 fix: recall_as_tool 返回 str（与 Port 签名一致），直接返回
        result = await self._memory_port.recall_as_tool(
            query=query,
            turn_id=context.turn_id,
        )
        return result if isinstance(result, str) else str(result)
