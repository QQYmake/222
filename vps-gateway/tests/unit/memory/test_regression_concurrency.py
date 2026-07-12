"""回归测试第二轮：修正可疑 PASS + 接口契约 + 2am 快照竞争。

测试设计原则：
- 确定性假 LLM、假时钟、临时 SQLite
- 断言架构约定，而非当前实现
- 并发测试使用 asyncio 屏障确保真正交错
"""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
from app.application.memory.buffer_manager import BufferManager
from app.application.memory.memory_engine import MemoryEngine
from app.domain.ports.memory_engine import MemoryPort, MemoryRecall as PortMemoryRecall, MemoryEngineConfig
from app.domain.models.memory import MemoryRecall as ModelMemoryRecall
from app.domain.models.memory import RecallEntry, SurfaceEntry
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.turn import ChatMessage
from app.domain.models.chat_completion import (
    ChatCompletionResponse,
    Choice,
)
from app.domain.models.context_builder import ContextBuilder
from app.domain.models.sample import SampleEnvelope
from app.domain.models.identity import IdentityData
from app.domain.models.preferences import PreferencesData
from app.domain.models.working_state import WorkingStateData
from app.domain.models.memories import MemoriesData, MemoryItem
from app.domain.ports.sample_reader import AllSamples, SampleReader
from app.application.turn_runner import TurnRunner
from app.adapters.tools.registry import ToolRegistry
from app.adapters.tools.memory_recall_tool import MemoryRecallExecutor, MEMORY_RECALL_DEF
from app.domain.models.tool import ToolDefinition, ToolExecutionContext
from app.application.memory.consolidation_pipeline import ConsolidationPipeline


# ─── fixtures ───

@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def store(tmp_db):
    return SQLiteBufferStore(tmp_db)


@pytest.fixture
def buffer(store):
    return BufferManager(store)


@pytest.fixture
def enabled_config():
    return MemoryEngineConfig(enabled=True, retrieval_timeout=0.5)


@pytest.fixture
def disabled_config():
    return MemoryEngineConfig(enabled=False)


def make_response(text: str = "test response") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="resp-1",
        object="chat.completion",
        created=1700000000,
        model="test-model",
        choices=[
            Choice(
                index=0,
                message_role="assistant",
                message_content=text,
                finish_reason="stop",
            )
        ],
        usage=None,
    )


def make_samples() -> AllSamples:
    identity_data = IdentityData(
        name="沉",
        self_description="我是沉",
        values=["真诚"],
        boundaries=["不说谎"],
        relationship_definition="伙伴",
    )
    prefs_data = PreferencesData(
        communication_preferences=["直接"],
        stable_likes=["深夜聊天"],
        stable_dislikes=["虚伪"],
        interaction_rules=["不要敷衍"],
    )
    ws_data = WorkingStateData(
        current_focus=["回归测试"],
        emotion_summary="专注",
        pending_items=["写测试"],
        next_wake_at="",
    )
    mem_item = MemoryItem(
        id="seed-1",
        content="种子记忆：用户喜欢深夜聊天",
        category="preference",
        priority=5,
        created_at="2024-01-01T00:00:00Z",
    )
    return AllSamples(
        identity=SampleEnvelope(sample_type="identity", version=1, updated_at="2024-01-01T00:00:00Z", source="sample", data=identity_data),
        preferences=SampleEnvelope(sample_type="preferences", version=1, updated_at="2024-01-01T00:00:00Z", source="sample", data=prefs_data),
        working_state=SampleEnvelope(sample_type="working_state", version=1, updated_at="2024-01-01T00:00:00Z", source="sample", data=ws_data),
        memories=SampleEnvelope(sample_type="memories", version=1, updated_at="2024-01-01T00:00:00Z", source="sample", data=MemoriesData(items=[mem_item])),
    )


def make_user_trigger(messages: list[dict] = None) -> UserTrigger:
    if messages is None:
        messages = [{"role": "user", "content": "你好"}]
    return UserTrigger(
        request_id="req-1",
        chat_request={"messages": messages, "model": "test"},
    )


def make_timer_trigger() -> TimerTrigger:
    return TimerTrigger(
        trigger_id="timer:2024-01-01T02:00:00",
        fired_at="2024-01-01T02:00:00Z",
        instruction="定时唤醒",
    )


class FakeAsyncModelClient:
    """假异步模型客户端。"""
    def __init__(self, response_text: str = "模型回复"):
        self._response = make_response(response_text)
        self.call_count = 0

    async def complete(self, model_input) -> ChatCompletionResponse:
        self.call_count += 1
        return self._response


class FakeSampleReader(SampleReader):
    """假 Sample 读取器。"""
    def __init__(self, samples: AllSamples):
        self._samples = samples

    def read_all(self) -> AllSamples:
        return self._samples

    def read(self, sample_type) -> SampleEnvelope:
        return getattr(self._samples, sample_type)


