"""M1: 记忆基础设施测试。

验证：
1. MemoryRecall 数据模型
2. GraphStore 端口 + SQLiteGraphStore（表创建/CRUD/递归 CTE 查询）
3. PersonaStore 端口 + SQLitePersonaStore（表创建/CRUD）
4. BufferStore 端口 + SQLiteBufferStore（表创建/CRUD/@e FIFO/@d 标记/@a 清空）
"""
from __future__ import annotations

import os
import tempfile
import json
from datetime import datetime, timezone

import pytest

from app.domain.models.memory import (
    MemoryRecall,
    MemorySurface,
    IntentResult,
    RecallEntry,
    SurfaceEntry,
)


class TestMemoryModels:
    """记忆数据模型测试。"""

    def test_memory_recall_query_mode(self):
        """MemoryRecall query 模式。"""
        recall = MemoryRecall(
            mode="query",
            text="some memory text",
            source_recall_ids=[1, 2],
            metadata={"intent": "query", "confidence": 0.9},
        )
        assert recall.mode == "query"
        assert recall.text == "some memory text"
        assert recall.source_recall_ids == [1, 2]

    def test_memory_recall_degraded_text_must_be_empty(self):
        """degraded 模式 text 必须为空。"""
        recall = MemoryRecall(
            mode="degraded",
            text="",
            source_recall_ids=[],
            metadata={"timeout": True},
        )
        assert recall.mode == "degraded"
        assert recall.text == ""

    def test_memory_recall_no_query_ids_empty(self):
        """no_query 模式 source_recall_ids 为空。"""
        recall = MemoryRecall(
            mode="no_query",
            text="surface content",
            source_recall_ids=[],
            metadata={"intent": "no_query"},
        )
        assert recall.source_recall_ids == []

    def test_memory_recall_new_window(self):
        """new_window 模式。"""
        recall = MemoryRecall(
            mode="new_window",
            text="concatenated recall text",
            source_recall_ids=[],
            metadata={},
        )
        assert recall.mode == "new_window"

    def test_intent_result_query(self):
        """IntentResult query。"""
        result = IntentResult(
            label="query",
            confidence=0.85,
            matched_patterns=["之前"],
            source="rule",
            intent_type="fact",
        )
        assert result.label == "query"
        assert result.intent_type == "fact"

    def test_intent_result_no_query_type_null(self):
        """no_query 时 intent_type 为 None。"""
        result = IntentResult(
            label="no_query",
            confidence=0.9,
            matched_patterns=[],
            source="rule",
            intent_type=None,
        )
        assert result.intent_type is None

    def test_recall_entry(self):
        """RecallEntry 数据模型。"""
        entry = RecallEntry(
            id=1,
            trigger_id="trig-1",
            content="polished text",
            raw_content="[MEMORY]+[NARRATIVE]",
            metadata={"tracks": ["vector"]},
            created_at="2025-01-01T00:00:00+00:00",
            read_at=None,
        )
        assert entry.id == 1
        assert entry.read_at is None

    def test_surface_entry(self):
        """SurfaceEntry 数据模型。"""
        entry = SurfaceEntry(
            id=1,
            content="polished surface",
            raw_content="raw surface",
            surface_type="association",
            source_recall_ids=[1, 2],
            created_at="2025-01-01T00:00:00+00:00",
        )
        assert entry.surface_type == "association"


