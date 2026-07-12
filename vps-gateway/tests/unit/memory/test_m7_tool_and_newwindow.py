"""M7: memory_recall 工具 + 主动回合工具暴露 + 新窗口衔接。

测试覆盖：
1. memory_recall 工具定义和执行
2. ToolRegistry 主动回合工具暴露（register_for_wake_only）
3. 新窗口路径（new_window → 读取最近 15 条 @d → 拼接）
4. @e 生成器周期运行
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.memory import MemoryRecall
from app.domain.models.tool import ToolDefinition, ToolCall, ToolResult, ToolExecutionContext
from app.adapters.tools.memory_recall_tool import (
    MEMORY_RECALL_DEF,
    MemoryRecallExecutor,
)
from app.adapters.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# memory_recall 工具定义
# ---------------------------------------------------------------------------
class TestMemoryRecallToolDefinition:
    def test_tool_definition_fields(self):
        assert MEMORY_RECALL_DEF.name == "memory_recall"
        assert "memory" in MEMORY_RECALL_DEF.description.lower() or "记忆" in MEMORY_RECALL_DEF.description
        assert MEMORY_RECALL_DEF.timeout_seconds <= 15
        assert "query" in str(MEMORY_RECALL_DEF.parameters)
        assert MEMORY_RECALL_DEF.enabled_in_production is True


# ---------------------------------------------------------------------------
# memory_recall 工具执行
# ---------------------------------------------------------------------------
class TestMemoryRecallExecutor:
    @pytest.mark.asyncio
    async def test_execute_returns_polished_text(self):
        """工具执行 → 触发 @4 查询路径 → 返回润色文本"""
        mock_memory_port = AsyncMock()
        mock_memory_port.recall_as_tool = AsyncMock(return_value=MemoryRecall(
            mode="query",
            text="你之前提到过喜欢猫。",
            source_recall_ids=[],
            metadata={},
        ))

        executor = MemoryRecallExecutor(mock_memory_port)
        ctx = ToolExecutionContext(turn_id="t1", trigger_type="wake", trigger_id="w1")
        result = await executor.execute({"query": "猫"}, ctx)

        assert isinstance(result, str)
        assert "猫" in result
        mock_memory_port.recall_as_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_degraded_returns_empty(self):
        """降级时返回空字符串"""
        mock_memory_port = AsyncMock()
        mock_memory_port.recall_as_tool = AsyncMock(return_value="")

        executor = MemoryRecallExecutor(mock_memory_port)
        ctx = ToolExecutionContext(turn_id="t1", trigger_type="wake", trigger_id="w1")
        result = await executor.execute({"query": "test"}, ctx)

        assert result == ""


# ---------------------------------------------------------------------------
# ToolRegistry 主动回合工具暴露
# ---------------------------------------------------------------------------
class TestWakeOnlyToolRegistration:
    def test_register_for_wake_only(self):
        """register_for_wake_only 注册的工具仅在主动回合暴露"""
        registry = ToolRegistry()
        registry.register(MEMORY_RECALL_DEF, MemoryRecallExecutor(AsyncMock()))
        registry.register_for_wake_only("memory_recall")

        # 用户回合不暴露
        user_schemas = registry.schemas_for_user()
        assert all(s["function"]["name"] != "memory_recall" for s in user_schemas)

        # 主动回合暴露
        wake_schemas = registry.schemas_for_wake()
        wake_names = [s["function"]["name"] for s in wake_schemas]
        assert "memory_recall" in wake_names

    def test_user_and_wake_tools_combined_in_wake(self):
        """主动回合同时暴露用户工具和 wake-only 工具"""
        registry = ToolRegistry()

        # 注册一个普通工具
        from app.adapters.tools.wake_tool_definitions import SCHEDULE_WAKEUP_DEF
        registry.register(SCHEDULE_WAKEUP_DEF, MagicMock())

        # 注册一个 wake-only 工具
        registry.register(MEMORY_RECALL_DEF, MemoryRecallExecutor(AsyncMock()))
        registry.register_for_wake_only("memory_recall")

        wake_names = [s["function"]["name"] for s in registry.schemas_for_wake()]
        assert "schedule_wakeup" in wake_names
        assert "memory_recall" in wake_names


# ---------------------------------------------------------------------------
# 新窗口衔接
# ---------------------------------------------------------------------------
class TestNewWindowPath:
    @pytest.mark.asyncio
    async def test_new_window_reads_recent_15_recall(self):
        """X-Memory-Mode: new_window → 读取最近 15 条 @d → 拼接 → MemoryRecall"""
        from app.application.memory.memory_engine import MemoryEngine
        from app.domain.ports.memory_engine import MemoryEngineConfig

        # Mock BufferManager
        mock_buffer = MagicMock()
        recall_entries = []
        for i in range(15):
            entry = MagicMock()
            entry.content = f"记忆条目{i}"
            entry.id = f"r{i}"
            recall_entries.append(entry)
        mock_buffer.read_recent_recall = AsyncMock(return_value=recall_entries)
        mock_buffer.read_surface = AsyncMock(return_value=None)

        engine = MemoryEngine(
            config=MemoryEngineConfig(enabled=True),
            buffer_manager=mock_buffer,
        )

        result = await engine.recall_new_window()

        assert result.mode == "new_window"
        assert "记忆条目0" in result.text
        assert "记忆条目14" in result.text
        mock_buffer.read_recent_recall.assert_called_once_with(15)

    @pytest.mark.asyncio
    async def test_new_window_empty_recall(self):
        """@d 为空时返回空文本"""
        from app.application.memory.memory_engine import MemoryEngine
        from app.domain.ports.memory_engine import MemoryEngineConfig

        mock_buffer = MagicMock()
        mock_buffer.read_recent_recall = AsyncMock(return_value=[])

        engine = MemoryEngine(
            config=MemoryEngineConfig(enabled=True),
            buffer_manager=mock_buffer,
        )

        result = await engine.recall_new_window()
        assert result.mode == "new_window"
        assert result.text == ""
