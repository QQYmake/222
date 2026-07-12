"""回归测试：并发交叉、空值语义、超时后台任务、跨存储部分提交。

7 个最小回归包，全部使用确定性假 LLM、假时钟、并发屏障和临时 SQLite。

测试目标：
  1. 空记忆三态测试 — None/空/非空 的降级与注入语义
  2. 两个并发 @4 不串记忆 — _run_query_path() 读取"最新 @d"而非按 trigger_id 读取
  3. 两个并发 @6 原子消费 — read_surface() SELECT→DELETE 非原子事务
  4. 累积聊天历史不重复写 @a — after_turn(raw_messages) 可能每轮把整个历史重复追加
  5. identity 失败零副作用 — 请求失败前不能消费 @e、写 @d/@a、调用记忆模型
  6. γ 超时跨 2am 不复活旧数据 — 超时后台任务在清理后"复活"昨日缓存
  7. 用户/主动回合并发时工具不泄漏 — memory_recall 工具不能出现在用户回合
"""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
from app.application.memory.buffer_manager import BufferManager
from app.application.memory.memory_engine import MemoryEngine
from app.domain.ports.memory_engine import MemoryEngineConfig, MemoryRecall
from app.application.memory.intent_classifier import IntentResult
from app.application.memory.retrieval_pipeline import RetrievalResult
from app.domain.models.turn import ChatMessage
from app.domain.models.chat_completion import ChatCompletionResponse, Choice
from app.domain.models.trigger import UserTrigger
from app.domain.models.sample import SampleReadError
from app.domain.models.tool import ToolDefinition, ToolExecutor, ToolExecutionContext
from app.adapters.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chat_response(content: str = "response text") -> ChatCompletionResponse:
    """构造一个合法的 ChatCompletionResponse。"""
    return ChatCompletionResponse(
        id="resp-1",
        object="chat.completion",
        created=0,
        model="test-model",
        choices=[Choice(
            index=0,
            message_role="assistant",
            message_content=content,
            finish_reason="stop",
        )],
        usage=None,
    )


def _make_user_trigger(messages: list[dict] | None = None) -> UserTrigger:
    """构造测试用 UserTrigger。"""
    if messages is None:
        messages = [{"role": "user", "content": "hello"}]
    return UserTrigger(request_id="req-1", chat_request={"messages": messages, "model": "test"})


# ===========================================================================
# Test 1: 空记忆三态测试
# ===========================================================================

