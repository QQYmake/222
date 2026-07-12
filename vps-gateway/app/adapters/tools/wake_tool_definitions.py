"""唤醒工具定义：schedule_wakeup、list_wakeups、cancel_wakeup。

数据合同来源：架构文档 6.13 唤醒工具。
"""
from __future__ import annotations

from app.domain.models.tool import ToolDefinition


SCHEDULE_WAKEUP_DEF = ToolDefinition(
    name="schedule_wakeup",
    description="安排一个未来的唤醒时间。当到达指定时间时，系统会主动发起一轮对话。",
    parameters={
        "type": "object",
        "properties": {
            "wake_id": {
                "type": "string",
                "description": "唤醒任务的唯一标识符。",
            },
            "requested_at": {
                "type": "string",
                "description": "请求的唤醒时间，ISO 8601 格式（如 2025-01-15T14:30:00+08:00）。",
            },
            "reason": {
                "type": "string",
                "description": "唤醒原因，供参考。",
            },
        },
        "required": ["wake_id", "requested_at", "reason"],
    },
    enabled_in_production=True,
    timeout_seconds=15,
    max_result_chars=4000,
)


LIST_WAKEUPS_DEF = ToolDefinition(
    name="list_wakeups",
    description="查询当前已安排的唤醒任务列表。",
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "按状态过滤：pending、running、completed、expired、cancelled。不传则返回全部。",
            },
        },
        "required": [],
    },
    enabled_in_production=True,
    timeout_seconds=15,
    max_result_chars=4000,
)


CANCEL_WAKEUP_DEF = ToolDefinition(
    name="cancel_wakeup",
    description="取消一个尚未执行的唤醒任务（仅 pending 状态可取消）。",
    parameters={
        "type": "object",
        "properties": {
            "wake_id": {
                "type": "string",
                "description": "要取消的唤醒任务 ID。",
            },
        },
        "required": ["wake_id"],
    },
    enabled_in_production=True,
    timeout_seconds=15,
    max_result_chars=4000,
)
