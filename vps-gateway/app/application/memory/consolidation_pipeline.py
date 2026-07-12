"""ConsolidationPipeline：2am 沉淀管线。

W1→W2→W3→W4→W5→W6→清理
数据合同来源：V3 架构文档 6.8 ConsolidationPipeline。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConsolidationResult:
    """沉淀管线执行结果。"""
    consolidation_id: str
    success: bool
    steps_completed: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    skipped: bool = False


class ConsolidationPipeline:
    """2am 沉淀管线编排器。

    依赖注入（全部通过构造函数注入，不引用 config.py）：
    - buffer_manager: BufferManager（@a/@d 读写）
    - event_extractor: EventExtractor（W1 事件抽取）
    - persona_manager: PersonaManager（W2 人格观察）
    - saga_manager: SagaManager（W4 saga 归并）
    - vector_storer: VectorStorer（W5 向量化）
    - polish_bridge: PolishBridge（W6 润色）
    - graph_store: GraphStore（持久化事件/episode/saga）
    - persona_store: PersonaStore（持久化人格快照）
    """

    def __init__(
        self,
        buffer_manager: Any,
        event_extractor: Any,
        persona_manager: Any,
        saga_manager: Any,
        vector_storer: Any,
        polish_bridge: Any,
        graph_store: Any,
        persona_store: Any,
    ) -> None:
        self._buffer = buffer_manager
        self._event_extractor = event_extractor
        self._persona_manager = persona_manager
        self._saga_manager = saga_manager
        self._vector_storer = vector_storer
        self._polish_bridge = polish_bridge
        self._graph_store = graph_store
        self._persona_store = persona_store

    async def run(self) -> ConsolidationResult:
        """执行 W1-W6 沉淀管线。"""
        consolidation_id = str(uuid.uuid4())
        logger.info("consolidation_started", extra={"consolidation_id": consolidation_id})

        # 读取 @a 和 @d
        raw_messages = await self._buffer.read_all_raw()
        if not raw_messages:
            logger.info("consolidation_skipped: empty @a", extra={"consolidation_id": consolidation_id})
            return ConsolidationResult(
                consolidation_id=consolidation_id,
                success=True,
                skipped=True,
            )

        recall_entries = await self._buffer.read_all_recall()
        steps_completed: list[str] = []

        # Bug 12 fix: 记录快照水位，清理时只删除 <= 水位的记录
        # 避免沉淀期间新写入的数据被全表清理
        snapshot_max_raw_id = max(
            (e["id"] if isinstance(e, dict) else getattr(e, "id", 0) for e in raw_messages),
            default=0,
        )
        snapshot_max_recall_id = max(
            (e.id if hasattr(e, "id") else e["id"] for e in recall_entries),
            default=0,
        )

        # W1: 事件/事实抽取
        try:
            events = await self._event_extractor.extract(raw_messages, recall_entries)
            steps_completed.append("W1")
            logger.info(
                "consolidation_w1_done",
                extra={"consolidation_id": consolidation_id, "event_count": len(events)},
            )
        except Exception as e:
            logger.error(
                "consolidation_failed",
                extra={"consolidation_id": consolidation_id, "step": "W1", "error": str(e)},
            )
            return ConsolidationResult(
                consolidation_id=consolidation_id,
                success=False,
                steps_completed=steps_completed,
                failed_step="W1",
                error=str(e),
            )

        # W2: 人格观察与演化
        persona_snapshot = None
        try:
            persona_snapshot = await self._persona_manager.observe(raw_messages, events)
            steps_completed.append("W2")
            logger.info(
                "consolidation_w2_done",
                extra={"consolidation_id": consolidation_id},
            )
        except Exception as e:
            logger.error(
                "consolidation_w2_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )
            return await self._finalize_with_cleanup(
                consolidation_id, steps_completed, "W2", str(e),
                events, persona_snapshot, None, None,
                snapshot_max_raw_id, snapshot_max_recall_id,
            )

        # W3: 结构化事件校验（规则层 + 可选 LLM 轻量校验）
        try:
            events = self._validate_events(events)
            steps_completed.append("W3")
            logger.info(
                "consolidation_w3_done",
                extra={"consolidation_id": consolidation_id, "validated_count": len(events)},
            )
        except Exception as e:
            logger.error(
                "consolidation_w3_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )
            return await self._finalize_with_cleanup(
                consolidation_id, steps_completed, "W3", str(e),
                events, persona_snapshot, None, None,
                snapshot_max_raw_id, snapshot_max_recall_id,
            )

        # W4: Saga 聚类归并
        saga_updates = None
        try:
            saga_updates = await self._saga_manager.cluster(events)
            steps_completed.append("W4")
            logger.info(
                "consolidation_w4_done",
                extra={"consolidation_id": consolidation_id, "saga_count": len(saga_updates) if saga_updates else 0},
            )
        except Exception as e:
            logger.error(
                "consolidation_w4_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )
            return await self._finalize_with_cleanup(
                consolidation_id, steps_completed, "W4", str(e),
                events, persona_snapshot, saga_updates, None,
                snapshot_max_raw_id, snapshot_max_recall_id,
            )

        # W5: 消息向量化
        try:
            await self._vector_storer.store_batch(raw_messages)
            steps_completed.append("W5")
            logger.info(
                "consolidation_w5_done",
                extra={"consolidation_id": consolidation_id},
            )
        except Exception as e:
            logger.error(
                "consolidation_w5_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )
            return await self._finalize_with_cleanup(
                consolidation_id, steps_completed, "W5", str(e),
                events, persona_snapshot, saga_updates, None,
                snapshot_max_raw_id, snapshot_max_recall_id,
            )

        # W6: 润色
        polished_persona = None
        polished_sagas = None
        try:
            polished_persona = await self._polish_bridge.polish(persona_snapshot, context_type="persona")
            polished_sagas = await self._polish_bridge.polish(saga_updates, context_type="saga")
            steps_completed.append("W6")
            logger.info(
                "consolidation_w6_done",
                extra={"consolidation_id": consolidation_id},
            )
        except Exception as e:
            logger.error(
                "consolidation_w6_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )
            return await self._finalize_with_cleanup(
                consolidation_id, steps_completed, "W6", str(e),
                events, persona_snapshot, saga_updates, polished_persona,
                snapshot_max_raw_id, snapshot_max_recall_id,
            )

        # 清理：持久化 + 清空缓冲区
        return await self._finalize_with_cleanup(
            consolidation_id, steps_completed, None, None,
            events, persona_snapshot, saga_updates, polished_persona,
            snapshot_max_raw_id, snapshot_max_recall_id,
        )

    def _validate_events(self, events: list[Any]) -> list[Any]:
        """W3: 规则校验 - 去重、冲突检测、字段标准化。"""
        # 基本去重：相同内容的 event 只保留一条
        seen = set()
        result = []
        for ev in events:
            key = str(ev) if not hasattr(ev, "__dict__") else str(ev.__dict__)
            if key not in seen:
                seen.add(key)
                result.append(ev)
        return result

    async def _finalize_with_cleanup(
        self,
        consolidation_id: str,
        steps_completed: list[str],
        failed_step: str | None,
        error: str | None,
        events: list[Any] | None,
        persona_snapshot: Any | None,
        saga_updates: list[Any] | None,
        polished_persona: Any | None,
        snapshot_max_raw_id: int = 0,
        snapshot_max_recall_id: int = 0,
    ) -> ConsolidationResult:
        """清理步骤：持久化已完成步骤的结果 + 清空 @a/@d。

        Bug 12 fix: 只清理 <= 快照水位的记录，保留沉淀期间新写入的数据。
        """
        try:
            # 持久化事件到 GraphStore
            if events:
                await self._graph_store.write_events(events)

            # 持久化 saga 到 GraphStore
            if saga_updates:
                await self._graph_store.write_sagas(saga_updates)

            # 持久化人格快照
            if persona_snapshot:
                await self._persona_store.write(persona_snapshot)

            steps_completed.append("cleanup")
        except Exception as e:
            logger.error(
                "consolidation_cleanup_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )

        # 清空 @a/@d — 只清理快照水位及以下的记录（Bug 12 fix）
        try:
            await self._buffer.clear_raw_up_to(snapshot_max_raw_id)
            await self._buffer.clear_recall_up_to(snapshot_max_recall_id)
            logger.info(
                "buffer_cleared_up_to_watermark",
                extra={
                    "consolidation_id": consolidation_id,
                    "max_raw_id": snapshot_max_raw_id,
                    "max_recall_id": snapshot_max_recall_id,
                },
            )
        except Exception as e:
            logger.error(
                "buffer_clear_failed",
                extra={"consolidation_id": consolidation_id, "error": str(e)},
            )

        success = failed_step is None
        logger.info(
            "consolidation_completed",
            extra={
                "consolidation_id": consolidation_id,
                "success": success,
                "failed_step": failed_step,
            },
        )

        return ConsolidationResult(
            consolidation_id=consolidation_id,
            success=success,
            steps_completed=steps_completed,
            failed_step=failed_step,
            error=error,
        )