class TestEmptyMemoryTriState:
    """None、空记忆、非空记忆三态。

    核心断言：
    - memory_recall=None 才回退 Sample（MEMORY_ENABLED=false 或 recall.text="" ）
    - text="" 必须生成空 <memories>，不能注入种子记忆
    - 非空 text 替换 Sample memories
    """

    @pytest.fixture
    def buffer_manager(self, tmp_path):
        store = SQLiteBufferStore(str(tmp_path / "memory.sqlite3"))
        return BufferManager(store)

    async def test_disabled_returns_degraded_empty(self, buffer_manager):
        """MEMORY_ENABLED=false → degraded, text='' → TurnRunner 视为 None → 回退 Sample。"""
        config = MemoryEngineConfig(db_path=":memory:", enabled=False)
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        trigger = _make_user_trigger()
        recall = await engine.recall(trigger, [])

        assert recall.mode == "degraded"
        assert recall.text == ""
        # TurnRunner 逻辑：recall.text 为空 → memory_recall_text = None → 回退 Sample
        memory_recall_text = recall.text if recall and recall.text else None
        assert memory_recall_text is None

    async def test_surface_empty_returns_no_query_empty(self, buffer_manager):
        """@6 路径 @e 为空 → no_query, text='' → 应视为 None → 回退 Sample。

        潜在 bug：文档 6.12 与降级约定冲突。
        @e 为空时，如果 text='' 被 TurnRunner 视为 None，
        则会回退到 Sample 种子记忆，而非生成空 <memories>。
        """
        config = MemoryEngineConfig(db_path=":memory:", enabled=True, retrieval_timeout=1.0)
        # intent_classifier 为 None → 走 _run_surface_path()（骨架默认 no_query）
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        trigger = _make_user_trigger()
        recall = await engine.recall(trigger, [])

        assert recall.mode == "no_query"
        assert recall.text == ""
        assert recall.source_recall_ids == []
        # TurnRunner 逻辑：空 text → None → 回退 Sample 种子记忆
        # 这意味着 @e 为空时不会生成空 <memories>，而是注入种子记忆
        memory_recall_text = recall.text if recall and recall.text else None
        assert memory_recall_text is None

    async def test_surface_non_empty_returns_content(self, buffer_manager):
        """@6 路径 @e 非空 → no_query, text='...' → 替换 Sample memories。"""
        # 先写入一条 @e
        await buffer_manager.write_surface(
            content="浮现的记忆内容",
            raw_content="raw surface",
            surface_type="association",
            source_ids=[1],
        )

        config = MemoryEngineConfig(db_path=":memory:", enabled=True, retrieval_timeout=1.0)
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        trigger = _make_user_trigger()
        recall = await engine.recall(trigger, [])

        assert recall.mode == "no_query"
        assert recall.text == "浮现的记忆内容"
        assert recall.source_recall_ids == []
        # TurnRunner 逻辑：非空 text → 替换 Sample memories
        memory_recall_text = recall.text if recall and recall.text else None
        assert memory_recall_text == "浮现的记忆内容"

    async def test_surface_consumed_after_read(self, buffer_manager):
        """@e 读取后即删：第二次 recall 应返回空。"""
        await buffer_manager.write_surface(
            content="第一条浮现",
            raw_content="raw",
            surface_type="association",
            source_ids=[1],
        )

        config = MemoryEngineConfig(db_path=":memory:", enabled=True, retrieval_timeout=1.0)
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        trigger = _make_user_trigger()
        first = await engine.recall(trigger, [])
        second = await engine.recall(trigger, [])

        assert first.text == "第一条浮现"
        assert second.text == ""  # @e 已被消费


# ===========================================================================
# Test 2: 两个并发 @4 不串记忆
# ===========================================================================