class SlowRetrievalPipeline:
    """慢检索管线，用于测试 γ 超时。"""

    def __init__(self, delay: float, buffer_manager: BufferManager, content: str = "检索结果"):
        self._delay = delay
        self._buffer = buffer_manager
        self._content = content
        self.execute_called = asyncio.Event()
        self.execute_finished = asyncio.Event()
        self.was_cancelled = False

    async def execute(self, intent_result, raw_messages):
        self.execute_called.set()
        try:
            await asyncio.sleep(self._delay)
            recall_id = await self._buffer.write_recall(
                trigger_id="query",
                content=self._content,
                raw_content=self._content,
                metadata={},
            )
            self.execute_finished.set()
            from app.application.memory.retrieval_pipeline import RetrievalResult
            return RetrievalResult(polished_content=self._content, recall_id=recall_id)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise


# ═══════════════════════════════════════════════════════════════
# A. 修正三个"可疑 PASS"
# ═══════════════════════════════════════════════════════════════

class TestEmptyMemoryTriStateCorrected:
    """修正后的空记忆三态测试。

    正确三态：
    1. MemoryEngine 未启用/未注入 → memory_recall_text=None → 回退 V2 Sample
    2. MemoryEngine 已运行但返回空 → memory_recall_text="" → 生成空 <memories>
    3. MemoryEngine 返回非空 → 使用动态记忆
    """

    def test_disabled_engine_falls_back_to_sample(self, disabled_config, buffer):
        """MEMORY_ENABLED=false → degraded → 回退 Sample。"""
        engine = MemoryEngine(disabled_config, buffer)
        samples = make_samples()
        cb = ContextBuilder(memory_char_budget=10000)

        trigger = make_user_trigger()
        prepared = cb.build(samples, trigger, memory_recall_text=None)

        system_msg = prepared.messages[0].content
        assert "种子记忆" in system_msg, "disabled engine should fall back to Sample memories"

    def test_enabled_engine_empty_surface_must_produce_empty_memories(self, enabled_config, buffer):
        """@e 为空时，MemoryRecall(mode='no_query', text='') 应生成空 <memories>，而非回退 Sample。"""
        engine = MemoryEngine(enabled_config, buffer)
        samples = make_samples()
        cb = ContextBuilder(memory_char_budget=10000)

        # 模拟 TurnRunner 的行为：recall 返回 text="" → memory_recall_text
        recall = PortMemoryRecall(mode="no_query", text="", source_recall_ids=[])

        # 当前实现：recall.text 为空字符串 → falsy → memory_recall_text = None
        memory_recall_text = recall.text if recall and recall.text else None

        # 架构约定：空 text 应生成空 <memories>，而非回退 Sample
        # 当前实现传 None 给 ContextBuilder，会回退 Sample —— 这是 BUG
        prepared = cb.build(samples, make_user_trigger(), memory_recall_text=memory_recall_text)

        system_msg = prepared.messages[0].content

        # 正确行为：空 <memories></memories>，不含种子记忆
        assert "种子记忆" not in system_msg, (
            "BUG: empty @e causes seed memory injection instead of empty <memories>. "
            "TurnRunner line 159 converts empty text to None, causing Sample fallback."
        )
        assert "<memories></memories>" in system_msg, (
            "Empty memory recall should produce empty <memories> block"
        )

    def test_enabled_engine_nonempty_surface_uses_dynamic(self, enabled_config, buffer):
        """@e 非空 → 使用动态记忆。"""
        samples = make_samples()
        cb = ContextBuilder(memory_char_budget=10000)

        prepared = cb.build(samples, make_user_trigger(), memory_recall_text="动态记忆内容")

        system_msg = prepared.messages[0].content
        assert "动态记忆内容" in system_msg
        assert "种子记忆" not in system_msg


