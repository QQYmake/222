"""SQLiteWakeJobStore：持久化唤醒任务存储。

数据合同来源：架构文档 6.9 WakeJobStore。

指令：
  1. 每次操作使用独立 SQLite 短连接
  2. 相同 wake_id 幂等写入
  3. 按 scheduled_at、created_at 查询到期 pending 任务
  4. 状态转换使用事务条件，防止同一任务重复启动
  5. 不在等待期间持有连接
"""
from __future__ import annotations

import logging
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason, RecoveryResult
from app.domain.ports.wake_job_store import WakeJobStore as WakeJobStorePort

logger = logging.getLogger(__name__)


class SQLiteWakeJobStore(WakeJobStorePort):
    """SQLite 持久化唤醒任务存储。

    架构不变量 15：SQLite 不得把同一个 connection 长期共享给 HTTP 与 Scheduler。
    每次操作打开独立短连接，操作结束立即关闭。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self):
        d = os.path.dirname(self._db_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        """每次操作创建独立短连接。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        """初始化表结构。"""
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wake_jobs (
                    wake_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    finished_at TEXT,
                    expire_reason TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_wake_status_scheduled
                ON wake_jobs(status, scheduled_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_wake_status_created
                ON wake_jobs(status, created_at)
            """)
        finally:
            conn.close()

    async def schedule_once(self, job: WakeJob) -> WakeJob:
        """幂等写入一条 WakeJob。

        相同 wake_id 重复提交不产生第二条记录，返回已有记录。
        """
        conn = self._connect()
        try:
            # 先检查是否已存在
            existing = conn.execute(
                "SELECT * FROM wake_jobs WHERE wake_id = ?", (job.wake_id,)
            ).fetchone()
            if existing is not None:
                return WakeJob.from_dict(dict(existing))

            # 设置 created_at
            if not job.created_at:
                job.created_at = datetime.now(timezone.utc).isoformat()

            conn.execute(
                """INSERT INTO wake_jobs
                   (wake_id, source, requested_at, scheduled_at, reason, status, created_at, started_at, finished_at, expire_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.wake_id, job.source, job.requested_at, job.scheduled_at,
                    job.reason, job.status.value, job.created_at,
                    job.started_at, job.finished_at,
                    job.expire_reason.value if job.expire_reason else None,
                ),
            )
            logger.info("wake_scheduled wake_id=%s scheduled_at=%s source=%s",
                        job.wake_id, job.scheduled_at, job.source)
            return job
        finally:
            conn.close()

    async def due_jobs(self, now: datetime, grace_seconds: int) -> list[WakeJob]:
        """查询到期 pending 任务，按 scheduled_at、created_at 排序。"""
        now_iso = now.isoformat()
        grace_deadline = (now - timedelta(seconds=grace_seconds)).isoformat()
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM wake_jobs
                   WHERE status = 'pending' AND scheduled_at <= ?
                   ORDER BY scheduled_at ASC, created_at ASC""",
                (now_iso,),
            ).fetchall()
            return [WakeJob.from_dict(dict(r)) for r in rows]
        finally:
            conn.close()

    async def transition(
        self,
        wake_id: str,
        expected: WakeJobStatus,
        target: WakeJobStatus,
        reason: Optional[ExpireReason] = None,
    ) -> bool:
        """条件状态转换，防止重复启动。"""
        conn = self._connect()
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            started_at = now_iso if target == WakeJobStatus.RUNNING else None
            finished_at = now_iso if target in (WakeJobStatus.COMPLETED, WakeJobStatus.EXPIRED, WakeJobStatus.FAILED) else None
            expire_reason_val = reason.value if reason else None

            # UPDATE ... WHERE status = expected（条件转换）
            cursor = conn.execute(
                """UPDATE wake_jobs
                   SET status = ?, started_at = COALESCE(?, started_at),
                       finished_at = COALESCE(?, finished_at),
                       expire_reason = COALESCE(?, expire_reason)
                   WHERE wake_id = ? AND status = ?""",
                (
                    target.value,
                    started_at,
                    finished_at,
                    expire_reason_val,
                    wake_id,
                    expected.value,
                ),
            )
            return cursor.rowcount > 0
        finally:
            conn.close()

    async def list_jobs(
        self,
        status: Optional[WakeJobStatus] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[WakeJob]:
        """查询任务列表，按 scheduled_at 升序返回。"""
        conn = self._connect()
        try:
            query = "SELECT * FROM wake_jobs WHERE 1=1"
            params: list = []
            if status is not None:
                query += " AND status = ?"
                params.append(status.value)
            if after is not None:
                query += " AND scheduled_at >= ?"
                params.append(after)
            if before is not None:
                query += " AND scheduled_at < ?"
                params.append(before)
            query += " ORDER BY scheduled_at ASC"
            rows = conn.execute(query, params).fetchall()
            return [WakeJob.from_dict(dict(r)) for r in rows]
        finally:
            conn.close()

    async def cancel(self, wake_id: str) -> WakeJob:
        """取消任务，只允许 pending → cancelled。"""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE wake_jobs SET status = 'cancelled' WHERE wake_id = ? AND status = 'pending'",
                (wake_id,),
            )
            if cursor.rowcount == 0:
                # 返回当前状态（可能 running/completed/expired）
                pass
            row = conn.execute(
                "SELECT * FROM wake_jobs WHERE wake_id = ?", (wake_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"wake_id not found: {wake_id}")
            return WakeJob.from_dict(dict(row))
        finally:
            conn.close()

    async def get_job(self, wake_id: str) -> Optional[WakeJob]:
        """获取单条任务。"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM wake_jobs WHERE wake_id = ?", (wake_id,)
            ).fetchone()
            if row is None:
                return None
            return WakeJob.from_dict(dict(row))
        finally:
            conn.close()

    async def recover_after_restart(self, now: datetime, grace_seconds: int) -> RecoveryResult:
        """重启恢复。

        架构文档 6.17 AppLifecycle：
        - 遗留 running → expired(crash_recovery)
        - pending 且已超过 START_GRACE → expired(missed_deadline)
        - 宽限内 pending 保留供正常扫描
        """
        result = RecoveryResult()
        conn = self._connect()
        try:
            now_iso = now.isoformat()
            grace_deadline_iso = (now - timedelta(seconds=grace_seconds)).isoformat()

            # 1. running → expired(crash_recovery)
            running_rows = conn.execute(
                "SELECT * FROM wake_jobs WHERE status = 'running'"
            ).fetchall()
            for row in running_rows:
                conn.execute(
                    """UPDATE wake_jobs SET status = 'expired',
                       finished_at = ?, expire_reason = 'crash_recovery'
                       WHERE wake_id = ? AND status = 'running'""",
                    (now_iso, row["wake_id"]),
                )
                result.running_expired += 1
                logger.info("wake_expired wake_id=%s reason=crash_recovery", row["wake_id"])

            # 2. pending 超过 grace → expired(missed_deadline)
            missed_rows = conn.execute(
                """SELECT * FROM wake_jobs
                   WHERE status = 'pending' AND scheduled_at < ?""",
                (grace_deadline_iso,),
            ).fetchall()
            for row in missed_rows:
                conn.execute(
                    """UPDATE wake_jobs SET status = 'expired',
                       finished_at = ?, expire_reason = 'missed_deadline'
                       WHERE wake_id = ? AND status = 'pending'""",
                    (now_iso, row["wake_id"]),
                )
                result.missed_deadline_expired += 1
                logger.info("wake_expired wake_id=%s reason=missed_deadline", row["wake_id"])

            # 3. 宽限内 pending 保留
            grace_pending = conn.execute(
                """SELECT COUNT(*) as cnt FROM wake_jobs
                   WHERE status = 'pending' AND scheduled_at >= ?""",
                (grace_deadline_iso,),
            ).fetchone()
            result.grace_pending_retained = grace_pending["cnt"] if grace_pending else 0

            return result
        finally:
            conn.close()

    async def get_last_start_on_date(self, date_str: str) -> Optional[datetime]:
        """获取指定日期最近一次实际启动时间。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT started_at FROM wake_jobs
                   WHERE status IN ('completed', 'failed')
                   AND started_at LIKE ?
                   ORDER BY started_at DESC LIMIT 1""",
                (f"{date_str}%",),
            ).fetchall()
            if not rows:
                return None
            return datetime.fromisoformat(rows[0]["started_at"])
        finally:
            conn.close()

    async def count_starts_on_date(self, date_str: str) -> int:
        """获取指定日期实际启动次数。"""
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM wake_jobs
                   WHERE status IN ('completed', 'failed', 'running')
                   AND started_at LIKE ?""",
                (f"{date_str}%",),
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()