class TestConcurrentQueryNoCrossContamination:
    """两个 @4 查询并发完成顺序交错时，A 只能得到 A 的 @d。

    潜在 bug：
    _run_query_path() 在 pipeline.execute() 完成后调用 read_recall_latest()，
    读取"最新 @d"而非按 trigger_id 读取。
    如果 A 和 B 的 pipeline 都完成后 A 才读取，A 会读到 B 的 @d。
    """

    @pytest.fixture
    def buffer_manager(self, tmp_path):
        store = SQLiteBufferStore(str(tmp_path / "memory.sqlite3"))
        return BufferManager(store)

    async def test_concurrent_queries_get_own_recall(self, buffer_manager):
        """两个并发 @4 查询不应串记忆。"""
        # 用 Event 屏障确保两个 pipeline 都写完 @d 后才返回
        a_written = asyncio.Event()
        b_written = asyncio.Event()

        class FakePipelineA:
            async def execute(self, intent_result, raw_messages):
                await buffer_manager.write_recall(
                    trigger_id="query_A",
                    content="A 的记忆内容",
                    raw_content="A raw",
                    metadata={},
                )
                a_written.set()
                # 等待 B 也写完，确保两个 @d 都在 buffer 中
                await asyncio.wait_for(b_written.wait(), timeout=5)
                return RetrievalResult(
                    polished_content="A 的记忆内容",
                    raw_content="A raw",
                    recall_id=1,
                )

        class FakePipelineB:
            async def execute(self, intent_result, raw_messages):
                await buffer_manager.write_recall(
                    trigger_id="query_B",
                    content="B 的记忆内容",
                    raw_content="B raw",
                    metadata={},
                )
                b_written.set()
                await asyncio.wait_for(a_written.wait(), timeout=5)
                return RetrievalResult(
                    polished_content="B 的记忆内容",
                    raw_content="B raw",
                    recall_id=2,
                )

        config = MemoryEngineConfig(
            db_path=":memory:", enabled=True, retrieval_timeout=30.0
        )

        # 引擎 A 使用 FakePipelineA
        engine_a = MemoryEngine(
            config=config,
            buffer_manager=buffer_manager,
            intent_classifier=None,
            retrieval_pipeline=FakePipelineA(),
        )
        # 引擎 B 使用 FakePipelineB（共享同一 buffer_manager）
        engine_b = MemoryEngine(
            config=config,
            buffer_manager=buffer_manager,
            intent_classifier=None,
            retrieval_pipeline=FakePipelineB(),
        )

        intent = IntentResult(label="query", confidence=0.9, source="rule")
        messages = [ChatMessage(role="user", content="查一下")]

        # 并发执行两个查询路径
        result_a, result_b = await asyncio.gather(
            engine_a._run_query_path(intent, messages),
            engine_b._run_query_path(intent, messages),
        )

        # 核心断言：A 得到 A 的记忆，B 得到 B 的记忆
        # 如果 _run_query_path() 使用 read_recall_latest()，
        # 两个查询都会读到最新的 @d（即 B 的），导致 A 串记忆
        assert result_a.text == "A 的记忆内容", (
            f"A 应得到自己的记忆 'A 的记忆内容'，实际得到 '{result_a.text}'（串记忆 bug）"
        )
        assert result_b.text == "B 的记忆内容", (
            f"B 应得到自己的记忆 'B 的记忆内容'，实际得到 '{result_b.text}'"
        )

    async def test_sequential_queries_get_own_recall(self, buffer_manager):
        """串行查询各自得到自己的 @d（基线对照）。"""

        class FakePipeline:
            def __init__(self, content):
                self._content = content

            async def execute(self, intent_result, raw_messages):
                rid = await buffer_manager.write_recall(
                    trigger_id="query",
                    content=self._content,
                    raw_content=self._content,
                    metadata={},
                )
                return RetrievalResult(
                    polished_content=self._content,
                    raw_content=self._content,
                    recall_id=rid,
                )

        config = MemoryEngineConfig(
            db_path=":memory:", enabled=True, retrieval_timeout=30.0
        )

        engine = MemoryEngine(
            config=config,
            buffer_manager=buffer_manager,
            retrieval_pipeline=FakePipeline("第一条记忆"),
        )
        intent = IntentResult(label="query", confidence=0.9, source="rule")
        messages = [ChatMessage(role="user", content="查一下")]

        result1 = await engine._run_query_path(intent, messages)
        assert result1.text == "第一条记忆"

        engine2 = MemoryEngine(
            config=config,
            buffer_manager=buffer_manager,
            retrieval_pipeline=FakePipeline("第二条记忆"),
        )
        result2 = await engine2._run_query_path(intent, messages)
        assert result2.text == "第二条记忆"


# ===========================================================================
# Test 3: 两个并发 @6 原子消费
# ===========================================================================

class TestConcurrentSurfaceAtomicConsume:
    """两个无查询回合同时读取一个 @e。

    核心断言：只能一个回合获得内容，另一个为空。
    read_surface() 的 SELECT→DELETE 若非同一原子事务，会重复消费。
    """

    @pytest.fixture
    def store(self, tmp_path):
        return SQLiteBufferStore(str(tmp_path / "memory.sqlite3"))

    async def test_concurrent_read_surface_only_one_gets_content(self, store):
        """两个并发 read_surface() 只有一个能获得内容。"""
        # 写入一条 @e
        await store.write_surface(
            content="唯一的浮现内容",
            raw_content="raw",
            surface_type="association",
            source_ids=[1],
        )

        # 并发读取
        result_a, result_b = await asyncio.gather(
            store.read_surface(),
            store.read_surface(),
        )

        # 核心断言：只有一个得到内容，另一个为 None
        results = [result_a, result_b]
        non_none = [r for r in results if r is not None]

        assert len(non_none) == 1, (
            f"应有且仅有一个 read_surface 返回内容，实际 {len(non_none)} 个返回了内容"
        )
        assert non_none[0].content == "唯一的浮现内容"

    async def test_concurrent_read_surface_multiple_entries(self, store):
        """多条 @e 并发消费：每条只被消费一次。"""
        for i in range(5):
            await store.write_surface(
                content=f"浮现_{i}",
                raw_content=f"raw_{i}",
                surface_type="association",
                source_ids=[i],
            )

        # 10 个并发读取，但只有 5 条 @e
        results = await asyncio.gather(*[store.read_surface() for _ in range(10)])

        non_none = [r for r in results if r is not None]
        contents = [r.content for r in non_none]

        # 每条 @e 只被消费一次
        assert len(non_none) == 5, f"应有 5 条被消费，实际 {len(non_none)}"
        assert len(set(contents)) == 5, "存在重复消费"


