"""WakeJob 数据模型。

数据合同来源：架构文档 5.5 WakeJob。
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Optional


class WakeJobStatus(enum.Enum):
    """WakeJob 状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExpireReason(enum.Enum):
    """过期原因。

    数据合同来源：架构文档 5.5 WakeJob.expire_reason。
    """
    ACTIVE_TURN_RUNNING = "active_turn_running"
    OUTSIDE_WINDOW = "outside_window"
    DAILY_LIMIT = "daily_limit"
    MIN_INTERVAL = "min_interval"
    MISSED_DEADLINE = "missed_deadline"
    CRASH_RECOVERY = "crash_recovery"


@dataclasses.dataclass
class WakeJob:
    """一次未来主动唤醒计划。

    数据合同来源：架构文档 5.5 WakeJob。

    约束：
    - scheduled_at 必须落在 08:00—24:00。
    - 不限制距离现在的最大未来天数。
    - 同一 wake_id 重复提交不产生第二条记录。
    - running 状态全局最多一条。
    - 只有 scheduled_at <= now <= scheduled_at + WAKE_START_GRACE_SECONDS 的任务可以开始。
    - 超过宽限窗口仍为 pending 的任务必须转为 expired(missed_deadline)，不得补跑。
    """

    wake_id: str
    source: str  # "fixed" | "random" | "tool"
    requested_at: str  # 模型/规则原始时间
    scheduled_at: str  # Asia/Shanghai 规范化后的执行时间
    reason: str
    status: WakeJobStatus = WakeJobStatus.PENDING
    created_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    expire_reason: Optional[ExpireReason] = None

    def to_dict(self) -> dict:
        return {
            "wake_id": self.wake_id,
            "source": self.source,
            "requested_at": self.requested_at,
            "scheduled_at": self.scheduled_at,
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "expire_reason": self.expire_reason.value if self.expire_reason else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WakeJob":
        return cls(
            wake_id=d["wake_id"],
            source=d["source"],
            requested_at=d["requested_at"],
            scheduled_at=d["scheduled_at"],
            reason=d["reason"],
            status=WakeJobStatus(d.get("status", "pending")),
            created_at=d.get("created_at", ""),
            started_at=d.get("started_at"),
            finished_at=d.get("finished_at"),
            expire_reason=ExpireReason(d["expire_reason"]) if d.get("expire_reason") else None,
        )


@dataclasses.dataclass
class RecoveryResult:
    """重启恢复结果。"""
    running_expired: int = 0
    missed_deadline_expired: int = 0
    grace_pending_retained: int = 0
