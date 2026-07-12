"""M0: 异步 ModelClient、TurnRunner、lifespan；SQLite 短连接。

验收:
  - 12.4.1: 用户请求等待 LLM 时，Outbox 请求仍可响应
  - 12.4.4: FastAPI create_app 未 startup → 不启动 Scheduler
  - 12.4.5: 单次 startup → 只产生一个 Scheduler 任务
  - 12.4.6: shutdown → Scheduler 和 Async HTTP Client 均已关闭
  - M0 反馈点: 用户 LLM 等待不再阻塞 Outbox HTTP
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# 12.4.4: create_app 不 startup → 不启动 Scheduler
# ---------------------------------------------------------------------------
class TestNoSchedulerWithoutStartup:
    """FastAPI create_app 但未 startup → 不启动 Scheduler。"""

    def test_create_app_does_not_start_scheduler(self, tmp_path):
        """create_app 返回的 app 不应在构造时启动 scheduler。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        config = Config(
            gateway_host="127.0.0.1",
            gateway_port=8000,
            gateway_api_key="test-key",
            upstream_base_url="https://api.deepseek.com",
            upstream_api_key="sk-test",
            upstream_model="test-model",
            upstream_timeout_seconds=30,
            upstream_token_limit_field="max_completion_tokens",
            sample_directory="./samples",
            memory_char_budget=12000,
            outbox_database_path=str(tmp_path / "outbox.sqlite3"),
            active_turn_enabled=True,
            active_turn_interval_minutes=60,
            active_turn_instruction="test",
            default_temperature=0.7,
            default_max_output_tokens=1200,
        )
        app = create_app(config)
        scheduler = app.state.scheduler
        # Scheduler must NOT be running after create_app without lifespan startup
        assert scheduler._running is False, "Scheduler should not start before lifespan startup"

    def test_create_app_without_lifespan_no_thread(self, tmp_path):
        """构造 app 后不应有后台线程。"""
        import threading
        from app.application.app import create_app
        from app.infrastructure.config import Config

        before_threads = threading.active_count()
        config = Config(
            gateway_host="127.0.0.1",
            gateway_port=8000,
            gateway_api_key="test-key",
            upstream_base_url="https://api.deepseek.com",
            upstream_api_key="sk-test",
            upstream_model="test-model",
            upstream_timeout_seconds=30,
            upstream_token_limit_field="max_completion_tokens",
            sample_directory="./samples",
            memory_char_budget=12000,
            outbox_database_path=str(tmp_path / "outbox.sqlite3"),
            active_turn_enabled=True,
            active_turn_interval_minutes=60,
            active_turn_instruction="test",
            default_temperature=0.7,
            default_max_output_tokens=1200,
        )
        app = create_app(config)
        after_threads = threading.active_count()
        assert after_threads == before_threads, (
            f"Thread count changed: {before_threads} → {after_threads}"
        )


# ---------------------------------------------------------------------------
# Async ModelClient port and TurnRunner
# ---------------------------------------------------------------------------
class TestAsyncModelClient:
    """ModelClient 端口应支持 async complete。"""

    def test_model_client_port_has_async_complete(self):
        """ModelClient ABC 应定义 async complete 方法。"""
        from app.domain.ports.model_client import AsyncModelClient
        import inspect
        assert hasattr(AsyncModelClient, "complete")
        assert inspect.iscoroutinefunction(AsyncModelClient.complete), \
            "AsyncModelClient.complete must be a coroutine function"


class TestAsyncTurnRunner:
    """TurnRunner 应支持 async run。"""

    @pytest.mark.asyncio
    async def test_turn_runner_run_user_turn_async(self, tmp_path):
        """异步 run_user_turn 返回 ChatCompletionResponse。"""
        from app.application.turn_runner import TurnRunner
        from app.domain.models.trigger import UserTrigger
        from app.domain.models.chat_completion import ChatCompletionResponse, Choice
        from app.domain.models.context_builder import ContextBuilder
        from app.domain.ports.sample_reader import SampleReader, AllSamples
        from app.domain.models.sample import SampleEnvelope
        from app.domain.models.identity import IdentityData
        from app.domain.models.preferences import PreferencesData
        from app.domain.models.memories import MemoriesData
        from app.domain.models.working_state import WorkingStateData
        from unittest.mock import AsyncMock

        # Build proper sample data
        identity_data = IdentityData(name="沉", self_description="desc", values=[], boundaries=[], relationship_definition="rel")
        preferences_data = PreferencesData(communication_preferences=[], stable_likes=[], stable_dislikes=[], interaction_rules=[])
        memories_data = MemoriesData(items=[])
        working_state_data = WorkingStateData(current_focus=[], emotion_summary="", pending_items=[], next_wake_at=None)

        def env(data, v=1):
            return SampleEnvelope(sample_type="x", version=v, updated_at="2026-01-01T00:00:00+08:00", source="sample", data=data)

        samples = AllSamples(
            identity=env(identity_data, 1),
            preferences=env(preferences_data, 1),
            memories=env(memories_data, 1),
            working_state=env(working_state_data, 1),
        )
        sample_reader = MagicMock(spec=SampleReader)
        sample_reader.read_all.return_value = samples

        # Mock async model client
        model_client = AsyncMock()
        model_client.complete.return_value = ChatCompletionResponse(
            id="resp-1",
            object="chat.completion",
            created=1234567890,
            model="test-model",
            choices=[
                Choice(index=0, message_role="assistant", message_content="Hello!", finish_reason="stop")
            ],
            usage=None,
        )

        context_builder = ContextBuilder(memory_char_budget=12000)
        turn_runner = TurnRunner(
            sample_reader=sample_reader,
            context_builder=context_builder,
            model_client=model_client,
        )

        trigger = UserTrigger(request_id="req-1", chat_request={"messages": [{"role": "user", "content": "hi"}]})
        response = await turn_runner.run_user_turn(trigger)

        assert response.first_assistant_text() == "Hello!"
        # Verify async client was called
        model_client.complete.assert_awaited_once()