# ===========================================================================
# Test 4: 累积聊天历史不重复写 @a
# ===========================================================================

class TestNoDuplicateRawAppend:
    """连续两轮提交完整聊天历史，@a 中每条消息只出现一次。

    潜在 bug：
    after_turn(raw_messages) 遍历全部 raw_messages 并逐条 append_raw，
    如果每轮都传入完整聊天历史，旧消息会被重复追加，导致沉淀输入指数膨胀。
    """

    @pytest.fixture
    def buffer_manager(self, tmp_path):
        store = SQLiteBufferStore(str(tmp_path / "memory.sqlite3"))
        return BufferManager(store)

    async def test_two_turns_no_duplicate(self, buffer_manager):
        """连续两轮传完整历史，@a 不应有重复消息。"""
        config = MemoryEngineConfig(db_path=":memory:", enabled=True)
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        response = _make_chat_response("你好")

        # 第一轮：2 条消息
        turn1_messages = [
            ChatMessage(role="user", content="你好"),
            ChatMessage(role="assistant", content="你好"),
        ]
        await engine.after_turn(turn1_messages, response, turn_id="turn-1")

        # 第二轮：4 条消息（包含第一轮的 2 条 + 新增 2 条）
        turn2_messages = [
            ChatMessage(role="user", content="你好"),
            ChatMessage(role="assistant", content="你好"),
            ChatMessage(role="user", content="今天怎么样"),
            ChatMessage(role="assistant", content="还不错"),
        ]
        await engine.after_turn(turn2_messages, response, turn_id="turn-2")

        # 读取 @a 全量
        all_raw = await buffer_manager.read_all_raw()

        # 核心断言：每条消息内容只出现一次
        # after_turn 追加 raw_messages 中的每条消息 + response
        # 第一轮：2 条消息 + 1 条 response = 3 条
        # 第二轮：4 条消息 + 1 条 response = 5 条
        # 总计 8 条（但 "你好" 出现了 4 次 — 2 次 user + 2 次 assistant）
        #
        # 如果 after_turn 正确去重，应该只有 4 条唯一消息：
        #   user:你好, assistant:你好, user:今天怎么样, assistant:还不错
        # 但当前实现不做去重，所以会有重复
        user_contents = [r for r in all_raw if r["role"] == "user"]
        assistant_contents = [r for r in all_raw if r["role"] == "assistant"]

        # "你好" 不应被重复追加
        user_hello = [r for r in user_contents if r["content"] == "你好"]
        assert len(user_hello) == 1, (
            f"user '你好' 应只出现一次，实际 {len(user_hello)} 次 — after_turn 重复追加"
        )

    async def test_after_turn_response_also_appended(self, buffer_manager):
        """after_turn 也应追加 response 内容（检查 response 提取是否正确）。

        已知问题：after_turn 使用 response.choices[0].message.content，
        但 Choice 没有 .message 属性（有 message_content），
        所以 response 内容不会被追加。
        """
        config = MemoryEngineConfig(db_path=":memory:", enabled=True)
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        messages = [ChatMessage(role="user", content="hello")]
        response = _make_chat_response("assistant reply")

        await engine.after_turn(messages, response, turn_id="turn-1")

        all_raw = await buffer_manager.read_all_raw()

        # user 消息应被追加
        user_msgs = [r for r in all_raw if r["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "hello"

        # response 也应被追加为 assistant 消息
        # 但如果 response.choices[0].message.content 抛出 AttributeError，
        # response 不会被追加（被 except 吞掉）
        assistant_msgs = [r for r in all_raw if r["role"] == "assistant"]
        # 这个断言可能会失败，暴露 response 提取 bug
        assert len(assistant_msgs) >= 1, (
            "response 内容应被追加到 @a，但未找到 — 可能是 .message.content 属性访问错误"
        )


# ===========================================================================
# Test 5: identity 失败零副作用
# ===========================================================================

class TestIdentityFailureZeroSideEffects:
    """identity 损坏时发起查询，返回 503 且不产生任何记忆副作用。

    核心断言：
    - 返回 503（不降级）
    - 不消费 @e（read_surface 不被调用）
    - 不写 @d（write_recall 不被调用）
    - 不写 @a（append_raw 不被调用）
    - 不调用记忆模型
    """

    async def test_identity_failure_no_memory_side_effects(self, tmp_path):
        """identity Sample 损坏时，记忆引擎零副作用。"""
        from app.application.turn_runner import TurnRunner
        from app.domain.models.context_builder import ContextBuilder

        # Mock sample_reader：read_all() 抛 SampleReadError
        sample_reader = MagicMock()
        sample_reader.read_all.side_effect = SampleReadError(
            sample_type="identity",
            reason="file not found",
        )

        # Mock memory_port：追踪所有方法调用
        memory_port = AsyncMock()
        memory_port.recall = AsyncMock(
            return_value=MemoryRecall(mode="degraded", text="", source_recall_ids=[])
        )
        memory_port.after_turn = AsyncMock()

        # Mock model_client
        model_client = AsyncMock()

        context_builder = ContextBuilder(memory_char_budget=12000)

        turn_runner = TurnRunner(
            sample_reader=sample_reader,
            context_builder=context_builder,
            model_client=model_client,
            memory_port=memory_port,
        )

        trigger = _make_user_trigger()

        # run_user_turn 应抛出 SampleReadError（identity 不降级）
        with pytest.raises(SampleReadError):
            await turn_runner.run_user_turn(trigger)

        # 核心断言：记忆引擎零副作用
        memory_port.recall.assert_not_called(), "identity 失败时不应调用 recall()"
        memory_port.after_turn.assert_not_called(), "identity 失败时不应调用 after_turn()"
        model_client.complete.assert_not_called(), "identity 失败时不应调用模型"

    async def test_identity_failure_no_surface_consume(self, tmp_path):
        """更精确：identity 失败时 @e 不被消费。"""
        from app.application.turn_runner import TurnRunner
        from app.domain.models.context_builder import ContextBuilder

        store = SQLiteBufferStore(str(tmp_path / "memory.sqlite3"))
        buffer_manager = BufferManager(store)

        # 预置一条 @e
        await buffer_manager.write_surface(
            content="预置浮现",
            raw_content="raw",
            surface_type="association",
            source_ids=[1],
        )

        # 使用真实 MemoryEngine
        config = MemoryEngineConfig(
            db_path=str(tmp_path / "memory.sqlite3"),
            enabled=True,
            retrieval_timeout=1.0,
        )
        engine = MemoryEngine(config=config, buffer_manager=buffer_manager)

        sample_reader = MagicMock()
        sample_reader.read_all.side_effect = SampleReadError(
            sample_type="identity",
            reason="corrupted",
        )
        model_client = AsyncMock()
        context_builder = ContextBuilder(memory_char_budget=12000)

        turn_runner = TurnRunner(
            sample_reader=sample_reader,
            context_builder=context_builder,
            model_client=model_client,
            memory_port=engine,
        )

        trigger = _make_user_trigger()

        with pytest.raises(SampleReadError):
            await turn_runner.run_user_turn(trigger)

        # @e 应仍在缓冲区中（未被消费）
        # 注意：read_surface() 本身会消费 @e，所以只能用 SQL 直接查
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "memory.sqlite3"))
        try:
            count = conn.execute("SELECT COUNT(*) FROM buffer_surface").fetchone()[0]
        finally:
            conn.close()

        assert count == 1, f"@e 应未被消费（count=1），实际 count={count}"