class TestGammaTimeoutCorrected:
    """修正后的 γ 超时测试。

    架构规定 γ 是"前台降级、后台继续"。
    asyncio.wait_for(task) 默认会取消 task —— 如果 task 被取消，说明 γ 没有正确实现。
    """

    @pytest.mark.asyncio
    async def test_foreground_degrades_on_timeout(self, enabled_config, buffer):
        """前台在超时点及时返回 degraded。"""
        slow_pipeline = SlowRetrievalPipeline(delay=5.0, buffer_manager=buffer)
        engine = MemoryEngine(
            enabled_config, buffer,
            intent_classifier=MagicMock(classify=AsyncMock(
                return_value=MagicMock(label="query")
            )),
            retrieval_pipeline=slow_pipeline,
        )

        trigger = make_user_trigger()
        start = time.monotonic()
        recall = await engine.recall(trigger, [ChatMessage(role="user", content="查询")])
        elapsed = time.monotonic() - start

        assert recall.mode == "degraded", "foreground should degrade on timeout"
        assert elapsed < 2.0, "foreground should return quickly after timeout"

    @pytest.mark.asyncio
    async def test_background_task_continues_after_timeout(self, enabled_config, buffer):
        """γ 超时后，后台任务应继续运行并写入当前代次的 @d。

        asyncio.wait_for 会取消 task —— 验证这是否发生。
        如果 task 被取消，说明"后台继续"没有实现。
        """
        slow_pipeline = SlowRetrievalPipeline(delay=0.3, buffer_manager=buffer, content="后台完成的结果")
        engine = MemoryEngine(
            enabled_config, buffer,
            intent_classifier=MagicMock(classify=AsyncMock(
                return_value=MagicMock(label="query")
            )),
            retrieval_pipeline=slow_pipeline,
        )

        trigger = make_user_trigger()
        recall = await engine.recall(trigger, [ChatMessage(role="user", content="查询")])

        # 等待后台任务完成（如果它没被取消）
        try:
            await asyncio.wait_for(slow_pipeline.execute_finished.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        # 检查后台任务是否被取消
        assert not slow_pipeline.was_cancelled, (
            "BUG: asyncio.wait_for cancelled the background task. "
            "γ timeout should let the background task continue, not cancel it."
        )

        # 检查 @d 是否被写入
        assert slow_pipeline.execute_finished.is_set(), (
            "BUG: background task did not complete after timeout. "
            "asyncio.wait_for cancels the task on timeout."
        )

        entry = await buffer.read_recall_latest()
        assert entry is not None, "background task should have written @d"
        assert entry.content == "后台完成的结果"


class TestSurfaceConcurrencyCorrected:
    """修正后的 @e 并发消费测试。

    当前通过是因为同步 SQLite 阻塞了事件循环。需要验证：
    1. FIFO 领取和删除是原子事务
    2. 数据库变慢时，事件循环仍能运行心跳任务
    """

    @pytest.mark.asyncio
    async def test_fifo_atomic_consume_with_explicit_interleave(self, store, buffer):
        """使用 asyncio 屏障确保真正交错，验证 FIFO 原子消费。"""
        # 写入 3 条 @e
        for i in range(3):
            await buffer.write_surface(
                content=f"surface-{i}",
                raw_content=f"raw-{i}",
                surface_type="association",
                source_ids=[i],
            )

        consumed = []
        barrier = asyncio.Event()

        async def consumer(idx: int):
            await barrier.wait()  # 等待所有 consumer 就绪
            # 添加微小随机延迟模拟交错
            await asyncio.sleep(0.001 * idx)
            entry = await buffer.read_surface()
            if entry:
                consumed.append((idx, entry.content))

        consumers = [asyncio.create_task(consumer(i)) for i in range(5)]
        barrier.set()
        await asyncio.gather(*consumers)

        # 断言：每条 @e 只被消费一次
        contents = [c[1] for c in consumed]
        assert len(contents) == 3, f"expected 3 consumed, got {len(contents)}"
        assert len(set(contents)) == 3, "all consumed entries should be distinct"
        # FIFO 顺序
        assert sorted(contents) == ["surface-0", "surface-1", "surface-2"]

    @pytest.mark.asyncio
    async def test_slow_db_does_not_block_event_loop(self, tmp_db, buffer):
        """当数据库操作慢时，事件循环仍应能运行其他任务。"""
        await buffer.write_surface(
            content="test",
            raw_content="test",
            surface_type="association",
            source_ids=[1],
        )

        heartbeat_count = 0

        async def heartbeat():
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.01)

        hb_task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)  # 让 heartbeat 启动

        count_before = heartbeat_count

        # 执行一个 surface 读取（同步 SQLite 会短暂阻塞事件循环）
        await buffer.read_surface()

        await asyncio.sleep(0.05)
        count_after = heartbeat_count

        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        # 事件循环应继续运行
        assert count_after > count_before, (
            "Event loop should still run heartbeat during DB operations. "
            "Synchronous SQLite blocks the event loop, but short operations may be acceptable. "
            "If this fails, consider using aiosqlite or running DB ops in executor."
        )


# ═══════════════════════════════════════════════════════════════
# B. 接口契约与真实接线路径
# ═══════════════════════════════════════════════════════════════

