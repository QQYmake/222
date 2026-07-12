"""M5: @4 查询路径完整链路测试——R2-R7 + 超时降级 γ + 润色。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.application.memory.retrieval_pipeline import RetrievalPipeline, RetrievalResult
from app.application.memory.polish_bridge import PolishBridge
from app.application.memory.memory_engine import MemoryEngine
from app.domain.ports.memory_engine import MemoryEngineConfig, MemoryRecall
from app.application.memory.buffer_manager import BufferManager
from app.application.memory.intent_classifier import IntentClassifier, IntentResult


class TestPolishBridge:
    """PolishBridge 润色器。"""

    async def test_polish_returns_polished_text(self):
        """润色返回润色后的文本。"""
        llm_bridge = AsyncMock()
        llm_bridge.generate = AsyncMock(return_value="polished content")
        bridge = PolishBridge(llm_bridge=llm_bridge)
        result = await bridge.polish("raw d content", context="user query")
        assert result == "polished content"
        llm_bridge.generate.assert_called_once()

    async def test_polish_returns_raw_on_error(self):
        """LLM 异常时返回原始文本。"""
        llm_bridge = AsyncMock()
        llm_bridge.generate = AsyncMock(side_effect=Exception("LLM error"))
        bridge = PolishBridge(llm_bridge=llm_bridge)
        result = await bridge.polish("raw content", context="ctx")
        assert result == "raw content"

    async def test_polish_no_llm_returns_raw(self):
        """无 LLM bridge 时返回原始文本。"""
        bridge = PolishBridge(llm_bridge=None)
        result = await bridge.polish("raw content", context="ctx")
        assert result == "raw content"


class TestRetrievalPipeline:
    """RetrievalPipeline 多轨检索。"""

    @pytest.fixture
    def mock_llm_bridge(self):
        bridge = AsyncMock()
        bridge.embed = AsyncMock(return_value=[0.1] * 384)
        bridge.generate = AsyncMock(return_value="raw d content")
        return bridge

    @pytest.fixture
    def mock_buffer_manager(self):
        bm = AsyncMock()
        bm.write_recall = AsyncMock(return_value=1)
        bm.read_recall_latest = AsyncMock(return_value=None)
        return bm

    @pytest.fixture
    def mock_polish_bridge(self):
        bridge = AsyncMock()
        bridge.polish = AsyncMock(return_value="polished content")
        return bridge

    @pytest.fixture
    def pipeline(self, mock_llm_bridge, mock_buffer_manager, mock_polish_bridge):
        return RetrievalPipeline(
            llm_bridge=mock_llm_bridge,
            buffer_manager=mock_buffer_manager,
            polish_bridge=mock_polish_bridge,
            vector_store=None,
            bm25_store=None,
            graph_store=None,
            event_repository=None,
            hybrid_scorer=None,
        )

    async def test_pipeline_executes_and_writes_recall(self, pipeline, mock_llm_bridge, mock_buffer_manager):
        """完整 R2-R7 执行后写入 @d。"""
        intent = IntentResult(label="query", confidence=0.9, source="rule")
        messages = [MagicMock(content="查一下之前的方案")]

        # Mock 各检索轨返回空
        pipeline._vector_store = AsyncMock()
        pipeline._vector_store.search = AsyncMock(return_value=[])
        pipeline._bm25_store = AsyncMock()
        pipeline._bm25_store.search = AsyncMock(return_value=[])
        pipeline._graph_store = AsyncMock()
        pipeline._graph_store.search_events = AsyncMock(return_value=[])
        pipeline._event_repository = AsyncMock()
        pipeline._event_repository.search_structured = AsyncMock(return_value=[])

        await pipeline.execute(intent, messages)

        mock_llm_bridge.embed.assert_called_once()
        mock_llm_bridge.generate.assert_called_once()
        mock_buffer_manager.write_recall.assert_called_once()

    async def test_pipeline_polish_applied(self, pipeline, mock_polish_bridge, mock_buffer_manager):
        """R7 润色被应用。"""
        intent = IntentResult(label="query", confidence=0.9, source="rule")
        messages = [MagicMock(content="查一下")]

        pipeline._vector_store = AsyncMock()
        pipeline._vector_store.search = AsyncMock(return_value=[])
        pipeline._bm25_store = AsyncMock()
        pipeline._bm25_store.search = AsyncMock(return_value=[])
        pipeline._graph_store = AsyncMock()
        pipeline._graph_store.search_events = AsyncMock(return_value=[])
        pipeline._event_repository = AsyncMock()
        pipeline._event_repository.search_structured = AsyncMock(return_value=[])

        await pipeline.execute(intent, messages)

        mock_polish_bridge.polish.assert_called_once()
        # write_recall 的 content 参数应为润色后的内容
        call_args = mock_buffer_manager.write_recall.call_args
        assert call_args.kwargs.get("content") == "polished content" or call_args[1].get("content") == "polished content"


class TestQueryPathWithTimeout:
    """@4 查询路径超时降级 γ。"""

    @pytest.fixture
    def mock_buffer_manager(self):
        bm = AsyncMock()
        bm.read_surface = AsyncMock(return_value=None)
        bm.read_recent_recall = AsyncMock(return_value=[])
        bm.write_recall = AsyncMock(return_value=1)
        bm.read_recall_latest = AsyncMock(return_value=None)
        bm.append_raw = AsyncMock()
        return bm

    @pytest.fixture
    def mock_intent_classifier(self):
        classifier = AsyncMock()
        classifier.classify = AsyncMock(return_value=IntentResult(
            label="query", confidence=0.9, source="rule"
        ))
        return classifier

    @pytest.fixture
    def mock_retrieval_pipeline(self):
        rp = AsyncMock()

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)  # 超过 timeout

        rp.execute = slow_execute
        return rp

    async def test_timeout_degrades_to_empty_recall(
        self, mock_buffer_manager, mock_intent_classifier, mock_retrieval_pipeline, tmp_path
    ):
        """超时后返回 degraded MemoryRecall。"""
        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.db"),
            retrieval_timeout=0.1,
            surface_interval=999999,
        )
        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_buffer_manager,
            intent_classifier=mock_intent_classifier,
            retrieval_pipeline=mock_retrieval_pipeline,
        )

        trigger = MagicMock()
        messages = [MagicMock(content="查一下之前")]

        result = await engine.recall(trigger, messages)
        assert result.mode == "degraded"
        assert result.text == ""

    async def test_non_timeout_returns_query_result(
        self, mock_buffer_manager, mock_intent_classifier, tmp_path
    ):
        """非超时返回 query 模式结果。"""
        from app.domain.models.memory import RecallEntry

        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.db"),
            retrieval_timeout=5.0,
            surface_interval=999999,
        )

        mock_rp = AsyncMock()
        mock_rp.execute = AsyncMock()
        mock_buffer_manager.read_recall_latest = AsyncMock(return_value=RecallEntry(
            id=1, content="polished recall text", raw_content="raw",
            trigger_id="t1", read_at=None, created_at="2025-01-01T00:00:00",
            metadata={},
        ))

        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_buffer_manager,
            intent_classifier=mock_intent_classifier,
            retrieval_pipeline=mock_rp,
        )

        trigger = MagicMock()
        messages = [MagicMock(content="查一下之前")]

        result = await engine.recall(trigger, messages)
        assert result.mode == "query"
        assert result.text == "polished recall text"
        assert result.source_recall_ids == [1]


class TestRecallAsTool:
    """recall_as_tool 工具入口。"""

    @pytest.fixture
    def mock_buffer_manager(self):
        bm = AsyncMock()
        bm.read_recall_latest = AsyncMock(return_value=None)
        bm.write_recall = AsyncMock(return_value=1)
        return bm

    @pytest.fixture
    def mock_retrieval_pipeline(self):
        rp = AsyncMock()
        rp.execute = AsyncMock()
        return rp

    async def test_recall_as_tool_triggers_retrieval(
        self, mock_buffer_manager, mock_retrieval_pipeline, tmp_path
    ):
        """recall_as_tool 触发 @4 检索流程。"""
        from app.domain.models.memory import RecallEntry

        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.db"),
            retrieval_timeout=5.0,
        )

        mock_buffer_manager.read_recall_latest = AsyncMock(return_value=RecallEntry(
            id=2, content="tool recall text", raw_content="raw",
            trigger_id="tool-1", read_at=None, created_at="2025-01-01T00:00:00",
            metadata={},
        ))

        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_buffer_manager,
            intent_classifier=None,
            retrieval_pipeline=mock_retrieval_pipeline,
        )

        result = await engine.recall_as_tool("查一下之前的方案")
        assert result == "tool recall text"
        mock_retrieval_pipeline.execute.assert_called_once()

    async def test_recall_as_tool_returns_empty_on_no_result(
        self, mock_buffer_manager, mock_retrieval_pipeline, tmp_path
    ):
        """recall_as_tool 无结果时返回空字符串。"""
        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.db"),
            retrieval_timeout=5.0,
        )

        mock_buffer_manager.read_recall_latest = AsyncMock(return_value=None)

        engine = MemoryEngine(
            config=config,
            buffer_manager=mock_buffer_manager,
            intent_classifier=None,
            retrieval_pipeline=mock_retrieval_pipeline,
        )

        result = await engine.recall_as_tool("查一下")
        assert result == ""