class TestSQLiteBufferStore:
    """SQLiteBufferStore 测试。"""

    def test_table_creation(self, tmp_path):
        """表创建成功。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        # 表存在
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "buffer.sqlite3"))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "buffer_raw" in tables
        assert "buffer_recall" in tables
        assert "buffer_surface" in tables

    @pytest.mark.asyncio
    async def test_append_raw_and_clear(self, tmp_path):
        """追加 @a 和清空。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        await store.append_raw("user", "hello", "web", "turn-1")
        await store.append_raw("assistant", "hi", "web", "turn-1")

        # 清空
        await store.clear_raw()
        # 验证已清空
        entries = await store.read_all_raw()
        assert len(entries) == 0

    @pytest.mark.asyncio
    async def test_write_recall_and_read_latest(self, tmp_path):
        """写入 @d 并读取最新。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        rid = await store.write_recall(
            "trig-1", "polished", "raw", {"tracks": ["vector"]}
        )
        assert rid > 0

        entry = await store.read_recall_latest()
        assert entry is not None
        assert entry.content == "polished"
        assert entry.read_at is not None  # 读取后标记

    @pytest.mark.asyncio
    async def test_read_recall_latest_returns_none_when_empty(self, tmp_path):
        """空 @d 读取返回 None。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        entry = await store.read_recall_latest()
        assert entry is None

    @pytest.mark.asyncio
    async def test_read_recent_recall(self, tmp_path):
        """读取最近 N 条 @d（不标记，不删除）。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        for i in range(5):
            await store.write_recall(f"trig-{i}", f"content-{i}", f"raw-{i}", {})

        entries = await store.read_recent_recall(3)
        assert len(entries) == 3
        # 最新的在前
        assert entries[0].content == "content-4"

    @pytest.mark.asyncio
    async def test_scan_recall_for_surface(self, tmp_path):
        """扫描 @d 供 @e 选材（不标记，不删除）。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        await store.write_recall("trig-1", "c1", "r1", {})
        await store.write_recall("trig-2", "c2", "r2", {})

        entries = await store.scan_recall_for_surface()
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_write_surface_and_read_fifo(self, tmp_path):
        """写入 @e 并 FIFO 读取（读取后删除）。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        id1 = await store.write_surface("surf-1", "raw-1", "association", [1])
        id2 = await store.write_surface("surf-2", "raw-2", "impression", [2])

        # FIFO 读取第一条
        entry = await store.read_surface()
        assert entry is not None
        assert entry.content == "surf-1"
        assert entry.surface_type == "association"

        # 第一条已删除
        entry2 = await store.read_surface()
        assert entry2 is not None
        assert entry2.content == "surf-2"

        # 全部读完
        entry3 = await store.read_surface()
        assert entry3 is None

    @pytest.mark.asyncio
    async def test_clear_recall(self, tmp_path):
        """清空 @d。"""
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        store = SQLiteBufferStore(str(tmp_path / "buffer.sqlite3"))
        await store.write_recall("trig-1", "c1", "r1", {})
        await store.clear_recall()
        entries = await store.scan_recall_for_surface()
        assert len(entries) == 0


class TestSQLiteGraphStore:
    """SQLiteGraphStore 测试。"""

    def test_table_creation(self, tmp_path):
        """表创建成功。"""
        from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
        store = SQLiteGraphStore(str(tmp_path / "graph.sqlite3"))
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "graph.sqlite3"))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "events" in tables
        assert "relations" in tables
        assert "episodes" in tables
        assert "sagas" in tables

    @pytest.mark.asyncio
    async def test_write_event_and_query(self, tmp_path):
        """写入事件并查询。"""
        from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
        store = SQLiteGraphStore(str(tmp_path / "graph.sqlite3"))
        await store.write_event({
            "event_id": "evt-1",
            "subject": "用户",
            "object": "Python",
            "predicate": "学习了",
            "action_type": "ACHIEVEMENT",
            "context": "学习 Python",
            "event_time": "2025-01-01T10:00:00+08:00",
            "emotion_label": "happy",
            "impact_score": 0.8,
            "confidence": 0.9,
            "source_msg_id": "msg-1",
            "created_at": "2025-01-01T10:00:00+08:00",
        })

        results = await store.query_events("用户", max_hops=1)
        assert len(results) >= 1
        assert results[0]["event_id"] == "evt-1"

    @pytest.mark.asyncio
    async def test_query_events_multi_hop(self, tmp_path):
        """递归 CTE 多跳查询。"""
        from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
        store = SQLiteGraphStore(str(tmp_path / "graph.sqlite3"))
        # A → B → C
        await store.write_event({
            "event_id": "evt-1", "subject": "A", "object": "B",
            "predicate": "knows", "action_type": "RELATIONSHIP",
            "source_msg_id": "msg-1", "created_at": "2025-01-01T00:00:00+00:00",
        })
        await store.write_event({
            "event_id": "evt-2", "subject": "B", "object": "C",
            "predicate": "knows", "action_type": "RELATIONSHIP",
            "source_msg_id": "msg-2", "created_at": "2025-01-01T00:00:00+00:00",
        })

        # 1 跳只到 B
        results_1 = await store.query_events("A", max_hops=1)
        ids_1 = {r["event_id"] for r in results_1}
        assert "evt-1" in ids_1
        assert "evt-2" not in ids_1

        # 2 跳到 B 和 C
        results_2 = await store.query_events("A", max_hops=2)
        ids_2 = {r["event_id"] for r in results_2}
        assert "evt-1" in ids_2
        assert "evt-2" in ids_2

    @pytest.mark.asyncio
    async def test_write_and_query_episodes(self, tmp_path):
        """写入并查询剧情。"""
        from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
        store = SQLiteGraphStore(str(tmp_path / "graph.sqlite3"))
        await store.write_episode({
            "episode_id": "ep-1",
            "summary": "学习 Python 的故事",
            "start_time": "2025-01-01T00:00:00+00:00",
            "end_time": "2025-01-02T00:00:00+00:00",
            "event_ids": json.dumps(["evt-1"]),
            "big_five_snapshot": "{}",
            "efstb_snapshot": "{}",
            "source_msg_ids": json.dumps(["msg-1"]),
            "created_at": "2025-01-02T00:00:00+00:00",
        })

        results = await store.query_episodes("fact")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_write_and_query_sagas(self, tmp_path):
        """写入并查询主线。"""
        from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
        store = SQLiteGraphStore(str(tmp_path / "graph.sqlite3"))
        await store.write_saga({
            "saga_id": "saga-1",
            "title": "编程之旅",
            "narrative": "润色后的叙事",
            "raw_narrative": "原始叙事",
            "episode_ids": json.dumps(["ep-1"]),
            "status": "active",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
        })

        results = await store.query_sagas("active")
        assert len(results) >= 1
        assert results[0]["title"] == "编程之旅"

    @pytest.mark.asyncio
    async def test_query_plans(self, tmp_path):
        """查询计划类型事件。"""
        from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
        store = SQLiteGraphStore(str(tmp_path / "graph.sqlite3"))
        await store.write_event({
            "event_id": "evt-plan-1", "subject": "用户", "object": "项目",
            "predicate": "计划", "action_type": "PLAN",
            "source_msg_id": "msg-1", "created_at": "2025-01-01T00:00:00+00:00",
        })

        results = await store.query_plans()
        assert len(results) >= 1


class TestSQLitePersonaStore:
    """SQLitePersonaStore 测试。"""

    def test_table_creation(self, tmp_path):
        """表创建成功。"""
        from app.adapters.memory.sqlite_persona_store import SQLitePersonaStore
        store = SQLitePersonaStore(str(tmp_path / "persona.sqlite3"))
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "persona.sqlite3"))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "persona_profiles" in tables
        assert "persona_observations" in tables

    @pytest.mark.asyncio
    async def test_write_and_read_profile(self, tmp_path):
        """写入和读取人格画像。"""
        from app.adapters.memory.sqlite_persona_store import SQLitePersonaStore
        store = SQLitePersonaStore(str(tmp_path / "persona.sqlite3"))
        await store.write_profile(
            actor_id="user-1",
            big_five={"openness": 0.8, "conscientiousness": 0.6},
            efstb={"energy": 0.7, "friendliness": 0.9},
            aliases=["用户", "小明"],
        )

        profile = await store.read_profile("user-1")
        assert profile is not None
        assert profile["actor_id"] == "user-1"
        big_five = json.loads(profile["big_five"])
        assert big_five["openness"] == 0.8

    @pytest.mark.asyncio
    async def test_write_and_read_observation(self, tmp_path):
        """写入和读取人格观察。"""
        from app.adapters.memory.sqlite_persona_store import SQLitePersonaStore
        store = SQLitePersonaStore(str(tmp_path / "persona.sqlite3"))
        await store.write_observation(
            actor_id="user-1",
            observation="用户表现出强烈的求知欲",
            raw_observation="raw observation",
            source_episode_ids=["ep-1"],
        )

        observations = await store.read_observations("user-1")
        assert len(observations) >= 1
        assert observations[0]["observation"] == "用户表现出强烈的求知欲"

    @pytest.mark.asyncio
    async def test_read_profile_returns_none_when_not_exists(self, tmp_path):
        """读取不存在的人格返回 None。"""
        from app.adapters.memory.sqlite_persona_store import SQLitePersonaStore
        store = SQLitePersonaStore(str(tmp_path / "persona.sqlite3"))
        profile = await store.read_profile("nonexistent")
        assert profile is None


class TestChromaVectorStore:
    """ChromaVectorStore 测试（使用 mock client）。"""

    def test_upsert_and_query(self, tmp_path, monkeypatch):
        """upsert 和 query 通过 mock client 验证。"""
        from unittest.mock import MagicMock, patch
        from app.adapters.memory.chroma_vector_store import ChromaVectorStore

        mock_client = MagicMock()
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection
        mock_collection.query.return_value = {
            "ids": [["id-1"]],
            "documents": [["doc-1"]],
            "metadatas": [[{"track": "vector"}]],
            "distances": [[0.1]],
        }
        mock_collection.count.return_value = 1

        store = ChromaVectorStore(str(tmp_path / "chroma"))
        # 注入 mock
        store._client = mock_client
        store._collection = mock_collection

        store.upsert(
            ids=["id-1"],
            embeddings=[[0.1, 0.2, 0.3]],
            documents=["doc-1"],
            metadatas=[{"track": "vector"}],
        )
        mock_collection.upsert.assert_called_once()

        result = store.query([0.1, 0.2, 0.3], n_results=5)
        assert result["ids"] == [["id-1"]]
        assert result["documents"] == [["doc-1"]]

        assert store.count() == 1