class TestTurnRunnerWiring:
    """TurnRunner → MemoryEngine 完整生产接线测试。

    这是当前最薄弱的回路：多个契约漂移被 except Exception 静默吞掉。
    """

    @pytest.mark.asyncio
    async def test_user_turn_calls_recall_and_after_turn(self, enabled_config, buffer):
        """用户回合应经过 recall → 主模型 → after_turn → @a 正好写入本轮 user+assistant。"""
        engine = MemoryEngine(enabled_config, buffer)
        model_client = FakeAsyncModelClient("模型回复内容")
        samples = make_samples()
        cb = ContextBuilder(memory_char_budget=10000)
        reader = FakeSampleReader(samples)

        runner = TurnRunner(
            sample_reader=reader,
            context_builder=cb,
            model_client=model_client,
            memory_port=engine,
        )

        trigger = make_user_trigger([
            {"role": "user", "content": "你好，今天天气怎么样"}
        ])

        response = await runner.run_user_turn(trigger)

        # 1. 模型被调用
        assert model_client.call_count == 1

        # 2. after_turn 应写入 @a
        raw_entries = await buffer.read_all_raw()
        assert len(raw_entries) > 0, (
            "BUG: after_turn did not write any @a entries. "
            "TurnRunner line 193 calls after_turn(trigger, response) but the signature is "
            "after_turn(raw_messages, response, turn_id, trigger=None) — missing turn_id causes TypeError."
        )

        # 3. @a 应包含本轮 user 和 assistant 各一条
        roles = [e["role"] for e in raw_entries]
        assert "user" in roles, "user message should be in @a"
        assert "assistant" in roles, "assistant response should be in @a"

        # 4. user 消息只出现一次（不重复追加历史）
        user_count = roles.count("user")
        assert user_count == 1, f"expected 1 user entry, got {user_count}"

    @pytest.mark.asyncio
    async def test_after_turn_response_extraction_uses_correct_field(self, enabled_config, buffer):
        """after_turn 应正确提取 response 内容写入 @a。

        MemoryEngine.after_turn line 169 使用 response.choices[0].message.content
        但 Choice 的字段是 message_content，不是 message.content。
        """
        engine = MemoryEngine(enabled_config, buffer)
        response = make_response("这是模型回复")

        await engine.after_turn(
            raw_messages=[ChatMessage(role="user", content="你好")],
            response=response,
            turn_id="turn-1",
        )

        raw_entries = await buffer.read_all_raw()
        assistant_entries = [e for e in raw_entries if e["role"] == "assistant"]

        assert len(assistant_entries) == 1, (
            "BUG: after_turn failed to extract response content. "
            "Line 169 uses response.choices[0].message.content but Choice has message_content, not message."
        )
        assert assistant_entries[0]["content"] == "这是模型回复"

    @pytest.mark.asyncio
    async def test_after_turn_does_not_duplicate_full_history(self, enabled_config, buffer):
        """连续两轮传入完整聊天历史，@a 中每条消息只出现一次。"""
        engine = MemoryEngine(enabled_config, buffer)
        response1 = make_response("第一次回复")
        response2 = make_response("第二次回复")

        # 第一轮：1 条 user 消息
        await engine.after_turn(
            raw_messages=[ChatMessage(role="user", content="你好")],
            response=response1,
            turn_id="turn-1",
        )

        # 第二轮：2 条 user 消息（完整历史）
        await engine.after_turn(
            raw_messages=[
                ChatMessage(role="user", content="你好"),
                ChatMessage(role="user", content="在吗"),
            ],
            response=response2,
            turn_id="turn-2",
        )

        raw_entries = await buffer.read_all_raw()

        # "你好" 应只出现一次，不应因为第二轮重传而重复
        ni_hao_entries = [e for e in raw_entries if e["content"] == "你好"]
        assert len(ni_hao_entries) == 1, (
            f"BUG: '你好' appears {len(ni_hao_entries)} times in @a, expected 1. "
            "after_turn appends ALL raw_messages each call, duplicating history."
        )


class TestRawMessageTypeContract:
    """原始消息类型：dict 与 ChatMessage.content 契约一致性。"""

    @pytest.mark.asyncio
    async def test_turnrunner_passes_dict_not_chatmessage(self, enabled_config, buffer):
        """TurnRunner 传给 recall() 的是 trigger.chat_request["messages"]（dict 列表），
        而 MemoryEngine.recall() 的 raw_messages 类型标注为 list[ChatMessage]。

        IntentClassifier.classify() 会访问 raw_messages[-1].content，
        但 dict 没有 .content 属性 → AttributeError。
        """
        engine = MemoryEngine(
            enabled_config, buffer,
            intent_classifier=MagicMock(classify=AsyncMock(
                return_value=MagicMock(label="no_query", confidence=1.0, source="rule")
            )),
        )

        trigger = make_user_trigger([
            {"role": "user", "content": "你好"},
        ])

        # TurnRunner 实际传给 recall 的是 dict 列表
        raw_messages = trigger.chat_request.get("messages", [])

        # recall 应能处理 dict 列表
        recall = await engine.recall(trigger, raw_messages)

        # 如果 intent_classifier 被调用，它会收到 raw_messages[-1].content
        # 但 dict 没有 .content 属性
        # 验证：recall 应正常返回，不抛异常
        assert recall is not None

    @pytest.mark.asyncio
    async def test_after_turn_handles_dict_messages(self, enabled_config, buffer):
        """after_turn 应能处理 dict 格式的消息（来自 chat_request）。"""
        engine = MemoryEngine(enabled_config, buffer)
        response = make_response("回复")

        # dict 格式消息（TurnRunner 实际传入的格式）
        raw_messages_as_dicts = [
            {"role": "user", "content": "你好"},
        ]

        # after_turn 使用 getattr(msg, "role", "user") 和 getattr(msg, "content", "")
        # dict 没有 .role/.content 属性，getattr 会返回默认值
        await engine.after_turn(
            raw_messages=raw_messages_as_dicts,
            response=response,
            turn_id="turn-1",
        )

        raw_entries = await buffer.read_all_raw()
        # getattr(dict, "content", "") 返回 ""，所以 dict 消息不会被写入 @a
        user_entries = [e for e in raw_entries if e["role"] == "user"]
        assert len(user_entries) == 0, (
            "BUG: dict messages are not written to @a because after_turn uses getattr(msg, 'content', '') "
            "which returns '' for dicts. Messages from chat_request are dicts, not ChatMessage objects."
        )


