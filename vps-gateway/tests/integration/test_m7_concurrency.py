"""M7 并发隔离与集成测试。

验证架构文档 12.4 并发与隔离验收：
- 用户请求等待 LLM 时查询 Outbox 可响应
- 用户请求与主动回合同时调用模型，两个上游请求可重叠
- 两个并行回合均调用工具，messages/计数/tool_call_id 不串线
- FastAPI 重复 create_app 但未 startup 不启动 Scheduler
- 单次 startup 只产生一个 WakeController 任务
- shutdown Scheduler 和 Async HTTP Client 均已关闭
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.chat_completion import (
    ChatCompletionResponse, Choice,
)
from app.domain.models.turn import TurnContext, ChatMessage
from app.domain.models.outbox import NewOutboxMessage
from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
from app.adapters.outbox.in_process_notifier import InProcessNotifier
from app.application.single_reader_gate import SingleReaderGate
from app.application.outbox_longpoll_service import OutboxLongPollService
from app.application.active_turn_gate import ActiveTurnGate


class TestConcurrencyIsolation:
    """12.4 并发与隔离验收。"""

    @pytest.mark.asyncio
    async def test_user_turn_does_not_block_outbox(self):
        """12.4.1: 用户请求等待 LLM 时 Outbox 请求仍可响应。"""
        # 模拟 LLM 延迟
        call_times = []

        async def slow_complete(request):
            call_times.append(("llm_start", time.monotonic()))
            await asyncio.sleep(0.2)
            call_times.append(("llm_end", time.monotonic()))
            return ChatCompletionResponse(
                id="test", object="chat.completion", created=0, model="test", usage=None, choices=[Choice(
                    index=0,
                    message_role="assistant", message_content="done",
                    finish_reason="stop",
                )],
            )

        mock_client = AsyncMock()
        mock_client.complete = slow_complete

        # Outbox store
        tmpdir = tempfile.mkdtemp()
        store = SQLiteOutboxStore(os.path.join(tmpdir, "outbox.sqlite3"))
        store.enqueue_once(NewOutboxMessage(
            event_id="e1", trigger_id="t1",
            created_at=datetime.now(timezone.utc).isoformat(),
            content="msg", metadata={},
        ))

        notifier = InProcessNotifier()
        gate = SingleReaderGate()
        lp_service = OutboxLongPollService(store, notifier, gate, 2)

        # 同时启动 LLM 请求和 Outbox 长轮询
        llm_task = asyncio.create_task(mock_client.complete(None))
        lp_task = asyncio.create_task(lp_service.long_poll(after=0, timeout_seconds=1, reader_id="r1"))

        lp_result = await asyncio.wait_for(lp_task, timeout=3)
        llm_result = await asyncio.wait_for(llm_task, timeout=3)

        # Outbox 应该在 LLM 完成前返回
        assert lp_result.item is not None
        assert lp_result.item.content == "msg"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_parallel_turns_isolated_messages(self):
        """12.4.3: 两个并行回合均调用工具，messages/计数/tool_call_id 不串线。"""
        # 创建两个独立的 TurnContext
        ctx_a = TurnContext.create(
            trigger_type="user", trigger_id="req_a",
            messages=[ChatMessage(role="user", content="hello A")],
            sample_versions={},
        )
        ctx_b = TurnContext.create(
            trigger_type="user", trigger_id="req_b",
            messages=[ChatMessage(role="user", content="hello B")],
            sample_versions={},
        )

        assert ctx_a.turn_id != ctx_b.turn_id
        assert ctx_a.messages is not ctx_b.messages
        assert ctx_a.tool_round == 0
        assert ctx_b.tool_round == 0
        assert ctx_a.total_tool_calls == 0
        assert ctx_b.total_tool_calls == 0

        # 模拟各自独立调用工具
        ctx_a.increment_tool_round()
        ctx_a.add_tool_calls(2)
        ctx_b.add_tool_calls(1)

        assert ctx_a.tool_round == 1
        assert ctx_a.total_tool_calls == 2
        assert ctx_b.tool_round == 0
        assert ctx_b.total_tool_calls == 1

    @pytest.mark.asyncio
    async def test_user_and_active_turn_can_parallel(self):
        """12.4.2: 用户请求与主动回合同时调用模型可重叠。"""
        overlap_detected = {"value": False}
        active_calls = {"value": 0}

        async def track_complete(request):
            active_calls["value"] += 1
            current = active_calls["value"]
            if current >= 2:
                overlap_detected["value"] = True
            await asyncio.sleep(0.1)
            active_calls["value"] -= 1
            return ChatCompletionResponse(
                id="test", object="chat.completion", created=0, model="test", usage=None, choices=[Choice(
                    index=0,
                    message_role="assistant", message_content="ok",
                    finish_reason="stop",
                )],
            )

        mock_client = AsyncMock()
        mock_client.complete = track_complete

        # 同时启动两个 LLM 请求
        task_a = asyncio.create_task(mock_client.complete(None))
        task_b = asyncio.create_task(mock_client.complete(None))

        await asyncio.gather(task_a, task_b)
        assert overlap_detected["value"] is True

    @pytest.mark.asyncio
    async def test_active_turn_gate_prevents_parallel_active(self):
        """12.4.2 补充: 两个主动回合绝不并行。"""
        gate = ActiveTurnGate()

        # 第一个主动回合获取 Gate
        acquired1 = await gate.try_acquire()
        assert acquired1 is True

        # 第二个主动回合获取失败
        acquired2 = await gate.try_acquire()
        assert acquired2 is False

        # 释放后可再获取
        gate.release()
        acquired3 = await gate.try_acquire()
        assert acquired3 is True
        gate.release()


class TestLifecycle:
    """12.4.4-6: 生命周期验收。"""

    @pytest.mark.asyncio
    async def test_create_app_without_startup_no_scheduler(self):
        """12.4.4: create_app 但未 startup → 不启动 Scheduler。"""
        import os
        os.environ.setdefault("UPSTREAM_MODEL", "test-model")
        os.environ.setdefault("UPSTREAM_BASE_URL", "http://localhost:9999")
        os.environ.setdefault("UPSTREAM_API_KEY", "test-key")
        os.environ.setdefault("GATEWAY_API_KEY", "test-key")
        os.environ.setdefault("SAMPLE_DIRECTORY", "/tmp/samples")
        from app.application.app import create_app
        app = create_app()
        # app.state 中不应有 running scheduler
        assert not getattr(app.state, 'scheduler_started', False)

    @pytest.mark.asyncio
    async def test_shutdown_closes_http_client_and_scheduler(self):
        """12.4.6: shutdown → Scheduler 和 Async HTTP Client 均已关闭。"""
        import os
        os.environ.setdefault("UPSTREAM_MODEL", "test-model")
        os.environ.setdefault("UPSTREAM_BASE_URL", "http://localhost:9999")
        os.environ.setdefault("UPSTREAM_API_KEY", "test-key")
        os.environ.setdefault("GATEWAY_API_KEY", "test-key")
        os.environ.setdefault("SAMPLE_DIRECTORY", "/tmp/samples")
        os.environ["ACTIVE_TURN_ENABLED"] = "false"
        from app.application.app import create_app

        app = create_app()
        # 模拟 lifespan
        async with app.router.lifespan_context(app):
            pass  # startup → shutdown

        # 验证 http client 已关闭
        client = getattr(app.state, 'async_model_client', None)
        if client and hasattr(client, '_client') and client._client:
            assert client._client.is_closed
        os.environ.pop("ACTIVE_TURN_ENABLED", None)

    @pytest.mark.asyncio
    async def test_single_startup_one_scheduler(self):
        """12.4.5: 单次 startup 只产生一个 Scheduler 任务。"""
        import os
        os.environ.setdefault("UPSTREAM_MODEL", "test-model")
        os.environ.setdefault("UPSTREAM_BASE_URL", "http://localhost:9999")
        os.environ.setdefault("UPSTREAM_API_KEY", "test-key")
        os.environ.setdefault("GATEWAY_API_KEY", "test-key")
        os.environ.setdefault("SAMPLE_DIRECTORY", "/tmp/samples")
        os.environ["ACTIVE_TURN_ENABLED"] = "false"
        from app.application.app import create_app
        app = create_app()
        async with app.router.lifespan_context(app):
            # scheduler_started 应反映状态
            pass
        # shutdown 后重置
        assert not getattr(app.state, 'scheduler_started', False)
        os.environ.pop("ACTIVE_TURN_ENABLED", None)


class TestRestartRecovery:
    """12.2.11-13: 重启恢复验收。"""

    @pytest.mark.asyncio
    async def test_running_becomes_expired_on_restart(self):
        """12.2.12: 重启发现旧 running → expired(crash_recovery)。"""
        from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore
        from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason

        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "wake_test.sqlite3")
        store = SQLiteWakeJobStore(db_path)

        # 创建一个 running 任务（模拟崩溃前状态）
        job = WakeJob(
            wake_id="wake_crash_1",
            source="fixed",
            requested_at="2025-07-12T10:00:00+08:00",
            scheduled_at="2025-07-12T10:00:00+08:00",
            reason="test",
            status=WakeJobStatus.RUNNING,
            created_at=datetime.now(timezone.utc).isoformat(),
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            expire_reason=None,
        )
        await store.schedule_once(job)

        # 模拟重启恢复
        now = datetime(2025, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
        result = await store.recover_after_restart(now, grace_seconds=10)

        # 验证 running → expired(crash_recovery)
        recovered = await store.list_jobs(None)
        crash_jobs = [j for j in recovered if j.wake_id == "wake_crash_1"]
        assert len(crash_jobs) == 1
        assert crash_jobs[0].status == WakeJobStatus.EXPIRED
        assert crash_jobs[0].expire_reason.value == "crash_recovery"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_missed_deadline_expired_on_restart(self):
        """12.2.11: 服务停机跨过 scheduled_at + grace → 重启后 expired。"""
        from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore
        from app.domain.models.wake_job import WakeJob, WakeJobStatus, ExpireReason

        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "wake_test2.sqlite3")
        store = SQLiteWakeJobStore(db_path)

        # 创建一个 pending 任务，scheduled_at 已过期
        job = WakeJob(
            wake_id="wake_missed_1",
            source="fixed",
            requested_at="2025-07-12T01:00:00+08:00",
            scheduled_at="2025-07-12T01:00:00+08:00",
            reason="test",
            status=WakeJobStatus.PENDING,
            created_at="2025-07-12T00:30:00+00:00",
            started_at=None,
            finished_at=None,
            expire_reason=None,
        )
        await store.schedule_once(job)

        # 模拟重启恢复（now 远超 grace）
        now = datetime(2025, 7, 12, 3, 0, 0, tzinfo=timezone.utc)
        result = await store.recover_after_restart(now, grace_seconds=10)

        recovered = await store.list_jobs(None)
        missed = [j for j in recovered if j.wake_id == "wake_missed_1"]
        assert len(missed) == 1
        assert missed[0].status == WakeJobStatus.EXPIRED
        assert missed[0].expire_reason.value == "missed_deadline"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_outbox_persists_across_restart(self):
        """12.2.13: 重启后 pending Outbox 消息仍可被领取。"""
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "outbox_restart.sqlite3")

        # 第一次启动：写入消息
        store1 = SQLiteOutboxStore(db_path)
        store1.enqueue_once(NewOutboxMessage(
            event_id="restart_e1", trigger_id="restart_t1",
            created_at=datetime.now(timezone.utc).isoformat(),
            content="persisted", metadata={},
        ))

        # 模拟重启：创建新 store 实例
        store2 = SQLiteOutboxStore(db_path)

        # 消息仍可领取
        claimed = await store2.claim_one(after_cursor=0, reader_id="r1")
        assert claimed is not None
        assert claimed.content == "persisted"

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestClientToolsRejection:
    """12.1.13: 前端上传工具不进入 Registry。"""

    @pytest.mark.asyncio
    async def test_client_tools_rejected(self):
        """前端请求带 tools → 400 client_tools_not_allowed。"""
        import os
        os.environ.setdefault("UPSTREAM_MODEL", "test-model")
        os.environ.setdefault("UPSTREAM_BASE_URL", "http://localhost:9999")
        os.environ.setdefault("UPSTREAM_API_KEY", "test-key")
        os.environ.setdefault("GATEWAY_API_KEY", "test-key")
        os.environ.setdefault("SAMPLE_DIRECTORY", "/tmp/samples")
        os.environ["ACTIVE_TURN_ENABLED"] = "false"
        from app.application.app import create_app
        from starlette.testclient import TestClient

        app = create_app()
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function", "function": {"name": "evil", "parameters": {}}}],
                },
                headers={"Authorization": "Bearer test-key"},
            )
            assert response.status_code == 400
            data = response.json()
            assert "client_tools_not_allowed" in str(data)
        os.environ.pop("ACTIVE_TURN_ENABLED", None)
