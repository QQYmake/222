"""M5 Task 1: LocalScheduler 单元测试。

验证:
  1. enabled=True 时 start() 启动线程
  2. enabled=False 时 start() 为 no-op
  3. 到达时间槽时生成 TimerTrigger 并调用 turn_runner.run()
  4. trigger_id 格式 = "timer:" + slot_start_iso
  5. 上一次未结束时跳过本次
  6. 异常不传播，记录日志后继续
  7. shutdown() 设置 _running=False，线程退出
"""
import threading
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest

from app.domain.models.trigger import TimerTrigger
from app.application.turn_runner import ActiveTurnResult


class TestLocalSchedulerInit:
    """LocalScheduler 构造和启停。"""

    def test_disabled_scheduler_does_not_start_thread(self):
        """enabled=False 时不启动线程。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=60,
            instruction="test instruction",
            enabled=False,
        )
        assert scheduler._thread is None
        scheduler.shutdown()

    def test_enabled_scheduler_starts_thread(self):
        """enabled=True 时启动 daemon 线程。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=1440,  # 24h, won't fire during test
            instruction="test",
            enabled=True,
        )
        assert scheduler._thread is not None
        assert scheduler._thread.daemon is True
        scheduler.shutdown()
        scheduler._thread.join(timeout=2)

    def test_shutdown_stops_loop(self):
        """shutdown() 后线程在合理时间内退出。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=1440,
            instruction="test",
            enabled=True,
        )
        scheduler.shutdown()
        scheduler._thread.join(timeout=3)
        assert not scheduler._thread.is_alive()


class TestLocalSchedulerTrigger:
    """LocalScheduler 产生 TimerTrigger 的行为。"""

    def test_trigger_id_format(self):
        """trigger_id 格式为 'timer:' + ISO8601 时间槽。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler, _floor_to_interval

        # 10:35 -> floor to 60min -> 10:00
        dt = datetime(2025, 1, 15, 10, 35, 0, tzinfo=timezone.utc)
        slot = _floor_to_interval(dt, 60)
        assert slot == datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

        trigger_id = "timer:" + slot.isoformat()
        assert trigger_id == "timer:2025-01-15T10:00:00+00:00"

    def test_floor_to_interval_30min(self):
        """30 分钟间隔对齐。"""
        from app.adapters.scheduler.local_scheduler import _floor_to_interval

        dt = datetime(2025, 1, 15, 10, 45, 0, tzinfo=timezone.utc)
        slot = _floor_to_interval(dt, 30)
        assert slot == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_floor_to_interval_with_minutes(self):
        """带分钟的时间槽对齐。"""
        from app.adapters.scheduler.local_scheduler import _floor_to_interval

        # 90 分钟间隔
        dt = datetime(2025, 1, 15, 11, 20, 0, tzinfo=timezone.utc)
        slot = _floor_to_interval(dt, 90)
        # 11:20 -> floor to 90min boundary -> 10:30
        assert slot == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


class TestLocalSchedulerRunOnce:
    """手动触发一次主动回合。"""

    def test_run_once_calls_turn_runner(self):
        """run_once 直接调用 turn_runner.run()。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        runner.run.return_value = ActiveTurnResult(
            trigger_id="timer:test",
            outcome="no_message",
        )

        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=60,
            instruction="test instruction",
            enabled=False,
        )

        result = scheduler.run_once()

        assert runner.run.call_count == 1
        trigger = runner.run.call_args[0][0]
        assert isinstance(trigger, TimerTrigger)
        assert trigger.type == "timer"
        assert trigger.instruction == "test instruction"
        assert trigger.trigger_id.startswith("timer:")
        assert result.outcome == "no_message"
        scheduler.shutdown()

    def test_run_once_with_message(self):
        """run_once 返回 message_enqueued。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        runner.run.return_value = ActiveTurnResult(
            trigger_id="timer:test",
            outcome="message_enqueued",
            event_id="evt-123",
        )

        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=60,
            instruction="test",
            enabled=False,
        )

        result = scheduler.run_once()
        assert result.outcome == "message_enqueued"
        assert result.event_id == "evt-123"
        scheduler.shutdown()

    def test_run_once_catches_exception(self):
        """run_once 异常不传播，返回 failed。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        runner.run.side_effect = RuntimeError("upstream error")

        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=60,
            instruction="test",
            enabled=False,
        )

        result = scheduler.run_once()
        assert result.outcome == "failed"
        scheduler.shutdown()

    def test_run_once_skips_when_locked(self):
        """lock 已持有时跳过。"""
        from app.adapters.scheduler.local_scheduler import LocalScheduler

        runner = MagicMock()
        runner.run.return_value = ActiveTurnResult(
            trigger_id="timer:test",
            outcome="no_message",
        )

        scheduler = LocalScheduler(
            turn_runner=runner,
            interval_minutes=60,
            instruction="test",
            enabled=False,
        )

        # 手动持锁
        scheduler._active_lock.acquire()
        try:
            result = scheduler.run_once()
            assert result.outcome == "skipped"
            assert runner.run.call_count == 0
        finally:
            scheduler._active_lock.release()
            scheduler.shutdown()
