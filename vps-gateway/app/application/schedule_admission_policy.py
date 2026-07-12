"""ScheduleAdmissionPolicy：唤醒时间准入策略。

数据合同来源：架构文档 6.7 ScheduleAdmissionPolicy。

指令：
  1. 转换为 Asia/Shanghai
  2. 检查 requested_at 位于未来
  3. 检查 08:00 <= time < 24:00
  4. 对已发生的同日记录检查当前已知的 20 分钟/10 次限制
  5. 未来状态不可预测时允许写入，由 StartPolicy 在执行时最终裁决
  6. 不检查最大未来期限
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
class AdmissionResult:
    """准入结果。"""
    accepted: bool
    normalized_time: Optional[str] = None
    reason: Optional[str] = None
    earliest_allowed: Optional[str] = None


class ScheduleAdmissionPolicy:
    """唤醒时间准入策略。

    数据合同来源：架构文档 6.7。

    拒绝码：
    - outside_active_window
    - min_interval_not_met
    - daily_limit_reached
    - invalid_datetime
    - not_in_future
    """

    def __init__(
        self,
        timezone_name: str = "Asia/Shanghai",
        active_start: str = "08:00",
        active_end: str = "24:00",
        min_interval_minutes: int = 20,
        daily_limit: int = 10,
    ):
        self._tz = ZoneInfo(timezone_name) if ZoneInfo else timezone(timedelta(hours=8))
        self._active_start_hour, self._active_start_min = map(int, active_start.split(":"))
        self._active_end_hour, self._active_end_min = map(int, active_end.split(":"))
        self._min_interval = timedelta(minutes=min_interval_minutes)
        self._daily_limit = daily_limit

    def admit(
        self,
        requested_at: str,
        now: datetime,
        known_starts: list[datetime],
    ) -> AdmissionResult:
        """校验请求时间是否可接受。

        参数：
        - requested_at: ISO8601 时间字符串
        - now: 当前时间
        - known_starts: 该日期已知的实际启动时间列表

        返回 AdmissionResult。
        """
        # 步骤 1：解析时间
        try:
            requested = self._parse_and_convert(requested_at)
        except (ValueError, TypeError):
            return AdmissionResult(accepted=False, reason="invalid_datetime")

        # 步骤 2：检查位于未来
        if requested <= now:
            return AdmissionResult(accepted=False, reason="not_in_future")

        # 步骤 3：检查 08:00 <= time < 24:00
        if not self._in_active_window(requested):
            return AdmissionResult(
                accepted=False,
                reason="outside_active_window",
                earliest_allowed=self._next_window_start(requested, now),
            )

        # 步骤 4：对同日已发生的记录检查间隔和次数
        same_day_starts = [
            s for s in known_starts
            if s.astimezone(self._tz).date() == requested.date()
        ]

        # 4a：检查每日限制
        if len(same_day_starts) >= self._daily_limit:
            return AdmissionResult(
                accepted=False,
                reason="daily_limit_reached",
            )

        # 4b：检查最短间隔
        for s in same_day_starts:
            if abs((requested - s).total_seconds()) < self._min_interval.total_seconds():
                return AdmissionResult(
                    accepted=False,
                    reason="min_interval_not_met",
                    earliest_allowed=(s + self._min_interval).isoformat(),
                )

        # 步骤 5：未来状态不可预测时允许写入
        return AdmissionResult(
            accepted=True,
            normalized_time=requested.isoformat(),
        )

    def _parse_and_convert(self, requested_at: str) -> datetime:
        """解析时间并转换到目标时区。"""
        dt = datetime.fromisoformat(requested_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self._tz)
        return dt.astimezone(self._tz)

    def _in_active_window(self, dt: datetime) -> bool:
        """检查时间是否在 08:00—24:00 内。"""
        local_dt = dt.astimezone(self._tz)
        hour = local_dt.hour
        minute = local_dt.minute
        time_minutes = hour * 60 + minute
        start_minutes = self._active_start_hour * 60 + self._active_start_min
        end_minutes = self._active_end_hour * 60 + self._active_end_min
        return start_minutes <= time_minutes < end_minutes

    def _next_window_start(self, dt: datetime, now: datetime) -> Optional[str]:
        """返回下一个活动窗口开始时间。"""
        local_dt = dt.astimezone(self._tz)
        next_day = local_dt.replace(
            hour=self._active_start_hour,
            minute=self._active_start_min,
            second=0,
            microsecond=0,
        )
        if next_day <= local_dt:
            next_day = next_day + timedelta(days=1)
        return next_day.isoformat()