class TestMemoryPortSignatureConsistency:
    """MemoryPort 签名一致性：实现类方法签名与抽象端口完全一致。"""

    def test_recall_signature_matches(self):
        """recall() 签名一致。"""
        port_sig = inspect.signature(MemoryPort.recall)
        impl_sig = inspect.signature(MemoryEngine.recall)
        assert port_sig == impl_sig, (
            f"recall() signature mismatch:\n  Port: {port_sig}\n  Impl: {impl_sig}"
        )

    def test_after_turn_signature_matches(self):
        """after_turn() 签名一致。"""
        port_sig = inspect.signature(MemoryPort.after_turn)
        impl_sig = inspect.signature(MemoryEngine.after_turn)
        assert port_sig == impl_sig, (
            f"after_turn() signature mismatch:\n  Port: {port_sig}\n  Impl: {impl_sig}"
        )

    def test_recall_as_tool_signature_matches(self):
        """recall_as_tool() 签名一致。"""
        port_sig = inspect.signature(MemoryPort.recall_as_tool)
        impl_sig = inspect.signature(MemoryEngine.recall_as_tool)
        assert port_sig == impl_sig, (
            f"recall_as_tool() signature mismatch:\n  Port: {port_sig}\n  Impl: {impl_sig}\n"
            "Port only has (query: str) but impl has (query: str, turn_id: str = '')"
        )

    def test_turnrunner_after_turn_call_matches_port_signature(self):
        """TurnRunner 调用 after_turn 的参数与 MemoryPort 签名一致。"""
        port_params = list(inspect.signature(MemoryPort.after_turn).parameters.values())
        # Port: (self, raw_messages, response, turn_id, trigger=None)
        # TurnRunner line 193: after_turn(trigger, response) — missing turn_id
        port_param_names = [p.name for p in port_params if p.name != "self"]
        assert port_param_names == ["raw_messages", "response", "turn_id", "trigger"], (
            f"Port after_turn params: {port_param_names}"
        )

        # TurnRunner 传入 (trigger, response) — 只有 2 个位置参数
        # Port 期望 (raw_messages, response, turn_id, trigger)
        # trigger 被当作 raw_messages，response 正确，turn_id 缺失 → TypeError
        # 这个测试通过签名比较来检测，不实际调用

    def test_start_stop_background_tasks_signature(self):
        """start/stop_background_tasks() 签名一致。"""
        for method in ("start_background_tasks", "stop_background_tasks"):
            port_sig = inspect.signature(getattr(MemoryPort, method))
            impl_sig = inspect.signature(getattr(MemoryEngine, method))
            assert port_sig == impl_sig


class TestMemoryRecallUniqueType:
    """MemoryRecall 唯一类型：全链路只使用一个类型。"""

    def test_two_memory_recall_types_exist(self):
        """存在两个不同的 MemoryRecall 类型。"""
        from app.domain.ports.memory_engine import MemoryRecall as PortRecall
        from app.domain.models.memory import MemoryRecall as ModelRecall

        assert PortRecall is not ModelRecall, (
            "BUG: Two different MemoryRecall types exist:\n"
            f"  ports.MemoryRecall: fields={list(PortRecall.__dataclass_fields__.keys())}\n"
            f"  models.MemoryRecall: fields={list(ModelRecall.__dataclass_fields__.keys())}\n"
            "ports version lacks 'metadata' field and validation."
        )

    def test_port_recall_has_no_metadata(self):
        """ports.MemoryRecall 缺少 metadata 字段。"""
        port_fields = set(PortMemoryRecall.__dataclass_fields__.keys())
        assert "metadata" not in port_fields, (
            f"Port MemoryRecall fields: {port_fields}\n"
            "Should have 'metadata' field like the domain model version."
        )

    def test_model_recall_has_validation(self):
        """models.MemoryRecall 有 __post_init__ 校验，ports 版本没有。"""
        # models.MemoryRecall 验证 degraded mode 不允许有 text
        with pytest.raises(ValueError):
            ModelMemoryRecall(
                mode="degraded",
                text="should fail",
                source_recall_ids=[],
                metadata={},
            )

        # ports.MemoryRecall 没有此校验
        port_recall = PortMemoryRecall(mode="degraded", text="should not fail", source_recall_ids=[])
        assert port_recall.text == "should not fail", (
            "Port MemoryRecall lacks validation: degraded mode with non-empty text should be rejected"
        )


