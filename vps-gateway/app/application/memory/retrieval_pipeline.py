"""RetrievalPipeline：多轨检索管线。

执行 R2-R7 完整检索链路：
R2: 嵌入查询向量
R3: 并行多轨检索（向量/BM25/图谱/结构化）
R4: HybridScorer 重排
R5: LLM 生成原始 @d 内容
R7: PolishBridge 润色

数据合同来源：V3 架构文档 6.2 _execute_retrieval_pipeline。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.memory.intent_classifier import IntentResult
    from app.domain.models.turn import ChatMessage

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """检索结果。"""

    polished_content: str = ""
    raw_content: str = ""
    candidate_count: int = 0
    recall_id: int = 0


class RetrievalPipeline:
    """多轨检索管线。

    依赖注入：
    - llm_bridge: LLMBridge（嵌入 + 生成）
    - buffer_manager: BufferManager（写入 @d）
    - polish_bridge: PolishBridge（润色）
    - vector_store: 向量存储（ChromaDB）
    - bm25_store: BM25 关键词检索
    - graph_store: 图谱存储（SQLite 递归 CTE）
    - event_repository: 结构化事件检索
    - hybrid_scorer: HybridScorer 重排
    """

    def __init__(
        self,
        llm_bridge: Any | None = None,
        buffer_manager: Any | None = None,
        polish_bridge: Any | None = None,
        vector_store: Any | None = None,
        bm25_store: Any | None = None,
        graph_store: Any | None = None,
        event_repository: Any | None = None,
        hybrid_scorer: Any | None = None,
    ) -> None:
        self._llm_bridge = llm_bridge
        self._buffer = buffer_manager
        self._polish = polish_bridge
        self._vector_store = vector_store
        self._bm25_store = bm25_store
        self._graph_store = graph_store
        self._event_repository = event_repository
        self._hybrid_scorer = hybrid_scorer

    async def execute(
        self, intent_result: "IntentResult", raw_messages: list["ChatMessage"]
    ) -> RetrievalResult:
        """执行完整 R2-R7 链路。"""
        # 兼容 dict 和 ChatMessage 对象
        if raw_messages:
            last_msg = raw_messages[-1]
            if isinstance(last_msg, dict):
                query_text = last_msg.get("content", "") or ""
            else:
                query_text = getattr(last_msg, "content", "") or ""
        else:
            query_text = ""

        # R2: 嵌入查询向量
        query_vector = await self._embed(query_text)

        # R3: 并行多轨检索
        candidates = await self._parallel_retrieve(query_text, query_vector, intent_result)

        # R4: HybridScorer 重排
        ranked = await self._rerank(candidates, intent_result)

        # R5: LLM 生成原始 @d 内容
        raw_d_content = await self._generate(ranked, raw_messages)

        # R7: PolishBridge 润色
        polished = await self._polish_content(raw_d_content, query_text)

        # 写入 @d 缓存
        recall_id = await self._buffer.write_recall(
            trigger_id="query",
            content=polished,
            raw_content=raw_d_content,
            metadata={"candidate_count": len(ranked)},
        )

        logger.info(
            "memory_recall_completed: candidates=%d recall_id=%d",
            len(ranked), recall_id,
        )

        return RetrievalResult(
            polished_content=polished,
            raw_content=raw_d_content,
            candidate_count=len(ranked),
            recall_id=recall_id,
        )

    async def _embed(self, text: str) -> list[float]:
        """R2: 嵌入查询向量。"""
        if self._llm_bridge is None:
            return []
        try:
            return await self._llm_bridge.embed(text)
        except Exception as e:
            logger.warning("embed_error: %s", e)
            return []

    async def _parallel_retrieve(
        self,
        query_text: str,
        query_vector: list[float],
        intent_result: "IntentResult",
    ) -> list[dict]:
        """R3: 并行多轨检索。"""
        tasks = []

        if self._vector_store is not None:
            tasks.append(self._safe_retrieve(
                lambda: self._vector_store.search(query_vector, top_k=10),
                "vector"
            ))
        else:
            tasks.append(self._empty_result("vector"))

        if self._bm25_store is not None:
            tasks.append(self._safe_retrieve(
                lambda: self._bm25_store.search(query_text, top_k=10),
                "bm25"
            ))
        else:
            tasks.append(self._empty_result("bm25"))

        if self._graph_store is not None:
            tasks.append(self._safe_retrieve(
                lambda: self._graph_store.search_events(query_text, max_hops=2),
                "graph"
            ))
        else:
            tasks.append(self._empty_result("graph"))

        if self._event_repository is not None:
            tasks.append(self._safe_retrieve(
                lambda: self._event_repository.search_structured(intent_result.label),
                "sql"
            ))
        else:
            tasks.append(self._empty_result("sql"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_candidates = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("retrieval_track_error: %s", result)
                continue
            if isinstance(result, list):
                all_candidates.extend(result)

        return all_candidates

    async def _safe_retrieve(self, func: Any, track_name: str) -> list[dict]:
        """安全执行单轨检索。"""
        try:
            result = func()
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            logger.warning("retrieval_%s_error: %s", track_name, e)
            return []

    async def _empty_result(self, track_name: str) -> list[dict]:
        """空结果占位。"""
        return []

    async def _rerank(
        self, candidates: list[dict], intent_result: "IntentResult"
    ) -> list[dict]:
        """R4: HybridScorer 重排。"""
        if not candidates:
            return []

        if self._hybrid_scorer is None:
            # 无 scorer 时按原始顺序返回前 10
            return candidates[:10]

        try:
            result = self._hybrid_scorer.score(candidates, intent_result.label)
            if asyncio.iscoroutine(result):
                result = await result
            return result if isinstance(result, list) else candidates[:10]
        except Exception as e:
            logger.warning("rerank_error: %s", e)
            return candidates[:10]

    async def _generate(
        self, ranked_candidates: list[dict], raw_messages: list["ChatMessage"]
    ) -> str:
        """R5: LLM 生成原始 @d 内容。"""
        if self._llm_bridge is None:
            return ""

        if raw_messages:
            last_msg = raw_messages[-1]
            if isinstance(last_msg, dict):
                query_text = last_msg.get("content", "") or ""
            else:
                query_text = getattr(last_msg, "content", "") or ""
        else:
            query_text = ""
        candidate_text = "\n".join(
            str(c.get("content", c)) for c in ranked_candidates[:10]
        )

        try:
            return await self._llm_bridge.generate(
                prompt=f"根据以下检索到的记忆片段，为用户查询生成简洁的记忆摘要：\n\n查询：{query_text}\n\n记忆片段：\n{candidate_text}",
                context=query_text,
            )
        except Exception as e:
            logger.warning("generate_error: %s", e)
            return ""

    async def _polish_content(self, raw_content: str, context: str) -> str:
        """R7: PolishBridge 润色。"""
        if self._polish is None:
            return raw_content

        result = self._polish.polish(raw_content, context=context)
        if asyncio.iscoroutine(result):
            result = await result
        return result
