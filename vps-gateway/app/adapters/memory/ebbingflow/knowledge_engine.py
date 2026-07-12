"""KnowledgeEngine 适配模块——Neo4j driver → SQLiteGraphStore。

适配自 ebbingflow memory/knowledge_engine.py：
  - Neo4j driver → 注入的 GraphStore（SQLite 递归 CTE）
  - ChromaDB → 注入的 ChromaVectorStore
  - Cypher 查询 → GraphStore.query_events/episodes/sagas/plans
  - infer_query_intent() 直接复用（零改动）
  - 配置通过构造注入，不引用 vps-gateway config.py
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.adapters.memory.ebbingflow.hybrid_scorer import (
    HybridScorer,
    ScoredCandidate,
    UnifiedMemoryResult,
)

logger = logging.getLogger(__name__)

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    BM25Okapi = None


class KnowledgeEngine:
    """混合记忆检索引擎（SQLite 适配版）。

    适配自 ebbingflow KnowledgeBaseEngine：
    - Neo4j driver → GraphStore (注入)
    - ChromaDB client → VectorStore (注入)
    - infer_query_intent() 零改动直接复用
    """

    def __init__(
        self,
        graph_store=None,
        vector_store=None,
        top_k: int = 5,
        window_cutoff: int = 6,
    ):
        self._graph_store = graph_store
        self._vector_store = vector_store
        self.top_k = top_k
        self.window_cutoff = window_cutoff
        self.scorer = HybridScorer()
        self.last_latency: Dict[str, Any] = {
            "vector": 0, "graph": 0, "bm25": 0, "total": 0,
        }

    async def close(self):
        """清理资源（SQLite 短连接无需显式关闭）。"""
        pass

    @staticmethod
    def infer_query_intent(query: str) -> str:
        """Classify retrieval intent so evidence and narrative memories do not compete blindly.

        直接复用自 ebbingflow——零改动。
        """
        q = str(query or "").strip().lower()
        if not q:
            return "semantic"

        long_term_markers = [
            "长期", "主线", "整体", "全局", "一直", "以来", "长期目标", "长期关系",
            "项目进展", "阶段", "脉络", "演变", "over time", "long-term", "roadmap",
        ]
        summary_markers = [
            "最近", "这几轮", "这段时间", "总结", "概括", "回顾", "我们聊了什么",
            "发生了什么", "进展如何", "summary", "recap", "what happened",
        ]
        fact_markers = [
            "多少", "哪里", "什么时候", "哪天", "谁", "是什么", "有没有", "是否",
            "上次说", "原文", "证据", "预算", "价格", "金额",
            "电话", "地址", "日期", "时间", "关系", "名字", "生日", "how much",
            "when", "where", "who", "what is", "evidence",
        ]
        numeric_fact_pattern = (
            r"\d+\s*(元|块|万|亿|%|岁|年|月|日|号|点|分钟|小时|天|个)"
            r"|\d+\s*(kg|g|cm|m|usd|rmb|cny|dollars?)\b"
        )

        has_fact_signal = bool(
            any(marker in q for marker in fact_markers)
            or re.search(numeric_fact_pattern, q)
        )
        if has_fact_signal:
            return "fact"
        if any(marker in q for marker in long_term_markers):
            return "long_term"
        if any(marker in q for marker in summary_markers):
            return "summary"
        return "semantic"

    async def query(
        self,
        query: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        query_embedding: Optional[List[float]] = None,
    ) -> List[UnifiedMemoryResult]:
        """全量检索：向量 + 图谱 + BM25 多路召回 + HybridScorer 重排。"""
        start_total = time.perf_counter()
        query_intent = self.infer_query_intent(query)
        self.last_latency["query_intent"] = query_intent

        # 1. 向量召回
        start_v = time.perf_counter()
        vector_candidates = await self._retrieve_vector(query, query_embedding)
        self.last_latency["vector"] = int((time.perf_counter() - start_v) * 1000)

        # 2. 图谱召回
        start_g = time.perf_counter()
        graph_candidates = await self._retrieve_graph(query)
        self.last_latency["graph"] = int((time.perf_counter() - start_g) * 1000)

        # 3. BM25 关键词召回
        start_b = time.perf_counter()
        bm25_candidates = await self._retrieve_bm25(query)
        self.last_latency["bm25"] = int((time.perf_counter() - start_b) * 1000)

        # 4. Episode/Saga 召回
        episode_candidates = await self._retrieve_episodes(query)
        saga_candidates = await self._retrieve_sagas()

        # 5. 汇总去重
        raw_candidates = (
            vector_candidates
            + graph_candidates
            + bm25_candidates
            + episode_candidates
            + saga_candidates
        )
        all_candidates = []
        seen_keys = set()
        for c in raw_candidates:
            key = c.content.strip()
            if key and key not in seen_keys:
                seen_keys.add(key)
                all_candidates.append(c)

        # 6. HybridScorer 重排
        scored = self.scorer.score_and_rank(all_candidates)
        results = scored[: self.top_k]

        self.last_latency["total"] = int((time.perf_counter() - start_total) * 1000)
        return results

    async def _retrieve_vector(
        self, query: str, query_embedding: Optional[List[float]]
    ) -> List[ScoredCandidate]:
        if self._vector_store is None or query_embedding is None:
            return []
        try:
            result = self._vector_store.query(query_embedding, n_results=self.top_k)
            candidates = []
            ids = result.get("ids", [[]])[0]
            documents = result.get("documents", [[]])[0]
            metadatas = result.get("metadatas", [[]])[0]
            distances = result.get("distances", [[]])[0]
            for i, doc in enumerate(documents):
                meta = metadatas[i] if i < len(metadatas) else {}
                dist = distances[i] if i < len(distances) else 1.0
                candidates.append(
                    ScoredCandidate(
                        content=doc,
                        speaker=meta.get("speaker", "unknown"),
                        timestamp=meta.get("created_at", ""),
                        source_type="vector_chat",
                        source_name=meta.get("source", ""),
                        semantic_score=max(0.0, 1.0 - dist),
                    )
                )
            return candidates
        except Exception as exc:
            logger.warning("[KnowledgeEngine] vector retrieval failed: %s", exc)
            return []

    async def _retrieve_graph(self, query: str) -> List[ScoredCandidate]:
        if self._graph_store is None:
            return []
        try:
            # 提取查询中的实体（简单分词）
            entities = self._extract_entities(query)
            candidates = []
            for entity in entities:
                events = await self._graph_store.query_events(entity, max_hops=2)
                for evt in events:
                    candidates.append(
                        ScoredCandidate(
                            content=evt.get("context", "") or f"{evt.get('subject', '')} {evt.get('predicate', '')} {evt.get('object', '')}",
                            speaker=evt.get("subject", "unknown"),
                            timestamp=evt.get("event_time", "") or evt.get("created_at", ""),
                            source_type="graph",
                            source_name=evt.get("event_id", ""),
                            graph_hop_score=1.0,
                            impact_score=float(evt.get("impact_score", 5.0)),
                        )
                    )
            return candidates
        except Exception as exc:
            logger.warning("[KnowledgeEngine] graph retrieval failed: %s", exc)
            return []

    async def _retrieve_bm25(self, query: str) -> List[ScoredCandidate]:
        if BM25Okapi is None or self._graph_store is None:
            return []
        try:
            # 使用 graph store 中的事件文本构建 BM25 索引
            # 简化实现：查询所有事件并做 BM25 检索
            events = await self._graph_store.query_events(query, max_hops=1)
            if not events:
                return []
            corpus = [
                (evt.get("context", "") or f"{evt.get('subject', '')} {evt.get('predicate', '')}").split()
                for evt in events
            ]
            if not any(corpus):
                return []
            bm25 = BM25Okapi(corpus)
            scores = bm25.get_scores(query.split())
            candidates = []
            for i, score in enumerate(scores):
                if score > 0:
                    evt = events[i]
                    candidates.append(
                        ScoredCandidate(
                            content=evt.get("context", "") or f"{evt.get('subject', '')} {evt.get('predicate', '')}",
                            speaker=evt.get("subject", "unknown"),
                            timestamp=evt.get("event_time", ""),
                            source_type="bm25",
                            source_name=evt.get("event_id", ""),
                        )
                    )
            return candidates
        except Exception as exc:
            logger.warning("[KnowledgeEngine] BM25 retrieval failed: %s", exc)
            return []

    async def _retrieve_episodes(self, query: str) -> List[ScoredCandidate]:
        if self._graph_store is None:
            return []
        try:
            episodes = await self._graph_store.query_episodes("all")
            candidates = []
            for ep in episodes:
                candidates.append(
                    ScoredCandidate(
                        content=ep.get("summary", ""),
                        speaker="system",
                        timestamp=ep.get("created_at", ""),
                        source_type="episode",
                        source_name=ep.get("episode_id", ""),
                    )
                )
            return candidates
        except Exception as exc:
            logger.warning("[KnowledgeEngine] episode retrieval failed: %s", exc)
            return []

    async def _retrieve_sagas(self) -> List[ScoredCandidate]:
        if self._graph_store is None:
            return []
        try:
            sagas = await self._graph_store.query_sagas("active")
            candidates = []
            for saga in sagas:
                candidates.append(
                    ScoredCandidate(
                        content=saga.get("narrative", ""),
                        speaker="system",
                        timestamp=saga.get("updated_at", ""),
                        source_type="saga",
                        source_name=saga.get("saga_id", ""),
                    )
                )
            return candidates
        except Exception as exc:
            logger.warning("[KnowledgeEngine] saga retrieval failed: %s", exc)
            return []

    @staticmethod
    def _extract_entities(query: str) -> list[str]:
        """从查询中提取候选实体（简单实现）。"""
        # 移除常见停用词和标点
        words = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z]+", query)
        # 过滤掉太短或太长的词
        return [w for w in words if 2 <= len(w) <= 10]