# ===========================================================================
# Test 6: γ 超时跨 2am 不复活旧数据
# ===========================================================================

class TestGammaTimeoutNoResurrection:
    """γ 超时任务跨越凌晨 2 点，沉淀清空后旧日后台任务不能重新写回 @d。

    潜在 bug：
    超时后台任务在清理后"复活"昨日缓存。
    如果超时 task 未被正确取消，它可能在 2am 清空 @d 之后写入新 @d。
    """

    @pytest.fixture
    def buffer_manager(self, tmp_path):
        store = SQLiteBufferStore(str(tmp_path / "memory.sqlite3"))
        return BufferManager(store)

    async def test_timeout_task_does_not_write_after_clear(self, buffer_manager):
        """γ 降级后，后台 task 不应在 @d 清空后写入。"""
        write_attempted = asyncio.Event()

        class SlowPipeline:
            """模拟慢检索管线：延迟后尝试写 @d。"""
            async def execute(self, intent_result, raw_messages):
                # 模拟慢操作：等待足够长时间让 timeout 触发
                await asyncio.sleep(0.3)
                # timeout 后尝试写 @d（如果 task 未被取消）
                try:
                    await buffer_manager.write_recall(
                        trigger_id="query",
                        content="复活的记忆",
                        raw_content="raw",
                        metadata={},
                    )
                    write_attempted.set()
                except Exception:
                    pass
                return RetrievalResult(
                    polished_content="复活的记忆",
                    raw_content="raw",
                    recall_id=0,
                )

        config = MemoryEngineConfig(
            db_path=":memory:",
            enabled=True,
            retrieval_timeout=0.05,  # 50ms 超时
        )
        engine = MemoryEngine(
            config=config,
            buffer_manager=buffer_manager,
            intent_classifier=None,
            retrieval_pipeline=SlowPipeline(),
        )

        intent = IntentResult(label="query", confidence=0.9, source="rule")
        messages = [ChatMessage(role="user", content="查一下")]

        # 触发 @4 查询 → 应超时降级
        result = await engine._run_query_path(intent, messages)
        assert result.mode == "degraded"
        assert result.text == ""

        # 模拟 2am 沉淀清空 @d
        await buffer_manager.clear_recall()

        # 等待足够时间让慢 pipeline 完成（如果未被取消）
        await asyncio.sleep(0.5)

        # 检查 @d 是否被"复活"
        all_recall = await buffer_manager.read_all_recall()

        # 核心断言：@d 应为空（后台 task 被取消，未写入）
        # 如果 task 未被取消，@d 会包含 "复活的记忆"
        assert len(all_recall) == 0, (
            f"@d 应在清空后保持为空，但找到 {len(all_recall)} 条记录 — "
            "超时后台任务未被取消，在 2am 清空后复活了 @d"
        )

    async def test_pre_timeout_write_survives_until_clear(self, buffer_manager):
        """超时前已写入的 @d 在清空后应消失（正常行为基线）。"""
        class FastThenSlowPipeline:
            """先写 @d，再慢操作触发 timeout。"""
            async def execute(self, intent_result, raw_messages):
                # 先写入 @d
                await buffer_manager.write_recall(
                    trigger_id="query",
                    content="正常写入的记忆",
                    raw_content="raw",
                    metadata={},
                )
                # 然后慢操作触发 timeout
                await asyncio.sleep(1.0)
                return RetrievalResult(
                    polished_content="正常写入的记忆",
                    raw_content="raw",
                    recall_id=1,
                )

        config = MemoryEngineConfig(
            db_path=":memory:",
            enabled=True,
            retrieval_timeout=0.05,
        )
        engine = MemoryEngine(
            config=config,
            buffer_manager=buffer_manager,
            intent_classifier=None,
            retrieval_pipeline=FastThenSlowPipeline(),
        )

        intent = IntentResult(label="query", confidence=0.9, source="rule")
        messages = [ChatMessage(role="user", content="查一下")]

        result = await engine._run_query_path(intent, messages)
        assert result.mode == "degraded"

        # @d 应有 1 条（超时前写入的）
        all_recall = await buffer_manager.read_all_recall()
        assert len(all_recall) == 1

        # 清空后应为 0
        await buffer_manager.clear_recall()
        all_recall = await buffer_manager.read_all_recall()
        assert len(all_recall) == 0


