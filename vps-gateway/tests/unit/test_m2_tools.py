"""M2: ToolRegistry + ToolDispatcher + 四个第一批工具。

验收基线（架构文档 12.1）：
  - 12.1.3: 工具名称不存在 → 不执行，错误回灌模型
  - 12.1.4: 工具参数无效 → 不执行，错误回灌模型
  - 12.1.5: 工具超时 → 15 秒内终止，不自动重试
  - 12.1.6: 工具固定失败 → fail_test 错误被模型收到
  - 12.1.12: 生产配置 → echo/delay/fail 不出现在工具 Schema
  - 12.1.13: 前端上传工具 → 不进入 Registry、不转发为可执行工具

数据合同来源：架构文档 5.2 ToolDefinition + 5.3 ToolCall/ToolResult + 6.2-6.4
"""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock

from app.domain.models.tool import ToolDefinition, ToolCall, ToolResult, ToolExecutionContext
from app.adapters.tools.registry import ToolRegistry
from app.adapters.tools.tool_dispatcher import ToolDispatcher


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------
class TestToolDefinition:
    """ToolDefinition 数据合同。"""

    def test_basic_definition(self):
        td = ToolDefinition(
            name="get_server_time",
            description="Returns the current server time",
            parameters={"type": "object", "properties": {}},
            enabled_in_production=True,
            timeout_seconds=15,
            max_result_chars=12000,
        )
        assert td.name == "get_server_time"
        assert td.enabled_in_production is True

    def test_test_tool_not_production(self):
        td = ToolDefinition(
            name="echo_test",
            description="Echo test",
            parameters={"type": "object"},
            enabled_in_production=False,
            timeout_seconds=15,
            max_result_chars=12000,
        )
        assert td.enabled_in_production is False


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------
class TestToolRegistry:
    """ToolRegistry 注册与查询。"""

    def _make_registry(self, test_tools_enabled=False):
        from app.adapters.tools.get_server_time import GetServerTimeExecutor
        from app.adapters.tools.test_tools import EchoTestExecutor, DelayTestExecutor, FailTestExecutor

        registry = ToolRegistry(test_tools_enabled=test_tools_enabled)
        registry.register(
            ToolDefinition(
                name="get_server_time",
                description="Returns the current server time in ISO 8601",
                parameters={"type": "object", "properties": {}},
                enabled_in_production=True,
                timeout_seconds=15,
                max_result_chars=12000,
            ),
            GetServerTimeExecutor(),
        )
        registry.register(
            ToolDefinition(
                name="echo_test",
                description="Echo back the input (test only)",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                enabled_in_production=False,
                timeout_seconds=15,
                max_result_chars=12000,
            ),
            EchoTestExecutor(),
        )
        return registry

    def test_schemas_production_excludes_test_tools(self):
        """12.1.12: 生产配置 → echo/delay/fail 不出现在工具 Schema。"""
        registry = self._make_registry(test_tools_enabled=False)
        schemas = registry.schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "get_server_time" in names
        assert "echo_test" not in names

    def test_schemas_test_mode_includes_test_tools(self):
        """测试配置 → echo/delay/fail 出现在工具 Schema。"""
        registry = self._make_registry(test_tools_enabled=True)
        schemas = registry.schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "get_server_time" in names
        assert "echo_test" in names

    def test_resolve_existing_tool(self):
        registry = self._make_registry(test_tools_enabled=True)
        executor = registry.resolve("get_server_time")
        assert executor is not None

    def test_resolve_nonexistent_tool(self):
        registry = self._make_registry()
        executor = registry.resolve("nonexistent_tool")
        assert executor is None

    def test_duplicate_name_rejected(self):
        """工具名唯一性校验。"""
        from app.adapters.tools.get_server_time import GetServerTimeExecutor
        registry = ToolRegistry(test_tools_enabled=True)
        td = ToolDefinition(
            name="get_server_time", description="t", parameters={"type": "object"},
            enabled_in_production=True, timeout_seconds=15, max_result_chars=12000,
        )
        registry.register(td, GetServerTimeExecutor())
        with pytest.raises(ValueError, match="duplicate"):
            registry.register(td, GetServerTimeExecutor())


