"""M8: ConsolidationPipeline (2am 沉淀管线 W1-W6 + 清理)。

测试覆盖：
1. W1-W6 完整执行流程
2. W1 失败时中断管线，不清空 @a/@d
3. W2-W6 部分失败时已完成步骤持久化，清空 @a/@d
4. 清理步骤：写入 GraphStore/PersonaStore + 清空 BufferStore
5. consolidation_id 可关联追踪
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.memory.consolidation_pipeline import ConsolidationPipeline


class TestConsolidationPipelineFullRun:
    """W1-W6 完整执行"""

    @pytest.mark.asyncio
    async def test_full_pipeline_executes_all_steps(self):
        """W1→W2→W3→W4→W5→W6→清理 全部执行"""
        mock_buffer = MagicMock()
        mock_buffer.read_all_raw = AsyncMock(return_value=[
            MagicMock(role="user", content="hello", platform="web"),
        ])
        mock_buffer.read_all_recall = AsyncMock(return_value=[
            MagicMock(content="recall text"),
        ])
        mock_buffer.clear_raw_up_to = AsyncMock()
        mock_buffer.clear_recall_up_to = AsyncMock()

        mock_event_extractor = MagicMock()
        mock_event_extractor.extract = AsyncMock(return_value=[MagicMock()])

        mock_persona_manager = MagicMock()
        mock_persona_manager.observe = AsyncMock(return_value={"name": "沉"})

        mock_saga_manager = MagicMock()
        mock_saga_manager.cluster = AsyncMock(return_value=[MagicMock()])

        mock_vector_storer = MagicMock()
        mock_vector_storer.store_batch = AsyncMock(return_value=3)

        mock_polish_bridge = MagicMock()
        mock_polish_bridge.polish = AsyncMock(side_effect=["polished_persona", "polished_sagas"])

        mock_graph_store = MagicMock()
        mock_graph_store.write_events = AsyncMock()
        mock_graph_store.write_episodes = AsyncMock()
        mock_graph_store.write_sagas = AsyncMock()

        mock_persona_store = MagicMock()
        mock_persona_store.write = AsyncMock()

        pipeline = ConsolidationPipeline(
            buffer_manager=mock_buffer,
            event_extractor=mock_event_extractor,
            persona_manager=mock_persona_manager,
            saga_manager=mock_saga_manager,
            vector_storer=mock_vector_storer,
            polish_bridge=mock_polish_bridge,
            graph_store=mock_graph_store,
            persona_store=mock_persona_store,
        )

        result = await pipeline.run()

        assert result.success is True
        assert result.consolidation_id  # non-empty
        assert result.steps_completed == ["W1", "W2", "W3", "W4", "W5", "W6", "cleanup"]

        # Verify cleanup
        mock_graph_store.write_events.assert_awaited_once()
        mock_graph_store.write_sagas.assert_awaited_once()
        mock_persona_store.write.assert_awaited_once()
        mock_buffer.clear_raw_up_to.assert_awaited_once()
        mock_buffer.clear_recall_up_to.assert_awaited_once()


class TestConsolidationPipelineW1Failure:
    """W1 失败时中断管线"""

    @pytest.mark.asyncio
    async def test_w1_failure_aborts_without_clearing(self):
        """W1 失败 → 中断管线 → 不清空 @a/@d"""
        mock_buffer = MagicMock()
        mock_buffer.read_all_raw = AsyncMock(return_value=[MagicMock()])
        mock_buffer.read_all_recall = AsyncMock(return_value=[])
        mock_buffer.clear_raw_up_to = AsyncMock()
        mock_buffer.clear_recall_up_to = AsyncMock()

        mock_event_extractor = MagicMock()
        mock_event_extractor.extract = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        pipeline = ConsolidationPipeline(
            buffer_manager=mock_buffer,
            event_extractor=mock_event_extractor,
            persona_manager=MagicMock(),
            saga_manager=MagicMock(),
            vector_storer=MagicMock(),
            polish_bridge=MagicMock(),
            graph_store=MagicMock(),
            persona_store=MagicMock(),
        )

        result = await pipeline.run()

        assert result.success is False
        assert result.failed_step == "W1"
        assert result.error  # contains error message
        # Buffer NOT cleared
        mock_buffer.clear_raw_up_to.assert_not_awaited()
        mock_buffer.clear_recall_up_to.assert_not_awaited()


class TestConsolidationPipelinePartialFailure:
    """W2-W6 部分失败时已完成步骤持久化，清空 @a/@d"""

    @pytest.mark.asyncio
    async def test_w3_failure_still_clears_buffers(self):
        """W3 校验失败 → W4-W6 跳过 → 清空 @a/@d"""
        mock_buffer = MagicMock()
        mock_buffer.read_all_raw = AsyncMock(return_value=[MagicMock()])
        mock_buffer.read_all_recall = AsyncMock(return_value=[])
        mock_buffer.clear_raw_up_to = AsyncMock()
        mock_buffer.clear_recall_up_to = AsyncMock()

        mock_event_extractor = MagicMock()
        mock_event_extractor.extract = AsyncMock(return_value=[MagicMock()])

        mock_persona_manager = MagicMock()
        mock_persona_manager.observe = AsyncMock(return_value={"name": "沉"})

        # W3: validation fails
        mock_saga_manager = MagicMock()
        mock_saga_manager.cluster = AsyncMock(side_effect=RuntimeError("cluster failed"))

        pipeline = ConsolidationPipeline(
            buffer_manager=mock_buffer,
            event_extractor=mock_event_extractor,
            persona_manager=mock_persona_manager,
            saga_manager=mock_saga_manager,
            vector_storer=MagicMock(),
            polish_bridge=MagicMock(),
            graph_store=MagicMock(),
            persona_store=MagicMock(),
        )

        result = await pipeline.run()

        assert result.success is False
        assert "W1" in result.steps_completed
        assert "W2" in result.steps_completed
        # W4 (saga) failed
        assert result.failed_step == "W4"
        # Buffers still cleared
        mock_buffer.clear_raw_up_to.assert_awaited_once()
        mock_buffer.clear_recall_up_to.assert_awaited_once()


class TestConsolidationPipelineEmptyBuffers:
    """@a 为空时跳过管线"""

    @pytest.mark.asyncio
    async def test_empty_raw_skips_pipeline(self):
        """@a 为空 → 跳过全部步骤 → 不清空"""
        mock_buffer = MagicMock()
        mock_buffer.read_all_raw = AsyncMock(return_value=[])

        pipeline = ConsolidationPipeline(
            buffer_manager=mock_buffer,
            event_extractor=MagicMock(),
            persona_manager=MagicMock(),
            saga_manager=MagicMock(),
            vector_storer=MagicMock(),
            polish_bridge=MagicMock(),
            graph_store=MagicMock(),
            persona_store=MagicMock(),
        )

        result = await pipeline.run()

        assert result.success is True
        assert result.skipped is True
        assert result.steps_completed == []
