"""M3: MemoryPort + MemoryEngine 骨架 + BufferManager 测试。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from app.domain.ports.memory_engine import MemoryPort, MemoryRecall, MemoryEngineConfig
from app.application.memory.buffer_manager import BufferManager
from app.application.memory.memory_engine import MemoryEngine


def _make_user_trigger():
    """创建测试用 UserTrigger。"""
    from app.domain.models.trigger import UserTrigger
    return UserTrigger(request_id="req-1", chat_request={"messages": []})


class TestMemoryPort:
    """MemoryPort 端口定义。"""

    def test_memory_port_is_abstract(self):
        """MemoryPort 是抽象基类，不能直接实例化。"""
        with pytest.raises(TypeError):
            MemoryPort()

    def test_memory_port_has_required_methods(self):
        """MemoryPort 定义了 5 个抽象方法。"""
        assert hasattr(MemoryPort, "recall")
        assert hasattr(MemoryPort, "after_turn")
        assert hasattr(MemoryPort, "start_background_tasks")
        assert hasattr(MemoryPort, "stop_background_tasks")
        assert hasattr(MemoryPort, "recall_as_tool")


class TestMemoryRecall:
    """MemoryRecall 数据合同。"""

    def test_memory_recall_dataclass(self):
        """MemoryRecall 包含 mode, text, source_recall_ids。"""
        recall = MemoryRecall(
            mode="query",
            text="some polished memory text",
            source_recall_ids=[1, 2, 3],
        )
        assert recall.mode == "query"
        assert recall.text == "some polished memory text"
        assert recall.source_recall_ids == [1, 2, 3]

    def test_memory_recall_degraded_mode(self):
        """degraded 模式 text 可为空。"""
        recall = MemoryRecall(
            mode="degraded",
            text="",
            source_recall_ids=[],
        )
        assert recall.mode == "degraded"
        assert recall.text == ""

    def test_memory_recall_no_query_mode(self):
        """no_query 模式 source_recall_ids 为空。"""
        recall = MemoryRecall(
            mode="no_query",
            text="surface text",
            source_recall_ids=[],
        )
        assert recall.mode == "no_query"
        assert recall.source_recall_ids == []


class TestMemoryEngineConfig:
    """MemoryEngineConfig 配置。"""

    def test_config_has_timeout(self):
        """配置包含检索超时。"""
        config = MemoryEngineConfig(
            db_path=":memory:",
            retrieval_timeout=20.0,
            surface_interval=3600.0,
            consolidation_hour=2,
        )
        assert config.retrieval_timeout == 20.0
        assert config.surface_interval == 3600.0
        assert config.consolidation_hour == 2

    def test_config_defaults(self):
        """配置有合理默认值。"""
        config = MemoryEngineConfig(db_path=":memory:")
        assert config.retrieval_timeout > 0
        assert config.surface_interval > 0
        assert 0 <= config.consolidation_hour <= 23


class TestBufferManager:
    """BufferManager 缓冲管理。"""

    @pytest.fixture
    def mock_buffer_store(self):
        store = AsyncMock()
        store.append_raw = AsyncMock()
        store.read_recent_recall = AsyncMock(return_value=[])
        store.write_recall = AsyncMock(return_value=1)
        store.read_recall_latest = AsyncMock(return_value=None)
        store.scan_recall_for_surface = AsyncMock(return_value=[])
        store.write_surface = AsyncMock(return_value=1)
        store.read_surface = AsyncMock(return_value=None)
        store.clear_raw = AsyncMock()
        store.clear_recall = AsyncMock()
        return store

    @pytest.fixture
    def buffer_manager(self, mock_buffer_store):
        return BufferManager(mock_buffer_store)

    async def test_append_raw_delegates_to_store(self, buffer_manager, mock_buffer_store):
        """append_raw 委托给 store。"""
        await buffer_manager.append_raw("user", "hello", "web", "turn-1")
        mock_buffer_store.append_raw.assert_called_once_with(
            role="user", content="hello", platform="web", turn_id="turn-1"
        )

    async def test_read_recent_recall_delegates(self, buffer_manager, mock_buffer_store):
        """read_recent_recall 委托给 store。"""
        await buffer_manager.read_recent_recall(15)
        mock_buffer_store.read_recent_recall.assert_called_once_with(15)

    async def test_write_recall_delegates(self, buffer_manager, mock_buffer_store):
        """write_recall 委托给 store。"""
        result = await buffer_manager.write_recall(
            "trigger-1", "polished", "raw", {"meta": "data"}
        )
        assert result == 1
        mock_buffer_store.write_recall.assert_called_once()

    async def test_read_recall_latest_delegates(self, buffer_manager, mock_buffer_store):
        """read_recall_latest 委托给 store。"""
        await buffer_manager.read_recall_latest()
        mock_buffer_store.read_recall_latest.assert_called_once()

    async def test_scan_recall_for_surface_delegates(self, buffer_manager, mock_buffer_store):
        """scan_recall_for_surface 委托给 store。"""
        await buffer_manager.scan_recall_for_surface()
        mock_buffer_store.scan_recall_for_surface.assert_called_once()

    async def test_write_surface_delegates(self, buffer_manager, mock_buffer_store):
        """write_surface 委托给 store。"""
        result = await buffer_manager.write_surface(
            "content", "raw", "daily", [1, 2]
        )
        assert result == 1
        mock_buffer_store.write_surface.assert_called_once()

    async def test_read_surface_delegates(self, buffer_manager, mock_buffer_store):
        """read_surface 委托给 store。"""
        await buffer_manager.read_surface()
        mock_buffer_store.read_surface.assert_called_once()

    async def test_clear_raw_delegates(self, buffer_manager, mock_buffer_store):
        """clear_raw 委托给 store。"""
        await buffer_manager.clear_raw()
        mock_buffer_store.clear_raw.assert_called_once()

    async def test_clear_recall_delegates(self, buffer_manager, mock_buffer_store):
        """clear_recall 委托给 store。"""
        await buffer_manager.clear_recall()
        mock_buffer_store.clear_recall.assert_called_once()


class TestMemoryEngineSkeleton:
    """MemoryEngine 骨架。"""

    @pytest.fixture
    def mock_buffer_manager(self):
        bm = AsyncMock(spec=BufferManager)
        bm.read_surface = AsyncMock(return_value=None)
        bm.read_recent_recall = AsyncMock(return_value=[])
        bm.write_recall = AsyncMock(return_value=1)
        bm.read_recall_latest = AsyncMock(return_value=None)
        bm.append_raw = AsyncMock()
        bm.clear_raw = AsyncMock()
        bm.clear_recall = AsyncMock()
        return bm

    @pytest.fixture
    def mock_intent_classifier(self):
        classifier = AsyncMock()
        classifier.classify = AsyncMock(return_value=MagicMock(
            label="no_query", confidence=1.0, source="rule", matched_patterns=[]
        ))
        return classifier

    @pytest.fixture
    def engine(self, mock_buffer_manager, mock_intent_classifier, tmp_path):
        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.db"),
            retrieval_timeout=1.0,
            surface_interval=999999,
            consolidation_hour=2,
        )
        return MemoryEngine(
            config=config,
            buffer_manager=mock_buffer_manager,
            intent_classifier=mock_intent_classifier,
            llm_bridge=None,
            retrieval_pipeline=None,
            surface_generator=None,
            consolidation_pipeline=None,
        )

    async def test_engine_implements_memory_port(self, engine):
        """MemoryEngine 实现 MemoryPort。"""
        assert isinstance(engine, MemoryPort)

    async def test_engine_recall_no_query_path(self, engine, mock_buffer_manager):
        """no_query 路径返回空 text 或 @e 内容。"""
        trigger = _make_user_trigger()
        messages = [MagicMock(content="hello there")]

        mock_buffer_manager.read_surface = AsyncMock(return_value=None)

        result = await engine.recall(trigger, messages)
        assert result.mode == "no_query"
        assert result.text == ""
        assert result.source_recall_ids == []

    async def test_engine_recall_no_query_with_surface(self, engine, mock_buffer_manager):
        """no_query 路径有 @e 时返回 @e 内容。"""
        from app.domain.models.memory import SurfaceEntry

        trigger = _make_user_trigger()
        messages = [MagicMock(content="hello")]

        surface = SurfaceEntry(id=1, content="surface text", raw_content="raw", surface_type="daily", source_recall_ids=[1], created_at="2025-01-01T00:00:00")
        mock_buffer_manager.read_surface = AsyncMock(return_value=surface)

        result = await engine.recall(trigger, messages)
        assert result.mode == "no_query"
        assert result.text == "surface text"

    async def test_engine_after_turn_appends_raw(self, engine, mock_buffer_manager):
        """after_turn 追加 @a 原料。"""
        trigger = _make_user_trigger()
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="response text"))]

        await engine.after_turn(
            raw_messages=[MagicMock(content="user msg", role="user")],
            response=response,
            turn_id="turn-1",
            trigger=trigger,
        )
        mock_buffer_manager.append_raw.assert_called()

    async def test_engine_start_stop_background_tasks(self, engine):
        """start/stop background tasks 不抛异常。"""
        await engine.start_background_tasks()
        await engine.stop_background_tasks()

    async def test_engine_recall_as_tool_not_implemented_raises(self, engine):
        """recall_as_tool 在骨架阶段抛 NotImplementedError 或返回降级。"""
        with pytest.raises((NotImplementedError, Exception)):
            await engine.recall_as_tool("query text")

    async def test_engine_memory_disabled_returns_empty(self, mock_buffer_manager, mock_intent_classifier, tmp_path):
        """MEMORY_ENABLED=false 时 recall 返回空 MemoryRecall。"""
        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.db"),
            retrieval_timeout=1.0,
            surface_interval=999999,
            consolidation_hour=2,
            enabled=False,
        )
        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_buffer_manager,
            intent_classifier=mock_intent_classifier,
            llm_bridge=None,
            retrieval_pipeline=None,
            surface_generator=None,
            consolidation_pipeline=None,
        )
        trigger = _make_user_trigger()
        messages = [MagicMock(content="hello")]

        result = await engine.recall(trigger, messages)
        assert result.mode == "degraded"
        assert result.text == ""
        mock_buffer_manager.read_surface.assert_not_called()
