"""唤醒工具：schedule_wakeup、list_wakeups、cancel_wakeup。

数据合同来源：架构文档 6.13 唤醒工具。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

from app.domain.models.tool import ToolExecutor, ToolExecutionContext
from app.domain.models.wake_job import WakeJob, WakeJobStatus
from app.application.schedule_admission_policy import ScheduleAdmissionPolicy
from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore

logger = logging.getLogger(__name__)


class ScheduleWakeupExecutor(ToolExecutor):
    """schedule_wakeup 工具执行器。

    数据合同来源：架构文档 6.13 schedule_wakeup。

    指令：
      1. ScheduleAdmissionPolicy 校验
      2. 失败：返回错误码与最早允许时间，不写数据库
      3. 成功：WakeJobStore.schedule_once()
      4. 重复 wake_id：返回原记录
    """

    def __init__(self, store: SQLiteWakeJobStore, policy: ScheduleAdmissionPolicy):
        self._store = store
        self._policy = policy

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> Any:
        wake_id = arguments.get("wake_id", "")
        requested_at = arguments.get("requested_at", "")
        reason = arguments.get("reason", "")

        # 获取当前时间
        tz = ZoneInfo("Asia/Shanghai") if ZoneInfo else timezone(timedelta(hours=8))
        now = datetime.now(tz)

        # 获取同日已知的实际启动记录
        local_date = now.astimezone(tz).strftime("%Y-%m-%d")
        last_start = await self._store.get_last_start_on_date(local_date)
        known_starts = [last_start] if last_start else []

        result = self._policy.admit(requested_at, now, known_starts)

        if not result.accepted:
            logger.info(
                "wake_schedule_rejected wake_id=%s reason=%s",
                wake_id, result.reason,
            )
            return {
                "accepted": False,
                "error_code": result.reason,
                "earliest_allowed": result.earliest_allowed,
            }

        # 创建 WakeJob
        job = WakeJob(
            wake_id=wake_id,
            source="tool",
            requested_at=requested_at,
            scheduled_at=result.normalized_time or requested_at,
            reason=reason,
        )

        saved = await self._store.schedule_once(job)

        return {
            "accepted": True,
            "wake_id": saved.wake_id,
            "scheduled_at": saved.scheduled_at,
            "status": saved.status.value,
        }


class ListWakeupsExecutor(ToolExecutor):
    """list_wakeups 工具执行器。

    数据合同来源：架构文档 6.13 list_wakeups。

    指令：查询并按 scheduled_at 升序返回。
    """

    def __init__(self, store: SQLiteWakeJobStore):
        self._store = store

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> Any:
        status_filter = arguments.get("status")
        status_enum = WakeJobStatus(status_filter) if status_filter else None

        jobs = await self._store.list_jobs(status=status_enum)
        return {
            "count": len(jobs),
            "jobs": [j.to_dict() for j in jobs],
        }


class CancelWakeupExecutor(ToolExecutor):
    """cancel_wakeup 工具执行器。

    数据合同来源：架构文档 6.13 cancel_wakeup。

    指令：
      1. 只允许 pending → cancelled
      2. running/completed/expired 不可取消
    """

    def __init__(self, store: SQLiteWakeJobStore):
        self._store = store

    async def execute(self, arguments: dict, context: ToolExecutionContext) -> Any:
        wake_id = arguments.get("wake_id", "")

        # 先检查当前状态
        existing = await self._store.get_job(wake_id)
        if existing is None:
            return {"cancelled": False, "error_code": "not_found"}

        if existing.status != WakeJobStatus.PENDING:
            return {
                "cancelled": False,
                "error_code": "not_pending",
                "current_status": existing.status.value,
            }

        result = await self._store.cancel(wake_id)
        return {
            "cancelled": result.status == WakeJobStatus.CANCELLED,
            "wake_id": result.wake_id,
            "status": result.status.value,
        }
