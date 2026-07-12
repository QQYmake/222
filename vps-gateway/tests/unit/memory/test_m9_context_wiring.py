"""M9: ContextBuilder 适配 + TurnRunner 适配 + AppFactory v3 接线 + 种子数据导入。

测试覆盖：
1. ContextBuilder 接受 memory_recall_text 替换 <memories>
2. MEMORY_ENABLED=false 时使用 sample memories（向后兼容）
3. 种子数据导入
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.context_builder import ContextBuilder, render_state_xml
from app.domain.models.memories import MemoryItem, MemoriesData
from app.domain.models.sample import SampleEnvelope
from app.domain.ports.sample_reader import AllSamples
from app.domain.models.trigger import UserTrigger


def _make_samples(memory_items=None):
    """构造测试用 AllSamples"""
    if memory_items is None:
        memory_items = [
            MemoryItem(id="m1", category="fact", priority=5, content="旧记忆内容", created_at="2025-01-01"),
        ]
    return AllSamples(
        identity=SampleEnvelope(
            sample_type="identity", version=1, updated_at="2025-01-01T00:00:00",
            source="sample", data=MagicMock(
                name="沉", self_description="test", values=[], boundaries=[],
                relationship_definition="test",
            ),
        ),
        preferences=SampleEnvelope(
            sample_type="preferences", version=1, updated_at="2025-01-01T00:00:00",
            source="sample", data=MagicMock(
                communication_preferences=[], stable_likes=[], stable_dislikes=[],
                interaction_rules=[],
            ),
        ),
        memories=SampleEnvelope(
            sample_type="memories", version=1, updated_at="2025-01-01T00:00:00",
            source="sample", data=MemoriesData(items=memory_items),
        ),
        working_state=SampleEnvelope(
            sample_type="working_state", version=1, updated_at="2025-01-01T00:00:00",
            source="sample", data=MagicMock(
                current_focus=[], emotion_summary="happy", pending_items=[], next_wake_at="",
            ),
        ),
    )


class TestContextBuilderMemoryRecall:
    """ContextBuilder 接受 memory_recall_text 替换 <memories>"""

    def test_memory_recall_text_replaces_memories_block(self):
        """当 memory_recall_text 提供时，<memories> 来源由记忆引擎接管"""
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="req-1",
            chat_request={"messages": [{"role": "user", "content": "hello"}]},
        )

        builder = ContextBuilder()

        # With memory_recall_text: replaces <memories>
        prepared = builder.build(samples, trigger, memory_recall_text="这是来自记忆引擎的动态记忆")
        system_content = prepared.messages[0].content
        assert "这是来自记忆引擎的动态记忆" in system_content
        assert "旧记忆内容" not in system_content

    def test_no_memory_recall_uses_sample_memories(self):
        """无 memory_recall_text 时使用 sample memories（向后兼容）"""
        samples = _make_samples()
        trigger = UserTrigger(
            request_id="req-2",
            chat_request={"messages": [{"role": "user", "content": "hello"}]},
        )

        builder = ContextBuilder()

        # Without memory_recall_text: uses sample memories
        prepared = builder.build(samples, trigger)
        system_content = prepared.messages[0].content
        assert "旧记忆内容" in system_content


class TestSeedDataImport:
    """memories.sample.json 降级为种子数据"""

    @pytest.mark.asyncio
    async def test_seed_import_writes_to_buffer(self, tmp_path):
        """首次启动时导入种子数据到 @a 缓冲区"""
        from app.application.memory.buffer_manager import BufferManager
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore

        db_path = str(tmp_path / "test_seed.db")
        store = SQLiteBufferStore(db_path)
        manager = BufferManager(store)

        # Simulate seed data
        seed_items = [
            {"role": "user", "content": "你好沉"},
            {"role": "assistant", "content": "你好，我是沉"},
        ]

        for item in seed_items:
            await manager.append_raw(
                role=item["role"],
                content=item["content"],
                platform="seed",
                turn_id="seed-0",
            )

        raw = await manager.read_all_raw()
        assert len(raw) == 2
        assert raw[0]["content"] == "你好沉"