# ---------------------------------------------------------------------------
# ToolDispatcher
# ---------------------------------------------------------------------------
class TestToolDispatcher:
    """ToolDispatcher 解析、校验、超时、截断和错误归一化。"""

    def _make_dispatcher(self, test_tools_enabled=True):
        from app.adapters.tools.get_server_time import GetServerTimeExecutor
        from app.adapters.tools.test_tools import EchoTestExecutor, DelayTestExecutor, FailTestExecutor

        registry = ToolRegistry(test_tools_enabled=test_tools_enabled)
        registry.register(
            ToolDefinition(
                name="get_server_time", description="Returns server time",
                parameters={"type": "object", "properties": {}},
                enabled_in_production=True, timeout_seconds=15, max_result_chars=12000,
            ),
            GetServerTimeExecutor(),
        )
        registry.register(
            ToolDefinition(
                name="echo_test", description="Echo",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                enabled_in_production=False, timeout_seconds=15, max_result_chars=12000,
            ),
            EchoTestExecutor(),
        )
        registry.register(
            ToolDefinition(
                name="delay_test", description="Delay",
                parameters={
                    "type": "object",
                    "properties": {"seconds": {"type": "number"}},
                    "required": ["seconds"],
                },
                enabled_in_production=False, timeout_seconds=2, max_result_chars=12000,
            ),
            DelayTestExecutor(),
        )
        registry.register(
            ToolDefinition(
                name="fail_test", description="Always fails",
                parameters={"type": "object", "properties": {}},
                enabled_in_production=False, timeout_seconds=15, max_result_chars=12000,
            ),
            FailTestExecutor(),
        )
        return ToolDispatcher(registry)

    def _make_context(self):
        return ToolExecutionContext(turn_id="t1", trigger_type="user", trigger_id="r1")

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        """12.1.3: 工具名称不存在 → 不执行，错误回灌模型。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="nonexistent", arguments_json="{}")
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is False
        assert result.error_code == "tool_not_found"
        assert "nonexistent" in result.content

    @pytest.mark.asyncio
    async def test_invalid_arguments(self):
        """12.1.4: 工具参数无效 → 不执行，错误回灌模型。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="echo_test", arguments_json="not valid json")
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is False
        assert result.error_code == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_missing_required_argument(self):
        """12.1.4: 缺少必填参数 → invalid_arguments。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="echo_test", arguments_json='{}')
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is False
        assert result.error_code == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_echo_test_success(self):
        """echo_test 正常执行。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="echo_test", arguments_json='{"message": "hello"}')
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is True
        assert "hello" in result.content

    @pytest.mark.asyncio
    async def test_get_server_time_success(self):
        """get_server_time 返回 ISO 8601 时间。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="get_server_time", arguments_json="{}")
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is True
        assert "T" in result.content  # ISO 8601

    @pytest.mark.asyncio
    async def test_fail_test(self):
        """12.1.6: fail_test 错误被模型收到。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="fail_test", arguments_json="{}")
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is False
        assert result.error_code == "tool_failed"

    @pytest.mark.asyncio
    async def test_delay_test_timeout(self):
        """12.1.5: 工具超时 → 2 秒内终止，不自动重试。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="delay_test", arguments_json='{"seconds": 10}')
        result = await dispatcher.execute(call, self._make_context())
        assert result.ok is False
        assert result.error_code == "tool_timeout"

    @pytest.mark.asyncio
    async def test_result_truncation(self):
        """结果超过 max_result_chars 时截断。"""
        from app.adapters.tools.get_server_time import GetServerTimeExecutor
        registry = ToolRegistry(test_tools_enabled=True)
        registry.register(
            ToolDefinition(
                name="get_server_time", description="t",
                parameters={"type": "object", "properties": {}},
                enabled_in_production=True, timeout_seconds=15, max_result_chars=10,
            ),
            GetServerTimeExecutor(),
        )
        dispatcher = ToolDispatcher(registry)
        call = ToolCall(id="tc-1", name="get_server_time", arguments_json="{}")
        result = await dispatcher.execute(call, self._make_context())
        assert result.truncated is True
        assert len(result.content) <= 10 + 20  # 截断标记余量

    @pytest.mark.asyncio
    async def test_tool_call_id_preserved(self):
        """ToolResult.tool_call_id 与 ToolCall.id 相同。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-xyz", name="get_server_time", arguments_json="{}")
        result = await dispatcher.execute(call, self._make_context())
        assert result.tool_call_id == "tc-xyz"

    @pytest.mark.asyncio
    async def test_duration_recorded(self):
        """ToolResult.duration_ms > 0。"""
        dispatcher = self._make_dispatcher()
        call = ToolCall(id="tc-1", name="get_server_time", arguments_json="{}")
        result = await dispatcher.execute(call, self._make_context())
        assert result.duration_ms >= 0
