"""WakeJobStore 端口接口。

数据合同来源：架构文档 6.9 WakeJobStore。
"""
from __future__ import annotations

import abc
from datetime import datetime
from typing import Optional

from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason, RecoveryResult


class WakeJobStore(abc.ABC):
    """唤醒任务存储端口。"""

    @abc.abstractmethod
    async def schedule_once(self, job: WakeJob) -> WakeJob:
        """幂等写入一条 WakeJob。"""

    @abc.abstractmethod
    async def due_jobs(self, now: datetime, grace_seconds: int) -> list[WakeJob]:
        """查询到期 pending 任务。"""

    @abc.abstractmethod
    async def transition(
        self,
        wake_id: str,
        expected: WakeJobStatus,
        target: WakeJobStatus,
        reason: Optional[ExpireReason] = None,
    ) -> bool:
        """条件状态转换。"""

    @abc.abstractmethod
    async def list_jobs(
        self,
        status: Optional[WakeJobStatus] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[WakeJob]:
        """查询任务列表。"""

    @abc.abstractmethod
    async def cancel(self, wake_id: str) -> WakeJob:
        """取消任务。"""

    @abc.abstractmethod
    async def recover_after_restart(self, now: datetime, grace_seconds: int) -> RecoveryResult:
        """重启恢复。"""

    @abc.abstractmethod
    async def get_job(self, wake_id: str) -> Optional[WakeJob]:
        """获取单条任务。"""

    @abc.abstractmethod
    async def get_last_start_on_date(self, date_str: str) -> Optional[datetime]:
        """获取指定日期最近一次实际启动时间。"""

    @abc.abstractmethod
    async def count_starts_on_date(self, date_str: str) -> int:
        """获取指定日期实际启动次数。"""