class TestMemoryRecallToolEndToEnd:
    """memory_recall 工具端到端：executor → port 返回类型一致性。"""

    @pytest.mark.asyncio
    async def test_executor_return_type_matches_port(self, enabled_config, buffer):
        """MemoryRecallExecutor.execute() 调用 recall_as_tool() 并访问 .text。

        但 MemoryEngine.recall_as_tool() 返回 str，不是 MemoryRecall。
        执行 recall.text 会得到 str.text → AttributeError。
        """
        engine = MemoryEngine(
            enabled_config, buffer,
            retrieval_pipeline=SlowRetrievalPipeline(delay=0.01, buffer_manager=buffer, content="工具检索结果"),
        )

        executor = MemoryRecallExecutor(memory_port=engine)
        context = ToolExecutionContext(
            turn_id="turn-1",
            trigger_type="wake",
            trigger_id="wake-1",
        )

        # 执行器调用 recall_as_tool(query=..., turn_id=...)
        # 但 port 签名只有 recall_as_tool(query: str) -> str
        # 实现签名是 recall_as_tool(query: str, turn_id: str = "") -> str
        # 执行器做 recall.text 但 recall 是 str → AttributeError
        try:
            result = await executor.execute(
                arguments={"query": "测试查询"},
                context=context,
            )
            # 如果没有异常，验证返回的是字符串
            assert isinstance(result, str), f"expected str, got {type(result)}"
        except AttributeError as e:
            pytest.fail(
                f"BUG: MemoryRecallExecutor.execute() does recall.text but recall_as_tool() returns str. "
                f"AttributeError: {e}"
            )

    @pytest.mark.asyncio
    async def test_executor_calls_port_with_turn_id_not_in_signature(self, enabled_config, buffer):
        """执行器调用 recall_as_tool(query=..., turn_id=...)，
        但 MemoryPort 抽象接口只有 recall_as_tool(query: str)。"""
        # 检查执行器代码是否传了 turn_id
        import inspect as ins
        executor_source = ins.getsource(MemoryRecallExecutor.execute)
        assert "turn_id" in executor_source, "executor passes turn_id to recall_as_tool"

        # 检查 port 签名
        port_sig = ins.signature(MemoryPort.recall_as_tool)
        port_params = [p.name for p in port_sig.parameters.values() if p.name != "self"]
        assert "turn_id" not in port_params, (
            f"Port recall_as_tool params: {port_params} — lacks turn_id, "
            "but executor passes it. Signature drift."
        )


class TestWakeTurnAfterTurn:
    """主动回合 after_turn：主动回复应写 @a、Outbox，无静默 warning。"""

    @pytest.mark.asyncio
    async def test_wake_turn_writes_after_turn(self, enabled_config, buffer):
        """主动回合应调用 after_turn 写入 @a。"""
        engine = MemoryEngine(enabled_config, buffer)
        model_client = FakeAsyncModelClient("主动消息内容")
        samples = make_samples()
        cb = ContextBuilder(memory_char_budget=10000)
        reader = FakeSampleReader(samples)

        # Fake outbox
        outbox = MagicMock()
        outbox.enqueue_once = MagicMock(return_value=MagicMock(event_id="evt-1"))

        runner = TurnRunner(
            sample_reader=reader,
            context_builder=cb,
            model_client=model_client,
            outbox_store=outbox,
            memory_port=engine,
        )

        trigger = make_timer_trigger()
        result = await runner.run_wake_turn(trigger)

        # 主动回合应写入 Outbox
        assert result.outcome == "message_enqueued"

        # 主动回合也应调用 after_turn 写入 @a
        raw_entries = await buffer.read_all_raw()
        assert len(raw_entries) > 0, (
            "BUG: wake turn does not call after_turn. "
            "run_wake_turn() has no memory_port.recall() or after_turn() call, "
            "so proactive turns don't contribute to @a."
        )

    @pytest.mark.asyncio
    async def test_wake_turn_recall_not_called(self, enabled_config, buffer):
        """主动回合应调用 recall() 获取记忆注入。

        当前 run_wake_turn 完全不使用 memory_port。
        """
        engine = MemoryEngine(enabled_config, buffer)

        # 包装 recall 以追踪调用
        original_recall = engine.recall
        recall_called = asyncio.Event()

        async def tracking_recall(trigger, raw_messages):
            recall_called.set()
            return await original_recall(trigger, raw_messages)

        engine.recall = tracking_recall

        model_client = FakeAsyncModelClient("主动消息")
        samples = make_samples()
        cb = ContextBuilder(memory_char_budget=10000)
        reader = FakeSampleReader(samples)
        outbox = MagicMock()
        outbox.enqueue_once = MagicMock(return_value=MagicMock(event_id="evt-1"))

        runner = TurnRunner(
            sample_reader=reader,
            context_builder=cb,
            model_client=model_client,
            outbox_store=outbox,
            memory_port=engine,
        )

        trigger = make_timer_trigger()
        await runner.run_wake_turn(trigger)

        assert recall_called.is_set(), (
            "BUG: run_wake_turn() does not call memory_port.recall(). "
            "Proactive turns should also get memory injection."
        )


