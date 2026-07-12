"""M0: app.py v2 组件接线补全测试。

验证：
1. ToolRegistry.register_for_wake_only() 标记工具仅主动唤醒回合暴露
2. ToolRegistry.schemas_for_user() 排除 wake-only 工具
3. ToolRegistry.schemas_for_wake() 包含 wake-only 工具
4. TurnRunner 接受可选 tool_registry 和 model_tool_loop
5. TurnRunner 有 model_tool_loop 时使用工具循环
6. TurnRunner 无 model_tool_loop 时保持 v2 行为
7. app.py create_app 注入 ToolRegistry/WakeController/WakePlanner/ModelToolLoop
8. app.py create_app 注册唤醒工具
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.tool import ToolDefinition, ToolExecutor, ToolExecutionContext
from app.domain.models.chat_completion import (
    ChatCompletionResponse, Choice,
)
from app.domain.models.turn import ChatMessage
from app.adapters.tools.registry import ToolRegistry


def _make_tool_def(name: str, enabled: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"test tool {name}",
        parameters={"type": "object", "properties": {}},
        enabled_in_production=enabled,
        timeout_seconds=15,
        max_result_chars=4000,
    )


class _DummyExecutor(ToolExecutor):
    async def execute(self, arguments: dict, context: ToolExecutionContext):
        return {"ok": True}


class TestToolRegistryWakeOnly:
    """ToolRegistry.register_for_wake_only 测试。"""

    def test_register_for_wake_only_marks_tool(self):
        """register_for_wake_only 标记工具为仅唤醒回合可用。"""
        registry = ToolRegistry(test_tools_enabled=True)
        td = _make_tool_def("memory_recall")
        executor = _DummyExecutor()
        registry.register(td, executor)
        registry.register_for_wake_only("memory_recall")

        # schemas_for_user 不包含 wake-only
        user_schemas = registry.schemas_for_user()
        user_names = [s["function"]["name"] for s in user_schemas]
        assert "memory_recall" not in user_names

        # schemas_for_wake 包含 wake-only
        wake_schemas = registry.schemas_for_wake()
        wake_names = [s["function"]["name"] for s in wake_schemas]
        assert "memory_recall" in wake_names

    def test_schemas_for_user_includes_normal_tools(self):
        """schemas_for_user 包含普通注册工具。"""
        registry = ToolRegistry(test_tools_enabled=True)
        registry.register(_make_tool_def("schedule_wakeup"), _DummyExecutor())
        registry.register(_make_tool_def("normal_tool"), _DummyExecutor())

        user_schemas = registry.schemas_for_user()
        user_names = [s["function"]["name"] for s in user_schemas]
        assert "schedule_wakeup" in user_names
        assert "normal_tool" in user_names

    def test_schemas_for_wake_includes_all_tools(self):
        """schemas_for_wake 包含所有已注册工具（含 wake-only）。"""
        registry = ToolRegistry(test_tools_enabled=True)
        registry.register(_make_tool_def("schedule_wakeup"), _DummyExecutor())
        registry.register(_make_tool_def("memory_recall"), _DummyExecutor())
        registry.register_for_wake_only("memory_recall")

        wake_schemas = registry.schemas_for_wake()
        wake_names = [s["function"]["name"] for s in wake_schemas]
        assert "schedule_wakeup" in wake_names
        assert "memory_recall" in wake_names

    def test_register_for_wake_only_unknown_tool_raises(self):
        """对未注册工具调用 register_for_wake_only 抛 ValueError。"""
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="unknown tool"):
            registry.register_for_wake_only("nonexistent")

    def test_resolve_wake_only_tool_still_works(self):
        """wake-only 工具仍然可以通过 resolve(trigger_type='wake') 解析。"""
        registry = ToolRegistry(test_tools_enabled=True)
        registry.register(_make_tool_def("memory_recall"), _DummyExecutor())
        registry.register_for_wake_only("memory_recall")

        executor = registry.resolve("memory_recall", trigger_type="wake")
        assert executor is not None


class TestTurnRunnerToolIntegration:
    """TurnRunner 接受可选 tool_registry 和 model_tool_loop。"""

    @pytest.mark.asyncio
    async def test_turn_runner_accepts_optional_tool_registry(self):
        """TurnRunner 可接受可选 tool_registry 参数。"""
        from app.application.turn_runner import TurnRunner
        from app.domain.models.trigger import UserTrigger
        from app.domain.models.context_builder import ContextBuilder

        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value=ChatCompletionResponse(
            id="test", object="chat.completion", created=0, model="test",
            usage=None,
            choices=[Choice(index=0, message_role="assistant",
                            message_content="hello", finish_reason="stop")],
        ))

        mock_sample_reader = MagicMock()
        mock_sample_reader.read_all.return_value = MagicMock(
            identity=MagicMock(content="id", version=1),
            persona=MagicMock(content="p", version=1),
            memories=MagicMock(content="m", version=1),
            optional=MagicMock(content="", version=1),
        )

        registry = ToolRegistry(test_tools_enabled=True)

        runner = TurnRunner(
            sample_reader=mock_sample_reader,
            context_builder=ContextBuilder(memory_char_budget=12000),
            model_client=mock_client,
            tool_registry=registry,
        )

        assert runner.tool_registry is registry
        assert runner.model_tool_loop is None

    @pytest.mark.asyncio
    async def test_turn_runner_without_tool_registry_works(self):
        """TurnRunner 无 tool_registry 时保持 v2 行为。"""
        from app.application.turn_runner import TurnRunner
        from app.domain.models.trigger import UserTrigger
        from app.domain.models.context_builder import ContextBuilder

        mock_client = AsyncMock()
        mock_client.complete = AsyncMock(return_value=ChatCompletionResponse(
            id="test", object="chat.completion", created=0, model="test",
            usage=None,
            choices=[Choice(index=0, message_role="assistant",
                            message_content="hello", finish_reason="stop")],
        ))

        mock_sample_reader = MagicMock()
        mock_sample_reader.read_all.return_value = MagicMock(
            identity=MagicMock(content="id", version=1),
            persona=MagicMock(content="p", version=1),
            memories=MagicMock(content="m", version=1),
            optional=MagicMock(content="", version=1),
        )

        runner = TurnRunner(
            sample_reader=mock_sample_reader,
            context_builder=ContextBuilder(memory_char_budget=12000),
            model_client=mock_client,
        )

        assert runner.tool_registry is None
        assert runner.model_tool_loop is None

        trigger = UserTrigger(
            request_id="req-1",
            chat_request={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        response = await runner.run_user_turn(trigger)
        assert response.choices[0].message_content == "hello"


class TestAppWiring:
    """app.py create_app 组件接线测试。"""

    def test_create_app_wires_tool_registry(self):
        """create_app 创建 ToolRegistry 并注册唤醒工具。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": "8000",
                "GATEWAY_API_KEY": "test-key",
                "UPSTREAM_BASE_URL": "https://api.example.com",
                "UPSTREAM_API_KEY": "test-upstream-key",
                "UPSTREAM_MODEL": "test-model",
                "SAMPLE_DIRECTORY": os.path.join(tmpdir, "samples"),
                "OUTBOX_DATABASE_PATH": os.path.join(tmpdir, "outbox.sqlite3"),
                "WAKE_JOBS_DATABASE_PATH": os.path.join(tmpdir, "wake_jobs.sqlite3"),
            }
            with patch.dict(os.environ, env, clear=False):
                # Create sample files
                os.makedirs(os.path.join(tmpdir, "samples"), exist_ok=True)
                for name in ["identity", "persona", "memories", "optional"]:
                    with open(os.path.join(tmpdir, "samples", f"{name}.sample.json"), "w") as f:
                        import json
                        json.dump({"content": "test", "version": 1}, f)

                config = Config.load_from_env()
                app = create_app(config)

                assert hasattr(app.state, "tool_registry")
                assert app.state.tool_registry is not None
                # 唤醒工具已注册
                assert app.state.tool_registry.get_definition("schedule_wakeup") is not None
                assert app.state.tool_registry.get_definition("list_wakeups") is not None
                assert app.state.tool_registry.get_definition("cancel_wakeup") is not None

    def test_create_app_wires_wake_controller(self):
        """create_app 创建 WakeController。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": "8000",
                "GATEWAY_API_KEY": "test-key",
                "UPSTREAM_BASE_URL": "https://api.example.com",
                "UPSTREAM_API_KEY": "test-upstream-key",
                "UPSTREAM_MODEL": "test-model",
                "SAMPLE_DIRECTORY": os.path.join(tmpdir, "samples"),
                "OUTBOX_DATABASE_PATH": os.path.join(tmpdir, "outbox.sqlite3"),
                "WAKE_JOBS_DATABASE_PATH": os.path.join(tmpdir, "wake_jobs.sqlite3"),
            }
            with patch.dict(os.environ, env, clear=False):
                os.makedirs(os.path.join(tmpdir, "samples"), exist_ok=True)
                for name in ["identity", "persona", "memories", "optional"]:
                    with open(os.path.join(tmpdir, "samples", f"{name}.sample.json"), "w") as f:
                        import json
                        json.dump({"content": "test", "version": 1}, f)

                config = Config.load_from_env()
                app = create_app(config)

                assert hasattr(app.state, "wake_controller")
                assert app.state.wake_controller is not None

    def test_create_app_wires_wake_planner(self):
        """create_app 创建 WakePlanner。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": "8000",
                "GATEWAY_API_KEY": "test-key",
                "UPSTREAM_BASE_URL": "https://api.example.com",
                "UPSTREAM_API_KEY": "test-upstream-key",
                "UPSTREAM_MODEL": "test-model",
                "SAMPLE_DIRECTORY": os.path.join(tmpdir, "samples"),
                "OUTBOX_DATABASE_PATH": os.path.join(tmpdir, "outbox.sqlite3"),
                "WAKE_JOBS_DATABASE_PATH": os.path.join(tmpdir, "wake_jobs.sqlite3"),
            }
            with patch.dict(os.environ, env, clear=False):
                os.makedirs(os.path.join(tmpdir, "samples"), exist_ok=True)
                for name in ["identity", "persona", "memories", "optional"]:
                    with open(os.path.join(tmpdir, "samples", f"{name}.sample.json"), "w") as f:
                        import json
                        json.dump({"content": "test", "version": 1}, f)

                config = Config.load_from_env()
                app = create_app(config)

                assert hasattr(app.state, "wake_planner")
                assert app.state.wake_planner is not None

    def test_create_app_wires_model_tool_loop(self):
        """create_app 创建 ModelToolLoop 并注入 TurnRunner。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": "8000",
                "GATEWAY_API_KEY": "test-key",
                "UPSTREAM_BASE_URL": "https://api.example.com",
                "UPSTREAM_API_KEY": "test-upstream-key",
                "UPSTREAM_MODEL": "test-model",
                "SAMPLE_DIRECTORY": os.path.join(tmpdir, "samples"),
                "OUTBOX_DATABASE_PATH": os.path.join(tmpdir, "outbox.sqlite3"),
                "WAKE_JOBS_DATABASE_PATH": os.path.join(tmpdir, "wake_jobs.sqlite3"),
            }
            with patch.dict(os.environ, env, clear=False):
                os.makedirs(os.path.join(tmpdir, "samples"), exist_ok=True)
                for name in ["identity", "persona", "memories", "optional"]:
                    with open(os.path.join(tmpdir, "samples", f"{name}.sample.json"), "w") as f:
                        import json
                        json.dump({"content": "test", "version": 1}, f)

                config = Config.load_from_env()
                app = create_app(config)

                assert hasattr(app.state, "model_tool_loop")
                assert app.state.model_tool_loop is not None
                # TurnRunner 持有 model_tool_loop
                assert app.state.turn_runner.model_tool_loop is not None

    def test_create_app_wires_active_turn_gate(self):
        """create_app 创建 ActiveTurnGate。"""
        from app.application.app import create_app
        from app.infrastructure.config import Config

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": "8000",
                "GATEWAY_API_KEY": "test-key",
                "UPSTREAM_BASE_URL": "https://api.example.com",
                "UPSTREAM_API_KEY": "test-upstream-key",
                "UPSTREAM_MODEL": "test-model",
                "SAMPLE_DIRECTORY": os.path.join(tmpdir, "samples"),
                "OUTBOX_DATABASE_PATH": os.path.join(tmpdir, "outbox.sqlite3"),
                "WAKE_JOBS_DATABASE_PATH": os.path.join(tmpdir, "wake_jobs.sqlite3"),
            }
            with patch.dict(os.environ, env, clear=False):
                os.makedirs(os.path.join(tmpdir, "samples"), exist_ok=True)
                for name in ["identity", "persona", "memories", "optional"]:
                    with open(os.path.join(tmpdir, "samples", f"{name}.sample.json"), "w") as f:
                        import json
                        json.dump({"content": "test", "version": 1}, f)

                config = Config.load_from_env()
                app = create_app(config)

                assert hasattr(app.state, "active_turn_gate")
                assert app.state.active_turn_gate is not None