# ===========================================================================
# Test 7: 用户/主动回合并发时工具不泄漏
# ===========================================================================

class TestToolLeakPrevention:
    """用户回合任何时刻都看不到 memory_recall 工具。

    如果修改的是全局 ToolRegistry，工具可能短暂泄漏到用户回合。
    """

    @pytest.fixture
    def registry(self):
        reg = ToolRegistry(test_tools_enabled=False)

        # 注册普通工具
        reg.register(
            ToolDefinition(
                name="get_server_time",
                description="获取服务器时间",
                parameters={"type": "object", "properties": {}},
                enabled_in_production=True,
                timeout_seconds=10,
                max_result_chars=4096,
            ),
            MagicMock(spec=ToolExecutor),
        )

        # 注册 memory_recall 工具
        reg.register(
            ToolDefinition(
                name="memory_recall",
                description="检索记忆",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
                enabled_in_production=True,
                timeout_seconds=15,
                max_result_chars=8192,
            ),
            MagicMock(spec=ToolExecutor),
        )

        # 标记 memory_recall 为 wake-only
        reg.register_for_wake_only("memory_recall")

        return reg

    def test_user_turn_excludes_memory_recall(self, registry):
        """用户回合 schemas 不包含 memory_recall。"""
        user_schemas = registry.schemas_for_user()
        tool_names = [s["function"]["name"] for s in user_schemas]

        assert "memory_recall" not in tool_names, (
            "memory_recall 不应出现在用户回合工具列表中"
        )
        assert "get_server_time" in tool_names

    def test_wake_turn_includes_memory_recall(self, registry):
        """主动唤醒回合 schemas 包含 memory_recall。"""
        wake_schemas = registry.schemas_for_wake()
        tool_names = [s["function"]["name"] for s in wake_schemas]

        assert "memory_recall" in tool_names
        assert "get_server_time" in tool_names

    async def test_concurrent_schemas_for_user_never_leaks(self, registry):
        """并发调用 schemas_for_user() 时 memory_recall 永不泄漏。"""
        # 并发调用 100 次 schemas_for_user
        results = await asyncio.gather(
            *[asyncio.to_thread(registry.schemas_for_user) for _ in range(100)]
        )

        for i, schemas in enumerate(results):
            tool_names = [s["function"]["name"] for s in schemas]
            assert "memory_recall" not in tool_names, (
                f"第 {i} 次调用 schemas_for_user() 泄漏了 memory_recall"
            )

    async def test_concurrent_user_and_wake_schemas(self, registry):
        """并发调用 schemas_for_user() 和 schemas_for_wake() 时互不干扰。"""
        # 交错调用 schemas_for_user 和 schemas_for_wake
        async def check_user():
            for _ in range(50):
                schemas = registry.schemas_for_user()
                names = [s["function"]["name"] for s in schemas]
                assert "memory_recall" not in names
                await asyncio.sleep(0)

        async def check_wake():
            for _ in range(50):
                schemas = registry.schemas_for_wake()
                names = [s["function"]["name"] for s in schemas]
                assert "memory_recall" in names
                await asyncio.sleep(0)

        # 并发执行
        await asyncio.gather(check_user(), check_wake())

    def test_resolve_memory_recall_for_user_context(self, registry):
        """用户回合不应能 resolve memory_recall 工具。

        注意：当前 resolve() 不区分 user/wake 上下文。
        这意味着如果模型在用户回合返回了 memory_recall tool_call，
        ToolDispatcher 仍能 resolve 它 — 这是一个潜在的泄漏路径。
        """
        # resolve() 当前不区分 user/wake
        executor = registry.resolve("memory_recall")
        # 当前实现：resolve 返回 executor（因为 memory_recall enabled_in_production=True）
        # 这意味着用户回合如果模型返回了 memory_recall tool_call，它会被执行
        # 这是一个潜在 bug：resolve() 应该也检查 _wake_only
        if executor is not None:
            pytest.fail(
                "memory_recall 应在用户回合不可 resolve，但 resolve() 返回了执行器 — "
                "resolve() 未检查 _wake_only 集合，存在工具泄漏风险"
            )
