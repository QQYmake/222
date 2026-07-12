"""MemoryEngine：记忆引擎编排器。

实现 MemoryPort，编排 @0—@7 完整流程。
数据合同来源：V3 架构文档 6.2 MemoryEngine。

M3 阶段为骨架实现：
- recall() 支持 no_query 路径和 MEMORY_ENABLED=false 降级
- after_turn() 追加 @a 原料
- recall_as_tool() 在 M5 实现完整 @4 路径前抛 NotImplementedError
- start/stop_background_tasks() 在 M6/M8 接入周期生成器和沉淀管线
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.domain.ports.memory_engine import MemoryPort, MemoryRecall, MemoryEngineConfig

if TYPE_CHECKING:
    from app.domain.models.turn import ChatMessage
    from app.domain.models.trigger import TurnTrigger
    from app.domain.models.chat_completion import ChatCompletionResponse
    from app.application.memory.buffer_manager import BufferManager

logger = logging.getLogger(__name__)


class MemoryEngine(MemoryPort):
    """记忆引擎编排器。

    依赖注入：
    - buffer_manager: BufferManager（缓冲区读写）
    - intent_classifier: IntentClassifier（意图分类，M4 实现）
    - llm_bridge: LLMBridge（记忆 LLM 调用）
    - retrieval_pipeline: RetrievalPipeline（多轨检索，M5 实现）
    - surface_generator: SurfaceGenerator（@e 生成，M6 实现）
    - consolidation_pipeline: ConsolidationPipeline（沉淀管线，M8 实现）
    """

    def __init__(
        self,
        config: MemoryEngineConfig,
        buffer_manager: "BufferManager",
        intent_classifier: Any | None = None,
        llm_bridge: Any | None = None,
        retrieval_pipeline: Any | None = None,
        surface_generator: Any | None = None,
        consolidation_pipeline: Any | None = None,
    ) -> None:
        self._config = config
        self._buffer = buffer_manager
        self._intent_classifier = intent_classifier
        self._llm_bridge = llm_bridge
        self._retrieval_pipeline = retrieval_pipeline
        self._surface_generator = surface_generator
        self._consolidation_pipeline = consolidation_pipeline
        self._background_tasks: list[asyncio.Task] = []
        self._running = False

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    async def recall(
        self, trigger: "TurnTrigger", raw_messages: list["ChatMessage"]
    ) -> MemoryRecall:
        """回合开始前调用。返回记忆注入内容。"""
        if not self._config.enabled:
            logger.debug("memory_recall_skipped: MEMORY_ENABLED=false")
            return MemoryRecall(mode="degraded", text="", source_recall_ids=[])

        # 检查 X-Memory-Mode: new_window（通过 trigger 的 metadata 传递）
        if hasattr(trigger, "metadata") and trigger.metadata:
            memory_mode = trigger.metadata.get("x-memory-mode", "")
            if memory_mode == "new_window":
                return await self._run_new_window_path()

        # 意图分类（M4 前 intent_classifier 可能是 mock）
        if self._intent_classifier is None:
            # 骨架阶段默认 no_query
            return await self._run_surface_path()

        intent_result = await self._intent_classifier.classify(
            raw_messages[-1].content if raw_messages else ""
        )

        if intent_result.label == "query":
            return await self._run_query_path(intent_result, raw_messages)
        else:
            return await self._run_surface_path()

    async def _run_query_path(
        self, intent_result: Any, raw_messages: list["ChatMessage"]
    ) -> MemoryRecall:
        """@4 查询路径。M5 完整实现。"""
        if self._retrieval_pipeline is None:
            # 骨架阶段降级
            logger.debug("query_path_not_available: retrieval_pipeline not set")
            return MemoryRecall(mode="degraded", text="", source_recall_ids=[])

        # M5 实现：asyncio.create_task + timeout + 降级 γ
        try:
            task = asyncio.create_task(
                self._retrieval_pipeline.execute(intent_result, raw_messages)
            )
            await asyncio.wait_for(task, timeout=self._config.retrieval_timeout)
            entry = await self._buffer.read_recall_latest()
            if entry:
                return MemoryRecall(
                    mode="query",
                    text=entry.content,
                    source_recall_ids=[entry.id],
                )
            return MemoryRecall(mode="query", text="", source_recall_ids=[])
        except asyncio.TimeoutError:
            logger.warning(
                "memory_recall_timeout: retrieval exceeded %.1fs",
                self._config.retrieval_timeout,
            )
            # γ 降级：后台 task 继续运行，不阻塞主 LLM
            return MemoryRecall(mode="degraded", text="", source_recall_ids=[])

    async def _run_surface_path(self) -> MemoryRecall:
        """@6 无查询路径。读取 @e 内容。"""
        surface = await self._buffer.read_surface()
        if surface:
            return MemoryRecall(
                mode="no_query",
                text=surface.content,
                source_recall_ids=[],
            )
        return MemoryRecall(mode="no_query", text="", source_recall_ids=[])

    async def _run_new_window_path(self) -> MemoryRecall:
        """新窗口衔接路径。读取最近 15 条 @d。"""
        entries = await self._buffer.read_recent_recall(15)
        if entries:
            text = "\n---\n".join(e.content for e in entries if e.content)
            return MemoryRecall(
                mode="new_window",
                text=text,
                source_recall_ids=[e.id for e in entries],
            )
        return MemoryRecall(mode="new_window", text="", source_recall_ids=[])

    async def after_turn(
        self,
        raw_messages: list["ChatMessage"],
        response: "ChatCompletionResponse",
        turn_id: str,
        trigger: "TurnTrigger | None" = None,
    ) -> None:
        """回合结束后调用。追加 @a 原料。"""
        if not self._config.enabled:
            return

        platform = getattr(trigger, "platform", "unknown") if trigger else "unknown"

        # 追加用户消息
        for msg in raw_messages:
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", "")
            if content:
                await self._buffer.append_raw(role, content, platform, turn_id)

        # 追加响应
        try:
            response_content = response.choices[0].message.content
            if response_content:
                await self._buffer.append_raw("assistant", response_content, platform, turn_id)
        except (AttributeError, IndexError, TypeError):
            logger.warning("after_turn: could not extract response content")

    async def recall_new_window(self) -> MemoryRecall:
        """新窗口衔接路径。读取最近 15 条 @d，拼接为 MemoryRecall。"""
        return await self._run_new_window_path()

    async def recall_as_tool(self, query: str, turn_id: str = "") -> str:
        """memory_recall 工具调用入口。触发 @4 流程，返回润色后的 @d 内容。"""
        if not self._config.enabled:
            return ""

        if self._retrieval_pipeline is None:
            raise NotImplementedError("recall_as_tool requires retrieval_pipeline (M5)")

        # 构造伪 IntentResult 和 messages
        from app.application.memory.intent_classifier import IntentResult
        from unittest.mock import MagicMock

        intent = IntentResult(label="query", confidence=1.0, source="tool")
        messages = [MagicMock(content=query)]

        try:
            task = asyncio.create_task(
                self._retrieval_pipeline.execute(intent, messages)
            )
            await asyncio.wait_for(task, timeout=self._config.retrieval_timeout)
            entry = await self._buffer.read_recall_latest()
            if entry:
                return entry.content
            return ""
        except asyncio.TimeoutError:
            logger.warning(
                "memory_recall_tool_timeout: %.1fs", self._config.retrieval_timeout
            )
            return ""

    async def start_background_tasks(self) -> None:
        """启动 @e 周期生成器和 2am 沉淀定时器。"""
        if self._running:
            return
        self._running = True
        # M6/M8 接入实际任务
        logger.info("memory_engine_background_tasks_started")

    async def stop_background_tasks(self) -> None:
        """停止后台任务。"""
        self._running = False
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        logger.info("memory_engine_background_tasks_stopped")
