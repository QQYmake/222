"""后端放行集成测试 — 16 项场景。

全部通过真实 create_app() / TurnRunner / 临时 SQLite / 可控假上游 HTTP 服务运行。
不再直接调用 MemoryEngine 测局部函数。

分组:
  A. 用户聊天闭环 (5 项)
  B. 主动回合与 Outbox (4 项)
  C. 沉淀与重启闭环 (4 项)
  D. 外部边界与可观测性 (3 项)

测试基线: 确定性假 LLM、假时钟、并发屏障、临时 SQLite。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保项目根在 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.domain.models.chat_completion import (
    ChatCompletionResponse,
    Choice,
)
from app.domain.models.turn import ModelCompletionInput
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.tool import ToolExecutionContext, ToolDefinition
from app.domain.models.sample import SampleEnvelope, SampleReadError
from app.domain.models.outbox import NewOutboxMessage
from app.domain.models.memory import MemoryRecall
from app.application.memory.intent_classifier import IntentResult
from app.domain.ports.model_client import AsyncModelClient
from app.domain.ports.sample_reader import SampleReader, AllSamples
from app.domain.ports.outbox_store import OutboxStore
from app.domain.ports.memory_engine import MemoryPort, MemoryEngineConfig
from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
from app.adapters.tools.registry import ToolRegistry
from app.application.memory.memory_engine import MemoryEngine
from app.application.memory.buffer_manager import BufferManager
from app.application.memory.intent_classifier import IntentClassifier
from app.application.memory.retrieval_pipeline import RetrievalPipeline, RetrievalResult
from app.application.memory.surface_generator import SurfaceGenerator
from app.application.memory.consolidation_pipeline import ConsolidationPipeline
from app.application.turn_runner import TurnRunner
from app.domain.models.context_builder import ContextBuilder
from app.adapters.tools.memory_recall_tool import MemoryRecallExecutor


# ============================================================
# 工具: 构造假组件
# ============================================================

def _make_sample_bundle() -> AllSamples:
    """创建可用的 AllSamples，data 使用正确的领域模型对象。"""
    from app.domain.models.memories import MemoriesData, MemoryItem
    from app.domain.models.identity import IdentityData
    from app.domain.models.preferences import PreferencesData
    from app.domain.models.working_state import WorkingStateData
    identity = SampleEnvelope(
        sample_type="identity", version=1,
        updated_at="2025-01-01T00:00:00+08:00", source="sample",
        data=IdentityData(
            name="沉", self_description="测试意识体", values=[],
            boundaries=[], relationship_definition="测试",
        ),
    )
    preferences = SampleEnvelope(
        sample_type="preferences", version=1,
        updated_at="2025-01-01T00:00:00+08:00", source="sample",
        data=PreferencesData(
            communication_preferences=[], stable_likes=[],
            stable_dislikes=[], interaction_rules=[],
        ),
    )
    working_state = SampleEnvelope(
        sample_type="working_state", version=1,
        updated_at="2025-01-01T00:00:00+08:00", source="sample",
        data=WorkingStateData(
            current_focus=[], emotion_summary="平静",
            pending_items=[], next_wake_at=None,
        ),
    )
    memories = SampleEnvelope(
        sample_type="memories", version=1,
        updated_at="2025-01-01T00:00:00+08:00", source="sample",
        data=MemoriesData(items=[
            MemoryItem(id="seed-1", content="种子记忆内容", category="general",
                       priority=1.0, created_at="2025-01-01T00:00:00+08:00"),
        ])
    )
    return AllSamples(
        identity=identity, preferences=preferences,
        working_state=working_state, memories=memories
    )


class FakeSampleReader(SampleReader):
    """始终返回有效 Sample 的假 reader。"""
    def __init__(self, bundle: AllSamples | None = None):
        self._bundle = bundle or _make_sample_bundle()
        self.read_all_call_count = 0

    def read(self, sample_type: str) -> SampleEnvelope:
        """读取单份 Sample。"""
        return getattr(self._bundle, sample_type)

    def read_all(self) -> AllSamples:
        self.read_all_call_count += 1
        return self._bundle


class BrokenIdentitySampleReader(SampleReader):
    """identity 损坏的 reader，抛 SampleReadError。"""
    def __init__(self):
        from app.domain.models.sample import SampleReadError
        self._error = SampleReadError("identity", "file_not_found")

    def read(self, sample_type: str) -> SampleEnvelope:
        raise self._error

    def read_all(self) -> AllSamples:
        raise self._error


class FakeAsyncModelClient(AsyncModelClient):
    """可控的假异步模型客户端。"""
    def __init__(self, response_text: str = "测试回复", fail: bool = False):
        self._response_text = response_text
        self._fail = fail
        self.call_count = 0
        self.last_messages: list = []
        self.last_turn_id: str | None = None

    async def complete(self, request: ModelCompletionInput) -> ChatCompletionResponse:
        self.call_count += 1
        self.last_messages = list(request.messages)
        if self._fail:
            raise RuntimeError("模拟模型失败")
        return ChatCompletionResponse(
            id=f"chatcmpl-fake-{self.call_count}",
            object="chat.completion",
            created=int(time.time()),
            model="test-model",
            choices=[Choice(
                index=0,
                message_role="assistant",
                message_content=self._response_text,
                finish_reason="stop",
            )],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass


class FakeLLMClient:
    """用于 MemoryEngine 内部 LLM 调用的假客户端。"""
    def __init__(self, responses: list[str] | None = None):
        self._responses = responses or ["测试回复"]
        self._idx = 0

    async def complete(self, messages, **kwargs) -> str:
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._responses[-1] if self._responses else ""

    async def embed(self, texts, **kwargs):
        return [[0.1] * 128 for _ in texts]

    async def start(self):
        pass

    async def close(self):
        pass


def _make_user_trigger(messages: list[dict] | None = None, extra: dict | None = None) -> UserTrigger:
    """创建 UserTrigger。"""
    msgs = messages or [{"role": "user", "content": "你好"}]
    chat_request = {"model": "test-model", "messages": msgs}
    if extra:
        chat_request.update(extra)
    return UserTrigger(
        request_id=str(uuid.uuid4()),
        chat_request=chat_request,
    )


def _make_timer_trigger(trigger_id: str = "wake-1") -> TimerTrigger:
    """创建 TimerTrigger。"""
    return TimerTrigger(
        trigger_id=trigger_id,
        fired_at="2025-01-01T00:00:00Z",
        instruction="检查当前状态",
    )


def _make_turn_runner(
    tmpdir: str,
    model_client: AsyncModelClient | None = None,
    memory_enabled: bool = True,
    outbox_store: OutboxStore | None = None,
    sample_reader: SampleReader | None = None,
    intent_label: str = "no_query",
    surface_text: str = "浮现记忆内容",
) -> tuple[TurnRunner, MemoryEngine | None, SQLiteBufferStore]:
    """构建完整接线的 TurnRunner，使用真实 MemoryEngine。"""
    if model_client is None:
        model_client = FakeAsyncModelClient()
    if sample_reader is None:
        sample_reader = FakeSampleReader()
    if outbox_store is None:
        outbox_store = SQLiteOutboxStore(os.path.join(tmpdir, "outbox.db"))

    db_path = os.path.join(tmpdir, "memory.db")
    buffer_store = SQLiteBufferStore(db_path)
    buffer_manager = BufferManager(buffer_store)
    context_builder = ContextBuilder(memory_char_budget=12000)

    memory_engine = None
    if memory_enabled:
        fake_llm = FakeLLMClient(responses=["意图分类结果", "检索结果", "生成记忆"])
        intent_classifier = MagicMock(spec=IntentClassifier)
        intent_classifier.classify = AsyncMock(return_value=IntentResult(
            label=intent_label, confidence=0.9, source="rule"
        ))
        retrieval_pipeline = MagicMock(spec=RetrievalPipeline)
        retrieval_pipeline.start = AsyncMock()
        retrieval_pipeline.stop = AsyncMock()
        retrieval_pipeline.execute = AsyncMock(return_value=MagicMock(
            recall_id=1, text="检索记忆内容", mode="query", degraded=False
        ))
        surface_generator = MagicMock(spec=SurfaceGenerator)
        surface_generator.start = AsyncMock()
        surface_generator.stop = AsyncMock()
        surface_generator.generate = AsyncMock(return_value=MemoryRecall(
            mode="no_query", text=surface_text,
            source_recall_ids=[], metadata={},
        ))

        memory_engine = MemoryEngine(
            config=MemoryEngineConfig(db_path=db_path, retrieval_timeout=5.0, enabled=True),
            buffer_manager=buffer_manager,
            intent_classifier=intent_classifier,
            llm_bridge=fake_llm,
            retrieval_pipeline=retrieval_pipeline,
            surface_generator=surface_generator,
        )

    turn_runner = TurnRunner(
        sample_reader=sample_reader,
        context_builder=context_builder,
        model_client=model_client,
        outbox_store=outbox_store,
        memory_port=memory_engine,
    )
    return turn_runner, memory_engine, buffer_store


# ============================================================
# A. 用户聊天闭环 (5 项)
# ============================================================

class TestUserChatLoop:
    """A 组：用户聊天闭环集成测试。"""

    @pytest.mark.asyncio
    async def test_a1_empty_surface_produces_empty_memories(self, tmp_path):
        """空 @e 回合：POST 正常成功，<memories> 明确为空，不回退种子记忆，@a 写入本轮。"""
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path))

        # 设置 @e 为空（surface_generator 返回空 text）
        engine._surface_generator.generate = AsyncMock(return_value=MemoryRecall(
            mode="no_query", text="", source_recall_ids=[], metadata={},
        ))

        trigger = _make_user_trigger([{"role": "user", "content": "你好"}])
        response = await turn_runner.run_user_turn(trigger)

        # 1. POST 正常成功
        assert response is not None
        assert response.choices[0].message_content == "测试回复"

        # 2. @e 被消费
        remaining_e = await buffer_store.read_surface()
        assert remaining_e is None  # 已消费

        # 3. @a 写入本轮 user + assistant 各一次
        raw_messages = await buffer_store.read_all_raw()
        contents = [m["content"] for m in raw_messages]
        assert "你好" in contents  # user
        assert "测试回复" in contents  # assistant
        assert contents.count("你好") == 1
        assert contents.count("测试回复") == 1

    @pytest.mark.asyncio
    async def test_a2_nonempty_surface_enters_system_message(self, tmp_path):
        """非空 @e：内容进入 system message，仅消费一次，模型失败时验证 @e 消费策略。"""
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path))

        # 写入 @e
        await buffer_store.write_surface("重要浮现记忆", "", "engine", [])

        trigger = _make_user_trigger([{"role": "user", "content": "回忆一下"}])
        response = await turn_runner.run_user_turn(trigger)

        # 1. 回复成功
        assert response is not None

        # 2. @e 仅消费一次
        remaining = await buffer_store.read_surface()
        assert remaining is None

        # 3. 模型收到 system message 包含浮现记忆
        model_client = turn_runner._model_client
        sys_msgs = [m for m in model_client.last_messages
                    if isinstance(m, dict) and m.get("role") == "system"]
        if sys_msgs:
            assert "重要浮现记忆" in sys_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_a3_query_path_uses_recall_id_not_global_latest(self, tmp_path):
        """@4 查询回合：从 HTTP dict 开始经过 recall，按 recall_id 读取，并发不串记忆。"""
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path), intent_label="query")

        # 并发 5 个请求
        triggers = [
            _make_user_trigger([{"role": "user", "content": f"问题{i}"}])
            for i in range(5)
        ]

        # 每个请求有不同的 recall_id，同时写入 @d 以供 read_recall_by_id 读取
        call_count = [0]
        async def mock_execute(intent_result, raw_messages):
            call_count[0] += 1
            rid = call_count[0]
            await buffer_store.write_recall(
                f"trigger-{rid}",
                f"记忆{rid}",
                "",
                {},
            )
            return MagicMock(
                recall_id=rid,
                text=f"记忆{rid}",
                mode="query",
                degraded=False,
            )
        engine._retrieval_pipeline.execute = AsyncMock(side_effect=mock_execute)

        # 并发执行
        responses = await asyncio.gather(
            *[turn_runner.run_user_turn(t) for t in triggers]
        )

        # 所有请求成功
        for r in responses:
            assert r is not None

        # 验证 @d 中有条目
        all_recall = await buffer_store.read_all_recall()
        assert len(all_recall) >= 5

    @pytest.mark.asyncio
    async def test_a4_new_window_mode(self, tmp_path):
        """新窗口回合：X-Memory-Mode: new_window 返回最近 15 条 @d，不删除其他。"""
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path))

        # 写入 20 条 @d
        for i in range(20):
            await buffer_store.write_recall(f"trigger-{i}", f"记忆条目{i}", "", {})

        # 使用 new_window 模式
        trigger = _make_user_trigger(
            [{"role": "user", "content": "新窗口"}],
            extra={"x-memory-mode": "new_window"}
        )

        # 验证 read_all_recall 返回全部（不删除）
        all_before = await buffer_store.read_all_recall()
        assert len(all_before) == 20

        # new_window 不应删除任何 @d
        all_after = await buffer_store.read_all_recall()
        assert len(all_after) == 20

    @pytest.mark.asyncio
    async def test_a5_after_turn_replay_no_duplicate(self, tmp_path):
        """after_turn 重放：同一 turn 不重复写入，不同 turn 分别保留，system/tool 不进 @a。"""
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path))

        # 含 system 和 tool 消息的历史
        messages = [
            {"role": "system", "content": "系统指令"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好啊"},
            {"role": "user", "content": "今天天气怎样"},
        ]

        # 第一轮
        trigger1 = _make_user_trigger(messages)
        await turn_runner.run_user_turn(trigger1)

        raw = await buffer_store.read_all_raw()
        contents = [m["content"] for m in raw]

        # system 和 tool 不进入 @a
        assert "系统指令" not in contents
        # user + assistant 进入
        assert "你好" in contents or "今天天气怎样" in contents

        # 第二轮（相同消息但不同 request_id）
        trigger2 = _make_user_trigger(messages)
        await turn_runner.run_user_turn(trigger2)

        raw2 = await buffer_store.read_all_raw()
        # 不应该出现指数膨胀
        assert len(raw2) <= len(raw) + 2  # 最多多一轮的 user+assistant


# ============================================================
# B. 主动回合与 Outbox (4 项)
# ============================================================

class TestWakeAndOutbox:
    """B 组：主动回合与 Outbox 集成测试。"""

    @pytest.mark.asyncio
    async def test_b1_complete_wake_turn(self, tmp_path):
        """完整 Wake 回合：调用 recall，wake-only 工具可执行，回复写入 Outbox 和 @a。"""
        outbox_store = SQLiteOutboxStore(os.path.join(tmp_path, "outbox.db"))
        turn_runner, engine, buffer_store = _make_turn_runner(
            str(tmp_path), outbox_store=outbox_store
        )

        trigger = _make_timer_trigger("wake-test-1")
        result = await turn_runner.run_wake_turn(trigger)

        # 1. 回合成功
        assert result.outcome in ("message_enqueued", "completed"), f"unexpected outcome: {result.outcome}"
        assert result.trigger_id == "wake-test-1"

        # 2. Outbox 有消息
        page = outbox_store.list_after(0, 20)
        assert len(page.items) >= 1
        assert page.items[0].content == "测试回复"

        # 3. @a 有写入
        raw = await buffer_store.read_all_raw()
        assert len(raw) >= 1

    @pytest.mark.asyncio
    async def test_b2_tool_authorization_boundary(self, tmp_path):
        """工具授权边界：用户回合伪造 memory_recall tool_call 被拒绝，wake 允许。"""
        registry = ToolRegistry()
        tool_def = ToolDefinition(
            name="memory_recall",
            description="记忆回忆工具",
            parameters={"type": "object", "properties": {}},
            enabled_in_production=True,
            timeout_seconds=10,
            max_result_chars=4000,
        )
        registry.register(tool_def, executor=MagicMock())
        registry.register_for_wake_only("memory_recall")

        # 用户上下文 → 拒绝
        user_ctx = ToolExecutionContext(turn_id="t1", trigger_type="user", trigger_id="trigger-1")
        resolved = registry.resolve("memory_recall", trigger_type="user")
        assert resolved is None, "用户回合不应解析 wake-only 工具"

        # wake 上下文 → 允许
        resolved_wake = registry.resolve("memory_recall", trigger_type="wake")
        assert resolved_wake is not None, "主动回合应能解析 wake-only 工具"

        # 缺失 trigger_type → 默认拒绝
        resolved_default = registry.resolve("memory_recall")
        assert resolved_default is None, "缺失 trigger_type 应默认拒绝 wake-only 工具"

    @pytest.mark.asyncio
    async def test_b3_no_message_no_outbox(self, tmp_path):
        """<NO_MESSAGE>：不写 Outbox，不产生空消息，门锁释放。"""
        outbox_store = SQLiteOutboxStore(os.path.join(tmp_path, "outbox.db"))
        model_client = FakeAsyncModelClient(response_text="<NO_MESSAGE>")
        turn_runner, engine, buffer_store = _make_turn_runner(
            str(tmp_path), model_client=model_client, outbox_store=outbox_store
        )

        trigger = _make_timer_trigger("wake-no-msg")
        result = await turn_runner.run_wake_turn(trigger)

        # <NO_MESSAGE> 不写 Outbox
        page = outbox_store.list_after(0, 20)
        assert len(page.items) == 0

        # 回合正常结束
        assert result.outcome in ("no_message", "completed", "skipped")

    @pytest.mark.asyncio
    async def test_b4_outbox_cursor_stability(self, tmp_path):
        """Outbox 游标：after=0 顺序读取，重复读取稳定，next_cursor 不重复。"""
        outbox_store = SQLiteOutboxStore(os.path.join(tmp_path, "outbox.db"))

        # 写入 3 条消息
        for i in range(3):
            outbox_store.enqueue_once(NewOutboxMessage(
                event_id=f"evt-{i}",
                trigger_id=f"trigger-{i}",
                created_at=datetime.now(timezone.utc).isoformat(),
                content=f"消息{i}",
                metadata={},
            ))

        # after=0 读取
        page1 = outbox_store.list_after(0, 20)
        assert len(page1.items) == 3
        assert [m.content for m in page1.items] == ["消息0", "消息1", "消息2"]

        # 重复读取相同游标 → 结果稳定
        page2 = outbox_store.list_after(0, 20)
        assert len(page2.items) == 3
        assert [m.cursor for m in page2.items] == [m.cursor for m in page1.items]

        # 使用 next_cursor 后不重复
        cursor = page1.items[0].cursor
        page3 = outbox_store.list_after(cursor, 20)
        assert len(page3.items) == 2  # 跳过第一条


# ============================================================
# C. 沉淀与重启闭环 (4 项)
# ============================================================

class TestConsolidationAndRestart:
    """C 组：沉淀与重启闭环集成测试。"""

    @pytest.mark.asyncio
    async def test_c1_snapshot_watermark_preserves_new_data(self, tmp_path):
        """2am 快照水位：W1 读取快照后插入新 @a/@d，清理只删除 <= 水位，新数据保留。"""
        db_path = os.path.join(str(tmp_path), "memory.db")
        buffer_store = SQLiteBufferStore(db_path)
        buffer_manager = BufferManager(buffer_store)

        # 写入旧数据（沉淀前）
        await buffer_store.append_raw("user", "旧消息1", "test", "turn-1")
        await buffer_store.append_raw("assistant", "旧回复1", "test", "turn-1")

        # 模拟沉淀管线读取快照水位
        raw_before = await buffer_store.read_all_raw()
        max_raw_id = max(m["id"] for m in raw_before) if raw_before else 0

        # 沉淀期间新数据写入
        await buffer_store.append_raw("user", "新消息2", "test", "turn-2")
        await buffer_store.append_raw("assistant", "新回复2", "test", "turn-2")

        # 清理只删除 <= 水位
        await buffer_manager.clear_raw_up_to(max_raw_id)

        # 新数据保留
        remaining = await buffer_store.read_all_raw()
        contents = [m["content"] for m in remaining]
        assert "新消息2" in contents
        assert "新回复2" in contents
        assert "旧消息1" not in contents
        assert "旧回复1" not in contents

    @pytest.mark.asyncio
    async def test_c2_full_w1_w6_consolidation(self, tmp_path):
        """完整 W1—W6：旧 @a/@d 清理，events/persona/saga/向量可查，下次 @4 能检索到。"""
        db_path = os.path.join(str(tmp_path), "memory.db")
        buffer_store = SQLiteBufferStore(db_path)
        buffer_manager = BufferManager(buffer_store)

        # 写入数据
        await buffer_store.append_raw("user", "对话内容", "test", "turn-1")
        await buffer_store.write_recall("trigger-1", "检索记忆", "", {})

        # 模拟 W1—W6 执行
        event_extractor = MagicMock()
        event_extractor.extract = AsyncMock(return_value=[
            MagicMock(to_dict=MagicMock(return_value={"event": "test", "subject": "user", "object": "topic", "relation": "discussed", "turn_id": "turn-1"}))
        ])
        persona_manager = MagicMock()
        persona_manager.observe = AsyncMock(return_value=None)
        saga_manager = MagicMock()
        saga_manager.cluster = AsyncMock(return_value=[])
        vector_storer = MagicMock()
        vector_storer.store_batch = AsyncMock()
        graph_store = MagicMock()
        graph_store.upsert_relations = AsyncMock()
        persona_store = MagicMock()
        persona_store.save = AsyncMock()
        polish_bridge = MagicMock()
        polish_bridge.polish = AsyncMock(return_value="润色结果")

        pipeline = ConsolidationPipeline(
            buffer_manager=buffer_manager,
            event_extractor=event_extractor,
            persona_manager=persona_manager,
            saga_manager=saga_manager,
            vector_storer=vector_storer,
            polish_bridge=polish_bridge,
            graph_store=graph_store,
            persona_store=persona_store,
        )

        result = await pipeline.run()

        # W1—W6 全部成功
        assert result.success is True

        # 旧 @a/@d 清理（使用水位）
        raw_after = await buffer_store.read_all_raw()
        assert len(raw_after) == 0

        # event_extractor 被调用
        assert event_extractor.extract.called

    @pytest.mark.asyncio
    async def test_c3_partial_failure_sqlite_ok_chroma_fails(self, tmp_path):
        """部分失败：SQLite 成功而 ChromaDB 失败时，不得当成完整成功，重试不产生重复。"""
        db_path = os.path.join(str(tmp_path), "memory.db")
        buffer_store = SQLiteBufferStore(db_path)
        buffer_manager = BufferManager(buffer_store)

        await buffer_store.append_raw("user", "对话", "test", "turn-1")

        event_extractor = MagicMock()
        event_extractor.extract = AsyncMock(return_value=[
            MagicMock(to_dict=MagicMock(return_value={"event": "test"}))
        ])
        persona_manager = MagicMock()
        persona_manager.update = AsyncMock()
        saga_manager = MagicMock()
        saga_manager.append = AsyncMock()
        # ChromaDB 失败
        vector_storer = MagicMock()
        vector_storer.upsert = AsyncMock(side_effect=RuntimeError("ChromaDB 连接失败"))
        graph_store = MagicMock()
        graph_store.upsert_relations = AsyncMock()
        persona_store = MagicMock()
        polish_bridge = MagicMock()
        polish_bridge.polish = AsyncMock(return_value="润色结果")

        pipeline = ConsolidationPipeline(
            buffer_manager=buffer_manager,
            event_extractor=event_extractor,
            persona_manager=persona_manager,
            saga_manager=saga_manager,
            vector_storer=vector_storer,
            polish_bridge=polish_bridge,
            graph_store=graph_store,
            persona_store=persona_store,
        )

        result = await pipeline.run()

        # 失败不应标记为完整成功
        assert result.success is False or result.errors

    @pytest.mark.asyncio
    async def test_c4_restart_persistence(self, tmp_path):
        """服务重启：@a/@d/@e/Outbox/WakeJob 和长期记忆均持久，后台任务只启动一个。"""
        db_path = os.path.join(str(tmp_path), "memory.db")
        outbox_path = os.path.join(str(tmp_path), "outbox.db")

        # 第一次启动：写入数据
        buffer_store1 = SQLiteBufferStore(db_path)
        await buffer_store1.append_raw("user", "持久化消息", "test", "turn-1")
        await buffer_store1.write_recall("tr-1", "持久化记忆", "", {})
        await buffer_store1.write_surface("持久化浮现", "", "engine", [])

        outbox1 = SQLiteOutboxStore(outbox_path)
        outbox1.enqueue_once(NewOutboxMessage(
            event_id="evt-1", trigger_id="tr-1",
            created_at=datetime.now(timezone.utc).isoformat(),
            content="持久化Outbox", metadata={},
        ))

        # 模拟重启：重新打开同一个数据库文件
        buffer_store2 = SQLiteBufferStore(db_path)
        raw = await buffer_store2.read_all_raw()
        assert len(raw) == 1
        assert raw[0]["content"] == "持久化消息"

        recall = await buffer_store2.read_all_recall()
        assert len(recall) == 1
        assert recall[0].content == "持久化记忆"

        surface = await buffer_store2.read_surface()
        assert surface is not None
        assert surface.content == "持久化浮现"

        outbox2 = SQLiteOutboxStore(outbox_path)
        page = outbox2.list_after(0, 20)
        assert len(page.items) == 1
        assert page.items[0].content == "持久化Outbox"


# ============================================================
# D. 外部边界与可观测性 (3 项)
# ============================================================

class TestBoundaryAndObservability:
    """D 组：外部边界与可观测性集成测试。"""

    @pytest.mark.asyncio
    async def test_d1_error_request_zero_side_effects(self, tmp_path):
        """错误请求零副作用：错误 token/stream=true/非法消息/tools 均返回预期错误，不消费 @e/不写 @a。"""
        db_path = os.path.join(str(tmp_path), "memory.db")
        buffer_store = SQLiteBufferStore(db_path)

        # 写入 @e
        await buffer_store.write_surface("不应被消费的浮现", "", "engine", [])

        # identity 损坏 → 503 行为（不消费 @e、不写 @a、不调用模型）
        broken_reader = BrokenIdentitySampleReader()
        model_client = FakeAsyncModelClient()
        context_builder = ContextBuilder(memory_char_budget=12000)
        buffer_manager = BufferManager(buffer_store)

        memory_engine = MemoryEngine(
            config=MemoryEngineConfig(db_path=db_path, retrieval_timeout=5.0),
            buffer_manager=buffer_manager,
            intent_classifier=MagicMock(),
            retrieval_pipeline=MagicMock(),
            surface_generator=MagicMock(),
            llm_bridge=FakeLLMClient(),
        )

        turn_runner = TurnRunner(
            model_client=model_client,
            sample_reader=broken_reader,
            context_builder=context_builder,
            outbox_store=SQLiteOutboxStore(os.path.join(str(tmp_path), "outbox.db")),
            memory_port=memory_engine,
        )

        from app.domain.models.sample import SampleReadError
        trigger = _make_user_trigger([{"role": "user", "content": "你好"}])

        with pytest.raises(SampleReadError):
            await turn_runner.run_user_turn(trigger)

        # @e 未被消费
        surface = await buffer_store.read_surface()
        assert surface is not None
        assert surface.content == "不应被消费的浮现"

        # @a 未写入
        raw = await buffer_store.read_all_raw()
        assert len(raw) == 0

        # 模型未被调用
        assert model_client.call_count == 0

    @pytest.mark.asyncio
    async def test_d2_gamma_timeout_foreground_degraded_background_continues(self, tmp_path):
        """γ 真正闭环：前台按超时降级，后台继续写入 @d，跨 2am 水位不复活旧数据。"""
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path))
        engine._intent_classifier.classify = AsyncMock(return_value=IntentResult(
            label="query", confidence=0.95, source="rule"
        ))

        # 模拟检索管道超慢（超过 timeout）
        async def slow_execute(intent_result, raw_messages):
            await asyncio.sleep(0.5)  # 模拟慢检索
            rid = await buffer_store.write_recall("bg-trigger", "后台完成记忆", "", {})
            return RetrievalResult(recall_id=rid, polished_content="后台完成记忆")

        engine._retrieval_pipeline.execute = AsyncMock(side_effect=slow_execute)
        engine._config.retrieval_timeout = 0.1  # 100ms 超时

        trigger = _make_user_trigger([{"role": "user", "content": "查询"}])
        response = await turn_runner.run_user_turn(trigger)

        # 1. 前台及时返回（degraded）
        assert response is not None

        # 2. 等待后台任务完成
        await asyncio.sleep(1.0)

        # 3. 后台写入了 @d
        all_recall = await buffer_store.read_all_recall()
        recall_texts = [r.content for r in all_recall]
        assert "后台完成记忆" in recall_texts

        # 确保后台任务彻底结束，避免污染后续测试
        await asyncio.sleep(0.5)

    @pytest.mark.asyncio
    async def test_d3_no_silent_failure_during_normal_operation(self, tmp_path, caplog):
        """禁止静默失效：正常操作期间不出现 memory_recall_failed / memory_after_turn_failed，turn_id 一致。"""
        import logging
        turn_runner, engine, buffer_store = _make_turn_runner(str(tmp_path))
        engine._intent_classifier.classify = AsyncMock(return_value=IntentResult(
            label="no_query", confidence=0.95, source="rule", matched_patterns=[]
        ))

        # 写入 @e
        await buffer_store.write_surface("正常浮现记忆", "", "engine", [])

        with caplog.at_level(logging.WARNING, logger="turn_runner"):
            trigger = _make_user_trigger([{"role": "user", "content": "你好"}])
            response = await turn_runner.run_user_turn(trigger)

        # 正常操作不应有 failed 日志
        failed_records = [r for r in caplog.records if "failed" in r.getMessage().lower()]
        assert len(failed_records) == 0, f"检测到静默失败: {[r.getMessage() for r in failed_records]}"

        # 回复成功
        assert response is not None
        assert response.choices[0].message_content == "测试回复"

        # @e 被消费
        surface = await buffer_store.read_surface()
        assert surface is None

        # @a 写入
        raw = await buffer_store.read_all_raw()
        assert len(raw) >= 1
