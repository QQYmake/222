"""测试工具：echo_test, delay_test, fail_test。

数据合同来源：架构文档 1.1.7 第一批工具 + 7.2 工具边界。

约束:
  - echo_test、delay_test、fail_test 只能在测试配置启用
  - enabled_in_production=False
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.domain.models.tool import ToolExecutor, ToolExecutionContext


class EchoTestExecutor(ToolExecutor):
    """回显输入消息（仅测试）。"""

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        message = arguments.get("message", "")
        return f"echo: {message}"


class DelayTestExecutor(ToolExecutor):
    """延迟指定秒数后返回（仅测试）。

    用于测试工具超时行为。
    """

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        seconds = arguments.get("seconds", 0)
        await asyncio.sleep(float(seconds))
        return f"delayed {seconds}s"


class FailTestExecutor(ToolExecutor):
    """总是抛出异常（仅测试）。

    用于测试工具失败行为。
    """

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        raise RuntimeError("fail_test: intentional failure")