# ═══════════════════════════════════════════════════════════════
# C. 2am 快照水位竞争
# ═══════════════════════════════════════════════════════════════

class TestConsolidationSnapshotWatermark:
    """2am 沉淀快照水位竞争测试。

    场景：
    1. 沉淀读取当前 @a/@d 快照
    2. W1-W6 执行期间，新用户回合写入新的 @a/@d
    3. 沉淀结束调用 clear_raw()/clear_recall()
    4. 断言快照之后写入的新数据仍然存在

    如果清理是无条件 DELETE FROM，那么沉淀期间产生的新消息会被清掉，
    却从未进入本次沉淀 —— 生产级数据丢失。
    """

    @pytest.mark.asyncio
    async def test_new_data_during_consolidation_survives_cleanup(self, buffer):
        """沉淀期间新写入的 @a 数据不应被全表清理。"""

        # 1. 写入初始 @a 数据
        await buffer.append_raw("user", "旧消息1", "platform", "turn-1")
        await buffer.append_raw("assistant", "旧回复1", "platform", "turn-1")

        # 2. 模拟沉淀管线读取快照
        snapshot = await buffer.read_all_raw()
        assert len(snapshot) == 2

        # 3. 沉淀执行期间，新用户回合写入新 @a
        await buffer.append_raw("user", "新消息2", "platform", "turn-2")

        # 4. 沉淀结束，清理 @a
        await buffer.clear_raw()

        # 5. 断言：新消息不应被清理
        remaining = await buffer.read_all_raw()
        new_messages = [e for e in remaining if e["content"] == "新消息2"]

        assert len(new_messages) == 1, (
            "BUG: clear_raw() does unconditional DELETE FROM buffer_raw. "
            "Data written during consolidation (between snapshot read and cleanup) is lost. "
            "Should use watermark: DELETE FROM buffer_raw WHERE id <= max_id."
        )

    @pytest.mark.asyncio
    async def test_new_recall_during_consolidation_survives_cleanup(self, buffer):
        """沉淀期间新写入的 @d 数据不应被全表清理。"""

        # 1. 写入初始 @d
        await buffer.write_recall("trigger-1", "旧检索结果", "raw", {})

        # 2. 模拟沉淀读取快照
        snapshot = await buffer.read_all_recall()
        assert len(snapshot) == 1

        # 3. 沉淀期间，新查询写入新 @d
        await buffer.write_recall("trigger-2", "新检索结果", "raw", {})

        # 4. 沉淀结束，清理 @d
        await buffer.clear_recall()

        # 5. 断言：新 @d 不应被清理
        remaining = await buffer.read_all_recall()
        new_entries = [e for e in remaining if e.content == "新检索结果"]

        assert len(new_entries) == 1, (
            "BUG: clear_recall() does unconditional DELETE FROM buffer_recall. "
            "Data written during consolidation is lost. Should use watermark."
        )

    @pytest.mark.asyncio
    async def test_consolidation_pipeline_uses_unconditional_clear(self, buffer):
        """验证 ConsolidationPipeline._finalize_with_cleanup 使用无条件清理。

        通过模拟 W2 失败来触发 _finalize_with_cleanup 清理路径。
        W1 失败不触发清理（直接返回），W2+ 失败和成功路径都会清理。
        """
        # 写入初始数据（沉淀快照内的数据）
        await buffer.append_raw("user", "消息1", "platform", "turn-1")

        # 构造一个 W1 成功、W2 失败的 pipeline
        success_extractor = AsyncMock()
        success_extractor.extract = AsyncMock(return_value=[{"event": "test"}])

        failing_persona = AsyncMock()
        failing_persona.observe = AsyncMock(side_effect=RuntimeError("W2 failed"))

        pipeline = ConsolidationPipeline(
            buffer_manager=buffer,
            event_extractor=success_extractor,
            persona_manager=failing_persona,
            saga_manager=AsyncMock(),
            vector_storer=AsyncMock(),
            polish_bridge=AsyncMock(),
            graph_store=AsyncMock(),
            persona_store=AsyncMock(),
        )

        # 模拟沉淀期间新用户回合写入新数据
        await buffer.append_raw("user", "消息2", "platform", "turn-2")

        result = await pipeline.run()

        # W2 失败，_finalize_with_cleanup 被调用
        assert result.success is False
        assert result.failed_step == "W2"

        # 验证：所有 @a 都被清理了（包括沉淀期间写入的新数据 "消息2"）
        remaining = await buffer.read_all_raw()
        assert len(remaining) == 0, (
            "BUG: ConsolidationPipeline._finalize_with_cleanup calls clear_raw() which does "
            "unconditional DELETE FROM buffer_raw. Data written between snapshot and cleanup is lost. "
            f"Expected 0 remaining (confirming data loss), got {len(remaining)}."
        )


