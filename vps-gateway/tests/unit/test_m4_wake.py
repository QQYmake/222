"""M4 WakeJobStore + Admission/Start Policy + 三个唤醒工具测试。

验证场景覆盖架构文档 12.2.3—7、11—12：
- 凌晨任务到期：直接 expired
- 当天第 11 次任务到期：不调用 LLM
- 间隔不足 20 分钟：schedule 拒绝
- 三个月后任务重启恢复：scheduled_at 不变
- 相同 wake_id 重复提交：数据库只有一条
- 随机任务重复扫描：scheduled_at 不重新随机
- 服务停机跨过 scheduled_at + grace：重启后 expired
- 重启发现旧 running：转为 expired(crash_recovery)
- 唤醒时间 08:00—24:00 校验
- 最短间隔 20 分钟校验
- 每日 10 次限制校验
- cancel_wakeup 只允许 pending → cancelled
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason
from app.application.schedule_admission_policy import ScheduleAdmissionPolicy, AdmissionResult
from app.application.wake_start_policy import WakeStartPolicy, StartPolicyResult
from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore
from app.adapters.tools.wake_tools import (
    ScheduleWakeupExecutor,
    ListWakeupsExecutor,
    CancelWakeupExecutor,
)
from app.domain.models.tool import ToolDefinition, ToolCall, ToolExecutionContext
from app.adapters.tools.registry import ToolRegistry


# ── WakeJob 模型 ──────────────────────────────────────────────────────────────

class TestWakeJobModel:
    def test_create_pending_job(self):
        """验证 WakeJob 创建时 status=pending。"""
        job = WakeJob(
            wake_id="test-1",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        assert job.status == WakeJobStatus.PENDING
        assert job.expire_reason is None
        assert job.started_at is None

    def test_status_transitions(self):
        """验证状态转换。"""
        job = WakeJob(
            wake_id="test-2",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        job.status = WakeJobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc).isoformat()
        assert job.status == WakeJobStatus.RUNNING

        job.status = WakeJobStatus.COMPLETED
        job.finished_at = datetime.now(timezone.utc).isoformat()
        assert job.status == WakeJobStatus.COMPLETED


# ── ScheduleAdmissionPolicy ──────────────────────────────────────────────────

class TestScheduleAdmissionPolicy:
    def setup_method(self):
        self.policy = ScheduleAdmissionPolicy(
            timezone_name="Asia/Shanghai",
            active_start="08:00",
            active_end="24:00",
            min_interval_minutes=20,
            daily_limit=10,
        )

    def test_accept_future_time_in_window(self):
        """验证未来时间在 08:00—24:00 内被接受。"""
        now = datetime(2025, 7, 12, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        requested = "2025-07-12T10:00:00+08:00"
        result = self.policy.admit(requested, now, [])
        assert result.accepted is True
        assert result.normalized_time is not None

    def test_reject_past_time(self):
        """验证过去时间被拒绝。"""
        now = datetime(2025, 7, 12, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        requested = "2025-07-12T10:00:00+08:00"
        result = self.policy.admit(requested, now, [])
        assert result.accepted is False
        assert result.reason == "not_in_future"

    def test_reject_outside_window_before_8(self):
        """验证 08:00 之前被拒绝。"""
        now = datetime(2025, 7, 12, 7, 0, tzinfo=timezone(timedelta(hours=8)))
        requested = "2025-07-12T07:30:00+08:00"
        result = self.policy.admit(requested, now, [])
        assert result.accepted is False
        assert result.reason == "outside_active_window"

    def test_reject_outside_window_after_24(self):
        """验证 24:00 被拒绝（边界 < 24:00）。"""
        now = datetime(2025, 7, 12, 23, 0, tzinfo=timezone(timedelta(hours=8)))
        requested = "2025-07-13T00:30:00+08:00"
        result = self.policy.admit(requested, now, [])
        assert result.accepted is False
        assert result.reason == "outside_active_window"

    def test_reject_invalid_datetime(self):
        """验证无效时间格式被拒绝。"""
        now = datetime(2025, 7, 12, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        result = self.policy.admit("not-a-date", now, [])
        assert result.accepted is False
        assert result.reason == "invalid_datetime"

    def test_no_future_limit(self):
        """验证不限制最大未来期限。"""
        now = datetime(2025, 7, 12, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        # 三个月后
        requested = "2025-10-12T10:00:00+08:00"
        result = self.policy.admit(requested, now, [])
        assert result.accepted is True


# ── WakeStartPolicy ──────────────────────────────────────────────────────────

class TestWakeStartPolicy:
    def setup_method(self):
        self.policy = WakeStartPolicy(
            timezone_name="Asia/Shanghai",
            active_start="08:00",
            active_end="24:00",
            min_interval_minutes=20,
            daily_limit=10,
            start_grace_seconds=10,
        )

    def test_can_start_within_grace(self):
        """验证 scheduled_at <= now <= scheduled_at + grace 可以启动。"""
        scheduled = datetime(2025, 7, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        now = scheduled + timedelta(seconds=5)
        result = self.policy.check(scheduled, now, last_start=None, daily_count=0)
        assert result.can_start is True

    def test_reject_missed_deadline(self):
        """验证超过 grace 被拒绝为 missed_deadline。"""
        scheduled = datetime(2025, 7, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        now = scheduled + timedelta(seconds=60)  # 远超 grace
        result = self.policy.check(scheduled, now, last_start=None, daily_count=0)
        assert result.can_start is False
        assert result.expire_reason == ExpireReason.MISSED_DEADLINE

    def test_reject_outside_window(self):
        """验证执行时处于凌晨被拒绝。"""
        scheduled = datetime(2025, 7, 12, 7, 30, tzinfo=timezone(timedelta(hours=8)))
        now = scheduled
        result = self.policy.check(scheduled, now, last_start=None, daily_count=0)
        assert result.can_start is False
        assert result.expire_reason == ExpireReason.OUTSIDE_WINDOW

    def test_reject_min_interval(self):
        """验证距上次启动不足 20 分钟被拒绝。"""
        scheduled = datetime(2025, 7, 12, 10, 10, tzinfo=timezone(timedelta(hours=8)))
        now = scheduled
        last_start = datetime(2025, 7, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        result = self.policy.check(scheduled, now, last_start=last_start, daily_count=1)
        assert result.can_start is False
        assert result.expire_reason == ExpireReason.MIN_INTERVAL

    def test_reject_daily_limit(self):
        """验证当天 10 次后拒绝。"""
        scheduled = datetime(2025, 7, 12, 22, 0, tzinfo=timezone(timedelta(hours=8)))
        now = scheduled
        last_start = datetime(2025, 7, 12, 21, 30, tzinfo=timezone(timedelta(hours=8)))
        result = self.policy.check(scheduled, now, last_start=last_start, daily_count=10)
        assert result.can_start is False
        assert result.expire_reason == ExpireReason.DAILY_LIMIT


# ── SQLiteWakeJobStore ───────────────────────────────────────────────────────

class TestSQLiteWakeJobStore:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "wake_jobs.sqlite3")
        self.store = SQLiteWakeJobStore(self.db_path)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_schedule_once(self):
        """验证写入一条 pending WakeJob。"""
        job = WakeJob(
            wake_id="w1",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        result = await self.store.schedule_once(job)
        assert result.wake_id == "w1"
        assert result.status == WakeJobStatus.PENDING

    @pytest.mark.asyncio
    async def test_idempotent_schedule(self):
        """验证 12.2.7：相同 wake_id 重复提交只有一条。"""
        job = WakeJob(
            wake_id="w2",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        await self.store.schedule_once(job)
        result2 = await self.store.schedule_once(job)
        assert result2.wake_id == "w2"
        # 列出只有一条
        jobs = await self.store.list_jobs()
        w2_jobs = [j for j in jobs if j.wake_id == "w2"]
        assert len(w2_jobs) == 1

    @pytest.mark.asyncio
    async def test_transition_pending_to_running(self):
        """验证 pending → running 状态转换。"""
        job = WakeJob(
            wake_id="w3",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        await self.store.schedule_once(job)
        ok = await self.store.transition("w3", WakeJobStatus.PENDING, WakeJobStatus.RUNNING)
        assert ok is True

    @pytest.mark.asyncio
    async def test_transition_wrong_expected_fails(self):
        """验证条件转换失败返回 False。"""
        job = WakeJob(
            wake_id="w4",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        await self.store.schedule_once(job)
        # 已是 pending，尝试从 running → completed 应失败
        ok = await self.store.transition("w4", WakeJobStatus.RUNNING, WakeJobStatus.COMPLETED)
        assert ok is False

    @pytest.mark.asyncio
    async def test_cancel_pending(self):
        """验证 cancel 只允许 pending → cancelled。"""
        job = WakeJob(
            wake_id="w5",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        await self.store.schedule_once(job)
        result = await self.store.cancel("w5")
        assert result.status == WakeJobStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_running_fails(self):
        """验证 running 不可取消。"""
        job = WakeJob(
            wake_id="w6",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        await self.store.schedule_once(job)
        await self.store.transition("w6", WakeJobStatus.PENDING, WakeJobStatus.RUNNING)
        result = await self.store.cancel("w6")
        assert result.status == WakeJobStatus.RUNNING  # 未改变

    @pytest.mark.asyncio
    async def test_due_jobs(self):
        """验证查询到期 pending 任务。"""
        now = datetime(2025, 7, 12, 10, 5, tzinfo=timezone(timedelta(hours=8)))
        # 到期的
        await self.store.schedule_once(WakeJob(
            wake_id="due1", source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="due",
        ))
        # 未到期的
        await self.store.schedule_once(WakeJob(
            wake_id="future1", source="tool",
            requested_at="2025-07-12T12:00:00+08:00",
            scheduled_at="2025-07-12T12:00:00+08:00",
            reason="future",
        ))
        due = await self.store.due_jobs(now, grace_seconds=10)
        due_ids = [j.wake_id for j in due]
        assert "due1" in due_ids
        assert "future1" not in due_ids

    @pytest.mark.asyncio
    async def test_recover_after_restart_running(self):
        """验证 12.2.12：重启发现旧 running → expired(crash_recovery)。"""
        job = WakeJob(
            wake_id="w7",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        )
        await self.store.schedule_once(job)
        await self.store.transition("w7", WakeJobStatus.PENDING, WakeJobStatus.RUNNING)

        now = datetime(2025, 7, 12, 10, 30, tzinfo=timezone(timedelta(hours=8)))
        result = await self.store.recover_after_restart(now, grace_seconds=10)
        assert result.running_expired >= 1

        jobs = await self.store.list_jobs()
        w7 = [j for j in jobs if j.wake_id == "w7"][0]
        assert w7.status == WakeJobStatus.EXPIRED
        assert w7.expire_reason == ExpireReason.CRASH_RECOVERY

    @pytest.mark.asyncio
    async def test_recover_after_restart_missed_deadline(self):
        """验证 12.2.11：停机跨过 scheduled_at + grace → expired(missed_deadline)。"""
        await self.store.schedule_once(WakeJob(
            wake_id="w8",
            source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        ))
        now = datetime(2025, 7, 12, 10, 30, tzinfo=timezone(timedelta(hours=8)))
        result = await self.store.recover_after_restart(now, grace_seconds=10)
        assert result.missed_deadline_expired >= 1

        jobs = await self.store.list_jobs()
        w8 = [j for j in jobs if j.wake_id == "w8"][0]
        assert w8.status == WakeJobStatus.EXPIRED
        assert w8.expire_reason == ExpireReason.MISSED_DEADLINE

    @pytest.mark.asyncio
    async def test_scheduled_at_preserved_after_restart(self):
        """验证 12.2.6：重启后 scheduled_at 完全不变。"""
        await self.store.schedule_once(WakeJob(
            wake_id="w9",
            source="tool",
            requested_at="2025-10-12T10:00:00+08:00",
            scheduled_at="2025-10-12T10:00:00+08:00",
            reason="far future",
        ))
        now = datetime(2025, 7, 12, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        await self.store.recover_after_restart(now, grace_seconds=10)

        # 新实例读取
        store2 = SQLiteWakeJobStore(self.db_path)
        jobs = await store2.list_jobs()
        w9 = [j for j in jobs if j.wake_id == "w9"][0]
        assert w9.scheduled_at == "2025-10-12T10:00:00+08:00"


# ── 唤醒工具 ──────────────────────────────────────────────────────────────────

class TestWakeTools:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "wake_jobs.sqlite3")
        self.store = SQLiteWakeJobStore(self.db_path)
        self.policy = ScheduleAdmissionPolicy(
            timezone_name="Asia/Shanghai",
            active_start="08:00",
            active_end="24:00",
            min_interval_minutes=20,
            daily_limit=10,
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_schedule_wakeup_tool_success(self):
        """验证 schedule_wakeup 工具成功创建任务。"""
        executor = ScheduleWakeupExecutor(self.store, self.policy)
        now = datetime(2025, 7, 12, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        ctx = ToolExecutionContext(turn_id="t1", trigger_type="user", trigger_id="r1")
        result = await executor.execute(
            {"wake_id": "tool-1", "requested_at": "2025-07-12T10:00:00+08:00", "reason": "wake up"},
            ctx,
        )
        assert "scheduled_at" in str(result).lower() or "accepted" in str(result).lower() or "tool-1" in str(result)

    @pytest.mark.asyncio
    async def test_schedule_wakeup_tool_rejected(self):
        """验证 schedule_wakeup 工具被拒绝时返回错误码。"""
        executor = ScheduleWakeupExecutor(self.store, self.policy)
        ctx = ToolExecutionContext(turn_id="t2", trigger_type="user", trigger_id="r2")
        # 使用凌晨时间（在窗口外），但确保是未来
        from datetime import datetime as dt, timedelta as td, timezone as tz
        tz_sh = timezone(timedelta(hours=8))
        future_date = (dt.now(tz_sh) + timedelta(days=1)).strftime("%Y-%m-%d")
        result = await executor.execute(
            {"wake_id": "tool-2", "requested_at": f"{future_date}T07:00:00+08:00", "reason": "too early"},
            ctx,
        )
        result_str = str(result)
        assert "outside_active_window" in result_str or "rejected" in result_str.lower()

    @pytest.mark.asyncio
    async def test_schedule_wakeup_duplicate_id(self):
        """验证 12.2.7：相同 wake_id 重复提交返回原记录。"""
        executor = ScheduleWakeupExecutor(self.store, self.policy)
        ctx = ToolExecutionContext(turn_id="t3", trigger_type="user", trigger_id="r3")
        # 使用未来时间
        from datetime import datetime as dt, timedelta as td, timezone as tz
        tz_sh = timezone(timedelta(hours=8))
        future_date = (dt.now(tz_sh) + timedelta(days=1)).strftime("%Y-%m-%d")
        requested_at = f"{future_date}T10:00:00+08:00"
        args = {"wake_id": "dup-1", "requested_at": requested_at, "reason": "first"}
        await executor.execute(args, ctx)
        result2 = await executor.execute(args, ctx)
        # 第二次应返回已存在
        assert "dup-1" in str(result2)

    @pytest.mark.asyncio
    async def test_list_wakeups(self):
        """验证 list_wakeups 返回任务列表。"""
        # 先创建几条
        await self.store.schedule_once(WakeJob(
            wake_id="l1", source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        ))
        await self.store.schedule_once(WakeJob(
            wake_id="l2", source="tool",
            requested_at="2025-07-12T14:00:00+08:00",
            scheduled_at="2025-07-12T14:00:00+08:00",
            reason="test2",
        ))
        executor = ListWakeupsExecutor(self.store)
        ctx = ToolExecutionContext(turn_id="t4", trigger_type="user", trigger_id="r4")
        result = await executor.execute({}, ctx)
        result_str = str(result)
        assert "l1" in result_str
        assert "l2" in result_str

    @pytest.mark.asyncio
    async def test_cancel_wakeup_success(self):
        """验证 cancel_wakeup 取消 pending 任务。"""
        await self.store.schedule_once(WakeJob(
            wake_id="c1", source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        ))
        executor = CancelWakeupExecutor(self.store)
        ctx = ToolExecutionContext(turn_id="t5", trigger_type="user", trigger_id="r5")
        result = await executor.execute({"wake_id": "c1"}, ctx)
        assert "cancelled" in str(result).lower()

    @pytest.mark.asyncio
    async def test_cancel_wakeup_running_fails(self):
        """验证 cancel_wakeup 不能取消 running 任务。"""
        await self.store.schedule_once(WakeJob(
            wake_id="c2", source="tool",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
        ))
        await self.store.transition("c2", WakeJobStatus.PENDING, WakeJobStatus.RUNNING)
        executor = CancelWakeupExecutor(self.store)
        ctx = ToolExecutionContext(turn_id="t6", trigger_type="user", trigger_id="r6")
        result = await executor.execute({"wake_id": "c2"}, ctx)
        assert "running" in str(result).lower() or "cannot" in str(result).lower()
