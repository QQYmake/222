"""WakeController：唤醒执行控制器。

数据合同来源：架构文档 6.11 WakeController。

指令：
  1. 对超出 START_GRACE 的任务标记 expired(missed_deadline)
  2. 用 WakeStartPolicy 过滤其他不可启动任务
  3. 从剩余任务中选择最早一条作为唯一候选
  4. 对候选调用 ActiveTurnGate.try_acquire()，绝不等待
  5. 获取失败：候选及本批其他到期任务全部 expired(active_turn_running)
  6. 获取成功：条件更新候选 pending → running
  7. 更新失败：释放 Gate；不得等待或重复启动
  8. 候选成功 running 后，本批其余到期任务立即 expired(active_turn_running)
  9. 创建独立后台任务调用 TurnRunner(WakeTrigger)，扫描循环立即返回
 10. 后台任务完成后写 completed/failed 并释放 Gate
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable, Awaitable

from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason
from app.application.active_turn_gate import ActiveTurnGate
from app.application.wake_start_policy import WakeStartPolicy
from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore

logger = logging.getLogger(__name__)


class WakeController:
    """唤醒执行控制器。

    架构文档 6.11：即使两个任务同时到期，也只有一条能进入 TurnRunner，
    另一条不会等待前一条结束。
    """

    def __init__(
        self,
        store: SQLiteWakeJobStore,
        gate: ActiveTurnGate,
        start_policy: WakeStartPolicy,
        turn_runner: object,
    ):
        self._store = store
        self._gate = gate
        self._policy = start_policy
        self._turn_runner = turn_runner
        self._active_task: Optional[asyncio.Task] = None

    async def process_batch(self, due_jobs: list[WakeJob], now: datetime) -> None:
        """处理一批到期任务。

        指令来源：架构文档 6.11 步骤 1—10。
        """
        if not due_jobs:
            return

        # 步骤 1：对超出 START_GRACE 的任务标记 expired(missed_deadline)
        # 步骤 2：用 WakeStartPolicy 过滤其他不可启动任务
        candidates: list[WakeJob] = []
        for job in due_jobs:
            scheduled = datetime.fromisoformat(job.scheduled_at)
            last_start = await self._store.get_last_start_on_date(
                scheduled.strftime("%Y-%m-%d")
            )
            daily_count = await self._store.count_starts_on_date(
                scheduled.strftime("%Y-%m-%d")
            )
            result = self._policy.check(scheduled, now, last_start, daily_count)

            if result.can_start:
                candidates.append(job)
            elif result.expire_reason is not None:
                await self._store.transition(
                    job.wake_id, WakeJobStatus.PENDING, WakeJobStatus.EXPIRED,
                    reason=result.expire_reason,
                )
                logger.info(
                    "wake_expired wake_id=%s reason=%s",
                    job.wake_id, result.expire_reason.value,
                )

        if not candidates:
            return

        # 步骤 3：选择最早一条作为唯一候选
        candidates.sort(key=lambda j: (j.scheduled_at, j.created_at))
        candidate = candidates[0]
        others = candidates[1:]

        # 步骤 4：对候选调用 ActiveTurnGate.try_acquire()
        acquired = await self._gate.try_acquire()

        if not acquired:
            # 步骤 5：获取失败，本批全部 expired(active_turn_running)
            for job in due_jobs:
                if job.status == WakeJobStatus.PENDING:
                    await self._store.transition(
                        job.wake_id, WakeJobStatus.PENDING, WakeJobStatus.EXPIRED,
                        reason=ExpireReason.ACTIVE_TURN_RUNNING,
                    )
                    logger.info(
                        "wake_expired wake_id=%s reason=active_turn_running",
                        job.wake_id,
                    )
            return

        # 步骤 6：获取成功，条件更新候选 pending → running
        ok = await self._store.transition(
            candidate.wake_id, WakeJobStatus.PENDING, WakeJobStatus.RUNNING,
        )

        if not ok:
            # 步骤 7：更新失败，释放 Gate
            self._gate.release()
            logger.warning(
                "wake_transition_failed wake_id=%s expected=pending target=running",
                candidate.wake_id,
            )
            return

        # 步骤 8：本批其余到期任务立即 expired(active_turn_running)
        for job in others:
            await self._store.transition(
                job.wake_id, WakeJobStatus.PENDING, WakeJobStatus.EXPIRED,
                reason=ExpireReason.ACTIVE_TURN_RUNNING,
            )
            logger.info(
                "wake_expired wake_id=%s reason=active_turn_running",
                job.wake_id,
            )

        # 步骤 9：创建独立后台任务调用 TurnRunner
        logger.info("wake_started wake_id=%s", candidate.wake_id)
        self._active_task = asyncio.create_task(
            self._run_active_turn(candidate)
        )

    async def _run_active_turn(self, job: WakeJob) -> None:
        """执行主动回合的后台任务。

        步骤 10：后台任务完成后写 completed/failed 并释放 Gate。
        """
        try:
            result = await self._turn_runner.run_wake(job)
            if result is not None and hasattr(result, 'outcome'):
                if result.outcome in ('message_enqueued', 'no_message'):
                    await self._store.transition(
                        job.wake_id, WakeJobStatus.RUNNING, WakeJobStatus.COMPLETED,
                    )
                    logger.info("wake_completed wake_id=%s", job.wake_id)
                else:
                    await self._store.transition(
                        job.wake_id, WakeJobStatus.RUNNING, WakeJobStatus.FAILED,
                    )
                    logger.info("wake_failed wake_id=%s outcome=%s", job.wake_id, result.outcome)
            else:
                await self._store.transition(
                    job.wake_id, WakeJobStatus.RUNNING, WakeJobStatus.COMPLETED,
                )
                logger.info("wake_completed wake_id=%s", job.wake_id)
        except Exception as e:
            logger.error("wake_failed wake_id=%s error=%s", job.wake_id, e, exc_info=True)
            await self._store.transition(
                job.wake_id, WakeJobStatus.RUNNING, WakeJobStatus.FAILED,
            )
        finally:
            self._gate.release()
            logger.info("active_turn_gate_released wake_id=%s", job.wake_id)

    @property
    def is_active(self) -> bool:
        """是否有主动回合正在运行。"""
        return self._gate.is_locked

    async def wait_for_active_task(self) -> None:
        """等待当前主动后台任务完成（用于 shutdown）。"""
        if self._active_task is not None and not self._active_task.done():
            await self._active_task