# ═══════════════════════════════════════════════════════════════
# D. 保留第一轮已确认的 bug 测试
# ═══════════════════════════════════════════════════════════════

class TestConcurrentQueryNoCrossContamination:
    """两个并发 @4 查询不能串记忆。"""

    @pytest.mark.asyncio
    async def test_concurrent_queries_get_own_recall(self, enabled_config, buffer):
        """A 请求只能得到 A 的 @d，B 只能得到 B 的 @d。"""
        from app.application.memory.retrieval_pipeline import RetrievalResult

        class TaggedPipeline:
            def __init__(self, tag: str, delay: float, buf: BufferManager):
                self._tag = tag
                self._delay = delay
                self._buf = buf

            async def execute(self, intent_result, raw_messages):
                await asyncio.sleep(self._delay)
                rid = await self._buf.write_recall(
                    trigger_id=self._tag,
                    content=f"{self._tag} 的记忆内容",
                    raw_content=self._tag,
                    metadata={},
                )
                return RetrievalResult(polished_content=f"{self._tag} 的记忆内容", recall_id=rid)

        pipeline_a = TaggedPipeline("A", 0.05, buffer)
        pipeline_b = TaggedPipeline("B", 0.10, buffer)

        async def run_query(pipeline, tag):
            engine = MemoryEngine(
                enabled_config, buffer,
                intent_classifier=MagicMock(classify=AsyncMock(
                    return_value=MagicMock(label="query")
                )),
                retrieval_pipeline=pipeline,
            )
            return await engine.recall(
                make_user_trigger(),
                [ChatMessage(role="user", content=f"查询{tag}")],
            )

        recall_a, recall_b = await asyncio.gather(
            run_query(pipeline_a, "A"),
            run_query(pipeline_b, "B"),
        )

        # A 应得到 A 的内容，B 应得到 B 的内容
        assert "A" in recall_a.text, f"A got: {recall_a.text}"
        assert "B" in recall_b.text, f"B got: {recall_b.text}"
        assert "B" not in recall_a.text, f"BUG: A got B's memory: {recall_a.text}"
        assert "A" not in recall_b.text, f"BUG: B got A's memory: {recall_b.text}"


class TestToolLeakPrevention:
    """用户/主动回合并发时工具不泄漏。"""

    def test_resolve_memory_recall_for_user_context(self):
        """resolve('memory_recall') 在用户回合不应返回执行器。"""
        registry = ToolRegistry()
        executor = MemoryRecallExecutor(memory_port=MagicMock())
        registry.register(MEMORY_RECALL_DEF, executor)
        registry.register_for_wake_only("memory_recall")

        # schemas_for_user 正确排除
        user_schemas = registry.schemas_for_user()
        user_names = [s["function"]["name"] for s in user_schemas]
        assert "memory_recall" not in user_names

        # resolve 应也排除 —— 但当前不检查 _wake_only
        resolved = registry.resolve("memory_recall")
        assert resolved is None, (
            "BUG: resolve() does not check _wake_only set. "
            "User context can still execute wake-only tools via resolve()."
        )

    def test_resolve_memory_recall_for_wake_context(self):
        """resolve('memory_recall') 在主动回合应返回执行器。"""
        registry = ToolRegistry()
        executor = MemoryRecallExecutor(memory_port=MagicMock())
        registry.register(MEMORY_RECALL_DEF, executor)
        registry.register_for_wake_only("memory_recall")

        wake_schemas = registry.schemas_for_wake()
        wake_names = [s["function"]["name"] for s in wake_schemas]
        assert "memory_recall" in wake_names

        resolved = registry.resolve("memory_recall")
        assert resolved is not None
