"""WakeStartPolicy：唤醒启动策略。

数据合同来源：架构文档 6.8 WakeStartPolicy。

指令：
  1. 检查 scheduled_at <= now <= scheduled_at + START_GRACE
  2. 超过宽限：拒绝 missed_deadline
  3. 检查执行时仍在 08:00—24:00
  4. 检查距离最近一次实际启动 >= 20 分钟
  5. 检查 scheduled_at 所属自然日实际启动次数 < 10
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

from app.domain.models.wake_job import ExpireReason


@dataclasses.dataclass
class StartPolicyResult:
    """启动策略结果。"""
    can_start: bool
    expire_reason: Optional[ExpireReason] = None


class WakeStartPolicy:
    """唤醒启动策略。

    数据合同来源：架构文档 6.8。
    """

    def __init__(
        self,
        timezone_name: str = "Asia/Shanghai",
        active_start: str = "08:00",
        active_end: str = "24:00",
        min_interval_minutes: int = 20,
        daily_limit: int = 10,
        start_grace_seconds: int = 10,
    ):
        self._tz = ZoneInfo(timezone_name) if ZoneInfo else timezone(timedelta(hours=8))
        self._active_start_hour, self._active_start_min = map(int, active_start.split(":"))
        self._active_end_hour, self._active_end_min = map(int, active_end.split(":"))
        self._min_interval = timedelta(minutes=min_interval_minutes)
        self._daily_limit = daily_limit
        self._grace = timedelta(seconds=start_grace_seconds)

    def check(
        self,
        scheduled_at: datetime,
        now: datetime,
        last_start: Optional[datetime],
        daily_count: int,
    ) -> StartPolicyResult:
        """检查任务是否可以启动。

        参数：
        - scheduled_at: 计划执行时间
        - now: 当前时间
        - last_start: 最近一次实际主动启动时间
        - daily_count: scheduled_at 所属自然日的实际启动次数

        返回 StartPolicyResult。
        """
        # 步骤 1：检查 scheduled_at <= now <= scheduled_at + grace
        if now < scheduled_at:
            # 还没到时间
            return StartPolicyResult(can_start=False)

        # 步骤 2：超过宽限 → missed_deadline
        if now > scheduled_at + self._grace:
            return StartPolicyResult(can_start=False, expire_reason=ExpireReason.MISSED_DEADLINE)

        # 步骤 3：检查执行时仍在 08:00—24:00
        if not self._in_active_window(now):
            return StartPolicyResult(can_start=False, expire_reason=ExpireReason.OUTSIDE_WINDOW)

        # 步骤 4：检查距上次启动 >= 20 分钟
        if last_start is not None:
            if (now - last_start) < self._min_interval:
                return StartPolicyResult(can_start=False, expire_reason=ExpireReason.MIN_INTERVAL)

        # 步骤 5：检查每日次数 < 10
        if daily_count >= self._daily_limit:
            return StartPolicyResult(can_start=False, expire_reason=ExpireReason.DAILY_LIMIT)

        return StartPolicyResult(can_start=True)

    def _in_active_window(self, dt: datetime) -> bool:
        """检查时间是否在 08:00—24:00 内。"""
        local_dt = dt.astimezone(self._tz)
        time_minutes = local_dt.hour * 60 + local_dt.minute
        start_minutes = self._active_start_hour * 60 + self._active_start_min
        end_minutes = self._active_end_hour * 60 + self._active_end_min
        return start_minutes <= time_minutes < end_minutes
