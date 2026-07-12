"""get_server_time 工具：返回服务器当前时间。

数据合同来源：架构文档 1.1.7 第一批工具。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.models.tool import ToolExecutor, ToolExecutionContext


class GetServerTimeExecutor(ToolExecutor):
    """返回服务器当前时间（ISO 8601）。"""

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> str:
        now = datetime.now(timezone.utc)
        return now.isoformat()
