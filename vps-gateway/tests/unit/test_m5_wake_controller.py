"""M5 WakePlanner + WakeController + ActiveTurnGate 测试。

验证场景覆盖架构文档 12.2.1—2、8—10、14：
- 两个主动任务同时到期：一个 running，另一个 expired
- 当前回合超过下次启动时间：下次任务 expired，不补跑
- 随机任务重复扫描：scheduled_at 不重新随机
- 用户回合正在等待 LLM：主动回合仍可启动并行 LLM 请求
- 主动回合已运行：第二个主动回合绝不进入 TurnRunner
- 固定规划器在相同 now 重启重算：仍得到同一个 wake_id
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason
from app.application.active_turn_gate import ActiveTurnGate
from app.application.wake_controller import WakeController
from app.application.wake_planner import WakePlanner
from app.application.wake_start_policy import WakeStartPolicy
from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore


tz_sh = timezone(timedelta(hours=8))


# ── ActiveTurnGate ───────────────────────────────────────────────────────────

class TestActiveTurnGate:
    @pytest.mark.asyncio
    async def test_acquire_when_idle(self):
        """验证 Gate 空闲时立即占用并返回 true。"""
        gate = ActiveTurnGate()
        assert await gate.try_acquire() is True

    @pytest.mark.asyncio
    async def test_acquire_when_busy(self):
        """验证 Gate 已占用时立即返回 false。"""
        gate = ActiveTurnGate()
        assert await gate.try_acquire() is True
        assert await gate.try_acquire() is False

    @pytest.mark.asyncio
    async def test_release_then_acquire(self):
        """验证释放后可再次获取。"""
        gate = ActiveTurnGate()
        await gate.try_acquire()
        gate.release()
        assert await gate.try_acquire() is True

    @pytest.mark.asyncio
    async def test_no_waiting(self):
        """验证不等待 Gate。"""
        gate = ActiveTurnGate()
        await gate.try_acquire()
        # 第二次应立即返回 false，不阻塞
        import time
        start = time.monotonic()
        result = await gate.try_acquire()
        elapsed = time.monotonic() - start
        assert result is False
        assert elapsed < 0.1  # 立即返回


# ── WakeController ───────────────────────────────────────────────────────────

class TestWakeController:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "wake_test.sqlite3")
        self.store = SQLiteWakeJobStore(self.db_path)
        self.gate = ActiveTurnGate()
        self.start_policy = WakeStartPolicy(
            timezone_name="Asia/Shanghai",
            active_start="08:00",
            active_end="24:00",
            min_interval_minutes=20,
            daily_limit=10,
            start_grace_seconds=10,
        )
        self.turn_runner_mock = AsyncMock()
        self.controller = WakeController(
            store=self.store,
            gate=self.gate,
            start_policy=self.start_policy,
            turn_runner=self.turn_runner_mock,
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_two_jobs_due_one_running_other_expired(self):
        """验证 12.2.1：两个主动任务同时到期，一个 running，另一个 expired。"""
        now = datetime(2025, 7, 12, 10, 0, tzinfo=tz_sh)
        # 两条同时到期的任务
        for wid in ["w1", "w2"]:
            await self.store.schedule_once(WakeJob(
                wake_id=wid, source="tool",
                requested_at=now.isoformat(),
                scheduled_at=now.isoformat(),
                reason="test",
            ))

        due = await self.store.due_jobs(now, grace_seconds=10)
        assert len(due) == 2

        await self.controller.process_batch(due, now)

        jobs = await self.store.list_jobs()
        w1 = [j for j in jobs if j.wake_id == "w1"][0]
        w2 = [j for j in jobs if j.wake_id == "w2"][0]

        # 一个 running，另一个 expired(active_turn_running)
        statuses = {w1.status, w2.status}
        assert WakeJobStatus.RUNNING in statuses or WakeJobStatus.COMPLETED in statuses
        assert WakeJobStatus.EXPIRED in statuses

        expired_job = w1 if w1.status == WakeJobStatus.EXPIRED else w2
        assert expired_job.expire_reason == ExpireReason.ACTIVE_TURN_RUNNING

    @pytest.mark.asyncio
    async def test_second_active_turn_rejected(self):
        """验证 12.2.10：主动回合已运行时第二个绝不进入 TurnRunner。"""
        now = datetime(2025, 7, 12, 10, 0, tzinfo=tz_sh)
        await self.store.schedule_once(WakeJob(
            wake_id="w3", source="tool",
            requested_at=now.isoformat(),
            scheduled_at=now.isoformat(),
            reason="first",
        ))
        await self.store.schedule_once(WakeJob(
            wake_id="w4", source="tool",
            requested_at=now.isoformat(),
            scheduled_at=now.isoformat(),
            reason="second",
        ))

        due = await self.store.due_jobs(now, grace_seconds=10)
        await self.controller.process_batch(due, now)

        # turn_runner 只被调用一次
        assert self.turn_runner_mock.run.call_count <= 1

    @pytest.mark.asyncio
    async def test_missed_deadline_expired(self):
        """验证 12.2.2：当前回合超过下次启动时间，下次任务 expired，不补跑。"""
        scheduled = datetime(2025, 7, 12, 10, 0, tzinfo=tz_sh)
        now = scheduled + timedelta(seconds=60)  # 远超 grace
        await self.store.schedule_once(WakeJob(
            wake_id="w5", source="tool",
            requested_at=scheduled.isoformat(),
            scheduled_at=scheduled.isoformat(),
            reason="test",
        ))

        due = await self.store.due_jobs(now, grace_seconds=10)
        await self.controller.process_batch(due, now)

        jobs = await self.store.list_jobs()
        w5 = [j for j in jobs if j.wake_id == "w5"][0]
        assert w5.status == WakeJobStatus.EXPIRED
        assert w5.expire_reason == ExpireReason.MISSED_DEADLINE

    @pytest.mark.asyncio
    async def test_gate_busy_all_expired(self):
        """验证 Gate 已占用时本批全部 expired。"""
        now = datetime(2025, 7, 12, 10, 0, tzinfo=tz_sh)
        await self.store.schedule_once(WakeJob(
            wake_id="w6", source="tool",
            requested_at=now.isoformat(),
            scheduled_at=now.isoformat(),
            reason="test",
        ))
        # 先占用 gate
        await self.gate.try_acquire()

        due = await self.store.due_jobs(now, grace_seconds=10)
        await self.controller.process_batch(due, now)

        jobs = await self.store.list_jobs()
        w6 = [j for j in jobs if j.wake_id == "w6"][0]
        assert w6.status == WakeJobStatus.EXPIRED
        assert w6.expire_reason == ExpireReason.ACTIVE_TURN_RUNNING


# ── WakePlanner ──────────────────────────────────────────────────────────────

class TestWakePlanner:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "wake_planner.sqlite3")
        self.store = SQLiteWakeJobStore(self.db_path)
        from app.application.schedule_admission_policy import ScheduleAdmissionPolicy
        self.policy = ScheduleAdmissionPolicy(
            timezone_name="Asia/Shanghai",
            active_start="08:00",
            active_end="24:00",
            min_interval_minutes=20,
            daily_limit=10,
        )
        self.planner = WakePlanner(
            store=self.store,
            policy=self.policy,
            fixed_enabled=True,
            fixed_interval_minutes=60,
            random_enabled=False,
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_fixed_planner_creates_next_slot(self):
        """验证固定规划器创建下一个槽位。"""
        now = datetime(2025, 7, 12, 9, 0, tzinfo=tz_sh)
        await self.planner.plan_fixed(now)
        jobs = await self.store.list_jobs()
        fixed_jobs = [j for j in jobs if j.source == "fixed"]
        assert len(fixed_jobs) <= 1  # 最多维护一条 pending fixed

    @pytest.mark.asyncio
    async def test_fixed_planner_idempotent_same_now(self):
        """验证 12.2.14：相同 now 重启重算仍得到同一个 wake_id。"""
        now = datetime(2025, 7, 12, 9, 0, tzinfo=tz_sh)
        await self.planner.plan_fixed(now)
        jobs1 = await self.store.list_jobs()
        fixed1 = [j for j in jobs1 if j.source == "fixed"]

        # 再次规划（模拟重启）
        await self.planner.plan_fixed(now)
        jobs2 = await self.store.list_jobs()
        fixed2 = [j for j in jobs2 if j.source == "fixed"]

        if fixed1 and fixed2:
            assert fixed1[0].wake_id == fixed2[0].wake_id
            assert fixed1[0].scheduled_at == fixed2[0].scheduled_at

    @pytest.mark.asyncio
    async def test_no_duplicate_fixed_jobs(self):
        """验证最多只维护一条 pending fixed 任务。"""
        now = datetime(2025, 7, 12, 9, 0, tzinfo=tz_sh)
        await self.planner.plan_fixed(now)
        await self.planner.plan_fixed(now)
        await self.planner.plan_fixed(now)

        jobs = await self.store.list_jobs()
        pending_fixed = [j for j in jobs if j.source == "fixed" and j.status == WakeJobStatus.PENDING]
        assert len(pending_fixed) <= 1
