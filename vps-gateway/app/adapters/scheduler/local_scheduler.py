"""LocalScheduler: 本地定时主动回合调度器。

数据合同来源：架构文档 6.9 LocalScheduler。

职责：
  1. 在本地按固定间隔产生主动回合触发
  2. 生成稳定 trigger_id
  3. 同一时刻只允许一个主动回合运行
  4. 上一次未结束时跳过本次
  5. 失败后记录日志，不立即重试

数据输入:
  - enabled: bool (ACTIVE_TURN_ENABLED)
  - interval_minutes: int (ACTIVE_TURN_INTERVAL_MINUTES)
  - instruction: str (ACTIVE_TURN_INSTRUCTION)
  - turn_runner: TurnRunner

数据输出:
  - TimerTrigger (传给 turn_runner)
  - 日志: timer_slot_started, active_turn_completed, active_turn_failed, skip_timer_slot
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from app.domain.models.trigger import TimerTrigger
from app.domain.models.turn import PreparedTurn, ModelCompletionInput
from app.application.turn_runner import ActiveTurnResult
from app.infrastructure.logging import get_logger


def _floor_to_interval(dt: datetime, interval_minutes: int) -> datetime:
    """将时间对齐到 interval 边界。

    数据输入: dt (datetime), interval_minutes (int)
    指令: epoch timestamp 对 interval_seconds 取整
    数据输出: 对齐后的 datetime
    """
    interval_seconds = interval_minutes * 60
    epoch = int(dt.timestamp())
    floored_epoch = (epoch // interval_seconds) * interval_seconds
    return datetime.fromtimestamp(floored_epoch, tz=timezone.utc)


class LocalScheduler:
    """本地定时主动回合调度器。

    首版使用单进程 daemon 线程。
    """

    def __init__(
        self,
        turn_runner,
        interval_minutes: int,
        instruction: str,
        enabled: bool,
    ):
        self._turn_runner = turn_runner
        self._interval_seconds = interval_minutes * 60
        self._instruction = instruction
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._logger = get_logger("scheduler")

        if enabled:
            self.start()

    def start(self):
        """启动后台线程。"""
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._logger.info("scheduler_started",
                          extra={"interval_minutes": self._interval_seconds // 60})

    def shutdown(self):
        """停止后台线程。"""
        self._running = False
        self._stop_event.set()
        self._logger.info("scheduler_stopped")

    def run_once(self) -> ActiveTurnResult:
        """手动执行一次主动回合。

        数据输入: 无（使用当前时间槽）
        指令:
          1. 生成 trigger_id
          2. 检查锁 — 已锁则跳过
          3. 调用 turn_runner.run(TimerTrigger)
          4. 异常不传播，返回 failed
        数据输出: ActiveTurnResult
        """
        slot = _floor_to_interval(datetime.now(timezone.utc), self._interval_seconds // 60)
        trigger_id = "timer:" + slot.isoformat()

        if self._active_lock.locked():
            self._logger.info("skip_timer_slot",
                              extra={"trigger_id": trigger_id, "reason": "previous_turn_running"})
            return ActiveTurnResult(
                trigger_id=trigger_id,
                outcome="skipped",
            )

        with self._active_lock:
            try:
                trigger = TimerTrigger(
                    type="timer",
                    trigger_id=trigger_id,
                    fired_at=datetime.now(timezone.utc).isoformat(),
                    instruction=self._instruction,
                )
                self._logger.info("timer_slot_started",
                                  extra={"trigger_id": trigger_id})

                result = self._turn_runner.run(trigger)
                self._logger.info("active_turn_completed",
                                  extra={"trigger_id": trigger_id, "outcome": result.outcome})
                return result

            except Exception as e:
                self._logger.info("active_turn_failed",
                                  extra={"trigger_id": trigger_id, "error": str(e)})
                return ActiveTurnResult(
                    trigger_id=trigger_id,
                    outcome="failed",
                )

    def _loop(self):
        """后台循环。

        指令:
          1. 先等待一个完整间隔（不立即触发）
          2. 到达时间槽时执行 run_once()
          3. 失败不传播，继续下一周期
        """
        while self._running:
            self._stop_event.wait(self._interval_seconds)
            if not self._running:
                break
            try:
                self.run_once()
            except Exception as e:
                # run_once 内部已捕获，这里做兜底
                self._logger.info("scheduler_loop_error",
                                  extra={"error": str(e)})
