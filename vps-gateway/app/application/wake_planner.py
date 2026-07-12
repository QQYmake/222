"""WakePlanner：唤醒规划器。

数据合同来源：架构文档 6.12 WakePlanner。

指令：
  1. 固定规划器最多维护下一条 pending fixed 任务
  2. 固定槽位以每天 WAKE_ACTIVE_START（默认 08:00）为锚点
  3. 当日槽位 = active_start + n × fixed_interval，且必须早于 active_end
  4. 无 pending fixed 时，选择严格晚于 now 的最早槽位
  5. 固定 ID = "fixed:" + scheduled_at
  6. 随机规划器最多维护下一条 pending random 任务
  7. 无 pending random 时，以 now 为基准在 [min,max] 范围抽取一次
  8. 已持久化随机时间不因扫描频率重新抽取
  9. tool 来源的远期任务不计入 fixed/random 各自的一条维护额度
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

from app.domain.models.wake_job import WakeJob, WakeJobStatus
from app.application.schedule_admission_policy import ScheduleAdmissionPolicy
from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore

logger = logging.getLogger(__name__)


class WakePlanner:
    """唤醒规划器。

    架构文档 6.12。
    """

    def __init__(
        self,
        store: SQLiteWakeJobStore,
        policy: ScheduleAdmissionPolicy,
        fixed_enabled: bool = True,
        fixed_interval_minutes: int = 60,
        random_enabled: bool = False,
        random_min_minutes: int = 20,
        random_max_minutes: int = 180,
        timezone_name: str = "Asia/Shanghai",
        active_start: str = "08:00",
        active_end: str = "24:00",
    ):
        self._store = store
        self._policy = policy
        self._fixed_enabled = fixed_enabled
        self._fixed_interval = timedelta(minutes=fixed_interval_minutes)
        self._random_enabled = random_enabled
        self._random_min = random_min_minutes
        self._random_max = random_max_minutes
        self._tz = ZoneInfo(timezone_name) if ZoneInfo else timezone(timedelta(hours=8))
        self._active_start_h, self._active_start_m = map(int, active_start.split(":"))
        self._active_end_h, self._active_end_m = map(int, active_end.split(":"))

    async def plan_fixed(self, now: datetime) -> Optional[WakeJob]:
        """规划固定唤醒任务。

        指令来源：架构文档 6.12 步骤 1—5。
        """
        if not self._fixed_enabled:
            return None

        # 步骤 1：检查是否已有 pending fixed
        existing = await self._store.list_jobs(status=WakeJobStatus.PENDING)
        pending_fixed = [j for j in existing if j.source == "fixed"]
        if pending_fixed:
            return pending_fixed[0]  # 已有，不创建

        # 步骤 2—4：计算下一个槽位
        local_now = now.astimezone(self._tz)
        next_slot = self._compute_next_fixed_slot(local_now)
        if next_slot is None:
            return None

        # 步骤 5：固定 ID = "fixed:" + scheduled_at
        scheduled_at_str = next_slot.isoformat()
        wake_id = f"fixed:{scheduled_at_str}"

        job = WakeJob(
            wake_id=wake_id,
            source="fixed",
            requested_at=scheduled_at_str,
            scheduled_at=scheduled_at_str,
            reason="fixed_schedule",
        )
        return await self._store.schedule_once(job)

    def _compute_next_fixed_slot(self, now: datetime) -> Optional[datetime]:
        """计算下一个固定槽位。"""
        active_start = now.replace(
            hour=self._active_start_h,
            minute=self._active_start_m,
            second=0,
            microsecond=0,
        )

        # 当日槽位 = active_start + n × fixed_interval
        slot = active_start
        end_of_day_minutes = self._active_end_h * 60 + self._active_end_m
        if end_of_day_minutes >= 24 * 60:
            # 24:00 → 次日 00:00
            end_of_day = (active_start + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            end_of_day = now.replace(
                hour=self._active_end_h,
                minute=self._active_end_m,
                second=0,
                microsecond=0,
            )

        while slot < end_of_day:
            if slot > now:
                return slot
            slot += self._fixed_interval

        # 当日无剩余槽位，选择次日 08:00
        next_day = active_start + timedelta(days=1)
        return next_day

    async def plan_random(self, now: datetime) -> Optional[WakeJob]:
        """规划随机唤醒任务。

        指令来源：架构文档 6.12 步骤 6—11。
        """
        if not self._random_enabled:
            return None

        # 步骤 6：检查是否已有 pending random
        existing = await self._store.list_jobs(status=WakeJobStatus.PENDING)
        pending_random = [j for j in existing if j.source == "random"]
        if pending_random:
            return pending_random[0]  # 已有，不重新抽取

        # 步骤 7：以 now 为基准抽取一次
        import random as rand
        offset_minutes = rand.randint(self._random_min, self._random_max)
        candidate = now + timedelta(minutes=offset_minutes)
        local_candidate = candidate.astimezone(self._tz)

        # 步骤 8：候选区间若落在凌晨，改用下一个活动窗口内的合法区间
        if not self._in_active_window(local_candidate):
            # 推到下一个 08:00
            next_start = local_candidate.replace(
                hour=self._active_start_h,
                minute=self._active_start_m,
                second=0,
                microsecond=0,
            )
            if next_start <= local_candidate:
                next_start += timedelta(days=1)
            local_candidate = next_start

        scheduled_at_str = local_candidate.isoformat()
        wake_id = f"random:{scheduled_at_str}:{uuid.uuid4().hex[:8]}"

        job = WakeJob(
            wake_id=wake_id,
            source="random",
            requested_at=now.isoformat(),
            scheduled_at=scheduled_at_str,
            reason="random_schedule",
        )
        return await self._store.schedule_once(job)

    def _in_active_window(self, dt: datetime) -> bool:
        """检查时间是否在 08:00—24:00 内。"""
        time_minutes = dt.hour * 60 + dt.minute
        start_minutes = self._active_start_h * 60 + self._active_start_m
        end_minutes = self._active_end_h * 60 + self._active_end_m
        return start_minutes <= time_minutes < end_minutes
