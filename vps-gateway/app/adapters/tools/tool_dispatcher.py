"""ToolDispatcher: 解析、校验、超时、截断和错误归一化的唯一入口。

数据合同来源：架构文档 6.3 ToolDispatcher。

职责:
  1. 查找工具；不存在则返回 tool_not_found
  2. 解析 arguments JSON
  3. 按 Schema 校验；失败返回 invalid_arguments
  4. 按 tool_calls 返回顺序逐个执行
  5. 单个工具最多执行 timeout_seconds 秒
  6. 失败不重试，转换为错误 ToolResult
  7. 结果超过上限时截断并标记 truncated
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.domain.models.tool import ToolCall, ToolResult, ToolExecutionContext
from app.adapters.tools.registry import ToolRegistry
from app.infrastructure.logging import get_logger


class ToolDispatcher:
    """工具调度器。"""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry
        self._logger = get_logger("tool_dispatcher")

    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        """执行一个工具调用。

        指令:
          1. 查找工具；不存在 → tool_not_found
          2. 解析 arguments JSON；失败 → invalid_arguments
          3. 按 Schema 校验必填字段；失败 → invalid_arguments
          4. 执行工具，超时 → tool_timeout
          5. 异常 → tool_failed
          6. 结果截断 → truncated=True
          7. 返回 ToolResult
        """
        started = time.monotonic()
        turn_id = context.turn_id
        tool_call_id = call.id

        self._logger.info("tool_call_started", extra={
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "tool_name": call.name,
        })

        # 1. 查找工具
        executor = self._registry.resolve(call.name)
        definition = self._registry.get_definition(call.name)
        if executor is None or definition is None:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._logger.info("tool_call_failed", extra={
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "error_code": "tool_not_found",
            })
            return ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                content=f"Error: tool '{call.name}' not found or not available",
                error_code="tool_not_found",
                duration_ms=duration_ms,
            )

        # 2. 解析 arguments JSON
        try:
            arguments = json.loads(call.arguments_json) if call.arguments_json else {}
        except (json.JSONDecodeError, TypeError) as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._logger.info("tool_call_failed", extra={
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "error_code": "invalid_arguments",
            })
            return ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                content=f"Error: invalid arguments JSON: {e}",
                error_code="invalid_arguments",
                duration_ms=duration_ms,
            )

        # 3. 按	Schema 校验必填字段
        required = definition.parameters.get("required", [])
        missing = [r for r in required if r not in arguments]
        if missing:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._logger.info("tool_call_failed", extra={
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "error_code": "invalid_arguments",
            })
            return ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                content=f"Error: missing required arguments: {', '.join(missing)}",
                error_code="invalid_arguments",
                duration_ms=duration_ms,
            )

        # 4. 执行工具（带超时）
        try:
            raw_result = await asyncio.wait_for(
                executor.execute(arguments, context),
                timeout=definition.timeout_seconds,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._logger.info("tool_call_failed", extra={
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "error_code": "tool_timeout",
            })
            return ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                content=f"Error: tool '{call.name}' timed out after {definition.timeout_seconds}s",
                error_code="tool_timeout",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._logger.info("tool_call_failed", extra={
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "error_code": "tool_failed",
            })
            return ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                content=f"Error: tool '{call.name}' failed: {e}",
                error_code="tool_failed",
                duration_ms=duration_ms,
            )

        # 5. 转换结果为字符串
        if isinstance(raw_result, str):
            content = raw_result
        else:
            content = json.dumps(raw_result, ensure_ascii=False, default=str)

        # 6. 截断
        truncated = False
        if len(content) > definition.max_result_chars:
            content = content[:definition.max_result_chars] + "...[truncated]"
            truncated = True

        duration_ms = int((time.monotonic() - started) * 1000)
        self._logger.info("tool_call_completed", extra={
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "tool_name": call.name,
            "duration_ms": duration_ms,
            "truncated": truncated,
        })

        return ToolResult(
            tool_call_id=tool_call_id,
            ok=True,
            content=content,
            truncated=truncated,
            duration_ms=duration_ms,
        )
