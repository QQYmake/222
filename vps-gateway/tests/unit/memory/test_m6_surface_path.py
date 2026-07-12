"""M6: @6 无查询路径 + SurfaceGenerator + RandomSurfaceSelector + @e 周期生成器。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.application.memory.surface_generator import (
    SurfaceGenerator,
    RandomSurfaceSelector,
    SurfaceGenResult,
)
from app.domain.models.memory import RecallEntry, SurfaceEntry


class TestRandomSurfaceSelector:
    """RandomSurfaceSelector 随机选材。"""

    def test_select_returns_empty_on_no_entries(self):
        selector = RandomSurfaceSelector()
        result = selector.select([])
        assert result == []

    def test_select_returns_subset(self):
        selector = RandomSurfaceSelector(max_items=3)
        entries = [
            RecallEntry(id=i, content=f"entry {i}", raw_content=f"raw {i}",
                       trigger_id="t", read_at=None, created_at="2025-01-01T00:00:00", metadata={})
            for i in range(10)
        ]
        result = selector.select(entries)
        assert len(result) <= 3
        assert all(r in entries for r in result)

    def test_select_returns_all_when_fewer_than_max(self):
        selector = RandomSurfaceSelector(max_items=5)
        entries = [
            RecallEntry(id=1, content="e1", raw_content="r1",
                       trigger_id="t", read_at=None, created_at="2025-01-01", metadata={}),
            RecallEntry(id=2, content="e2", raw_content="r2",
                       trigger_id="t", read_at=None, created_at="2025-01-01", metadata={}),
        ]
        result = selector.select(entries)
        assert len(result) == 2


class TestSurfaceGenerator:
    """SurfaceGenerator @e 周期生成。"""

    @pytest.fixture
    def mock_llm_bridge(self):
        bridge = AsyncMock()
        bridge.generate = AsyncMock(return_value="generated surface content")
        return bridge

    @pytest.fixture
    def mock_buffer_manager(self):
        bm = AsyncMock()
        bm.scan_recall_for_surface = AsyncMock(return_value=[])
        bm.write_surface = AsyncMock(return_value=1)
        return bm

    @pytest.fixture
    def mock_polish_bridge(self):
        bridge = AsyncMock()
        bridge.polish = AsyncMock(return_value="polished surface")
        return bridge

    @pytest.fixture
    def generator(self, mock_llm_bridge, mock_buffer_manager, mock_polish_bridge):
        return SurfaceGenerator(
            llm_bridge=mock_llm_bridge,
            buffer_manager=mock_buffer_manager,
            polish_bridge=mock_polish_bridge,
            selector=RandomSurfaceSelector(max_items=5),
        )

    async def test_generate_skips_when_no_entries(self, generator, mock_buffer_manager):
        """无 @d 条目时跳过生成。"""
        mock_buffer_manager.scan_recall_for_surface = AsyncMock(return_value=[])
        result = await generator.generate()
        assert result.skipped is True
        mock_buffer_manager.write_surface.assert_not_called()

    async def test_generate_writes_surface(self, generator, mock_buffer_manager, mock_llm_bridge):
        """有 @d 条目时生成 @e 并写入。"""
        entries = [
            RecallEntry(id=1, content="memory 1", raw_content="raw 1",
                       trigger_id="t", read_at=None, created_at="2025-01-01", metadata={}),
            RecallEntry(id=2, content="memory 2", raw_content="raw 2",
                       trigger_id="t", read_at=None, created_at="2025-01-01", metadata={}),
        ]
        mock_buffer_manager.scan_recall_for_surface = AsyncMock(return_value=entries)

        result = await generator.generate()
        assert result.skipped is False
        assert result.content == "polished surface"
        mock_llm_bridge.generate.assert_called_once()
        mock_buffer_manager.write_surface.assert_called_once()

    async def test_generate_handles_llm_error(self, generator, mock_llm_bridge, mock_buffer_manager):
        """LLM 异常时跳过写入。"""
        mock_llm_bridge.generate = AsyncMock(side_effect=Exception("LLM error"))
        entries = [
            RecallEntry(id=1, content="mem", raw_content="raw",
                       trigger_id="t", read_at=None, created_at="2025-01-01", metadata={}),
        ]
        mock_buffer_manager.scan_recall_for_surface = AsyncMock(return_value=entries)

        result = await generator.generate()
        assert result.skipped is True
        mock_buffer_manager.write_surface.assert_not_called()

    async def test_generate_with_no_llm_skips(self, mock_buffer_manager, mock_polish_bridge):
        """无 LLM bridge 时跳过。"""
        gen = SurfaceGenerator(
            llm_bridge=None,
            buffer_manager=mock_buffer_manager,
            polish_bridge=mock_polish_bridge,
        )
        entries = [
            RecallEntry(id=1, content="mem", raw_content="raw",
                       trigger_id="t", read_at=None, created_at="2025-01-01", metadata={}),
        ]
        mock_buffer_manager.scan_recall_for_surface = AsyncMock(return_value=entries)

        result = await gen.generate()
        assert result.skipped is True


class TestSurfacePathIntegration:
    """@6 无查询路径集成测试。"""

    async def test_surface_path_reads_surface_entry(self):
        """@6 路径读取 @e 内容。"""
        from app.application.memory.memory_engine import MemoryEngine
        from app.domain.ports.memory_engine import MemoryEngineConfig
        from app.application.memory.buffer_manager import BufferManager

        mock_bm = AsyncMock(spec=BufferManager)
        mock_bm.read_surface = AsyncMock(return_value=SurfaceEntry(
            id=1, content="surface text", raw_content="raw",
            surface_type="daily", source_recall_ids=[1],
            created_at="2025-01-01T00:00:00",
        ))
        mock_bm.read_recent_recall = AsyncMock(return_value=[])

        config = MemoryEngineConfig(db_path=":memory:", enabled=True)
        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_bm,
            intent_classifier=None,
        )

        trigger = MagicMock()
        messages = [MagicMock(content="hello")]

        result = await engine.recall(trigger, messages)
        assert result.mode == "no_query"
        assert result.text == "surface text"

    async def test_surface_path_empty_when_no_surface(self):
        """@6 路径无 @e 时返回空 text。"""
        from app.application.memory.memory_engine import MemoryEngine
        from app.domain.ports.memory_engine import MemoryEngineConfig
        from app.application.memory.buffer_manager import BufferManager

        mock_bm = AsyncMock(spec=BufferManager)
        mock_bm.read_surface = AsyncMock(return_value=None)

        config = MemoryEngineConfig(db_path=":memory:", enabled=True)
        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_bm,
            intent_classifier=None,
        )

        trigger = MagicMock()
        messages = [MagicMock(content="hello")]

        result = await engine.recall(trigger, messages)
        assert result.mode == "no_query"
        assert result.text == ""