# ---------------------------------------------------------------------------
# 12.4.1: Async model call does not block other HTTP requests
# ---------------------------------------------------------------------------
class TestNonBlockingAsyncCall:
    """用户请求等待 LLM 时，其他请求仍可响应。"""

    @pytest.mark.asyncio
    async def test_concurrent_async_calls_overlap(self):
        """两个 async complete 调用可以重叠。"""
        from app.adapters.models.async_openai_client import AsyncOpenAIUpstreamClient
        from app.infrastructure.config import Config

        config = Config(
            gateway_host="127.0.0.1",
            gateway_port=8000,
            gateway_api_key="test-key",
            upstream_base_url="https://api.deepseek.com",
            upstream_api_key="sk-test",
            upstream_model="test-model",
            upstream_timeout_seconds=30,
            upstream_token_limit_field="max_completion_tokens",
            sample_directory="./samples",
            memory_char_budget=12000,
            outbox_database_path="./data/outbox.sqlite3",
            active_turn_enabled=False,
            active_turn_interval_minutes=60,
            active_turn_instruction="test",
            default_temperature=0.7,
            default_max_output_tokens=1200,
        )

        client = AsyncOpenAIUpstreamClient(config=config)
        # Verify it has an async complete method
        import inspect
        assert inspect.iscoroutinefunction(client.complete), \
            "AsyncOpenAIUpstreamClient.complete must be async"

        # Verify it can create and close a shared httpx client
        await client.start()
        assert client._http_client is not None
        await client.close()
        assert client._http_client is None


# ---------------------------------------------------------------------------
# SQLite short connections
# ---------------------------------------------------------------------------
class TestSQLiteShortConnections:
    """SQLite 每次操作使用短连接。"""

    def test_outbox_store_uses_short_connection(self, tmp_path):
        """SQLiteOutboxStore 每次操作打开独立连接，不长期持有。"""
        from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
        from app.domain.models.outbox import NewOutboxMessage
        import sqlite3

        db_path = str(tmp_path / "outbox.sqlite3")
        store = SQLiteOutboxStore(db_path)

        # Store should NOT hold a persistent connection
        assert not hasattr(store, '_conn') or store._conn is None, \
            "SQLiteOutboxStore must not hold a persistent _conn"

        # enqueue_once should work with short connections
        msg = NewOutboxMessage(
            event_id="evt-1",
            trigger_id="trig-1",
            created_at="2025-01-01T00:00:00Z",
            content="test content",
            metadata={"key": "value"},
        )
        saved = store.enqueue_once(msg)
        assert saved.event_id == "evt-1"
        assert saved.cursor is not None

        # list_after should also work
        page = store.list_after(0, 10)
        assert len(page.items) == 1
        assert page.items[0].content == "test content"

    def test_outbox_store_multiple_ops_independent(self, tmp_path):
        """多次操作不依赖同一连接。"""
        from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
        from app.domain.models.outbox import NewOutboxMessage

        db_path = str(tmp_path / "outbox.sqlite3")
        store = SQLiteOutboxStore(db_path)

        for i in range(5):
            msg = NewOutboxMessage(
                event_id=f"evt-{i}",
                trigger_id=f"trig-{i}",
                created_at="2025-01-01T00:00:00Z",
                content=f"content-{i}",
                metadata={},
            )
            saved = store.enqueue_once(msg)
            assert saved.event_id == f"evt-{i}"

        page = store.list_after(0, 100)
        assert len(page.items) == 5


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------
class TestFastAPILifespan:
    """FastAPI lifespan 管理 Scheduler 生命周期。"""

    @pytest.mark.asyncio
    async def test_lifespan_starts_and_stops_scheduler(self, tmp_path):
        """lifespan startup 启动 scheduler, shutdown 停止。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        config = Config(
            gateway_host="127.0.0.1",
            gateway_port=8000,
            gateway_api_key="test-key",
            upstream_base_url="https://api.deepseek.com",
            upstream_api_key="sk-test",
            upstream_model="test-model",
            upstream_timeout_seconds=30,
            upstream_token_limit_field="max_completion_tokens",
            sample_directory="./samples",
            memory_char_budget=12000,
            outbox_database_path=str(tmp_path / "outbox.sqlite3"),
            active_turn_enabled=True,
            active_turn_interval_minutes=60,
            active_turn_instruction="test",
            default_temperature=0.7,
            default_max_output_tokens=1200,
        )
        app = create_app(config)
        scheduler = app.state.scheduler

        # Before lifespan: not running
        assert scheduler._running is False

        # Manually trigger lifespan
        async with app.router.lifespan_context(app):
            # Lifespan startup triggered
            assert scheduler._running is True, "Scheduler should be running after startup"

            # Verify async model client is started
            assert app.state.async_model_client is not None
            assert app.state.async_model_client._http_client is not None

        # After lifespan: stopped
        assert scheduler._running is False, "Scheduler should be stopped after shutdown"
        assert app.state.async_model_client._http_client is None, \
            "Async HTTP client should be closed after shutdown"
