"""TurnRunner: 唯一回合编排器。

数据合同来源：架构文档 6.6 TurnRunner v2。

职责：只安排顺序——读取 Sample → 构造上下文 → 调用模型 → 返回或写入 Outbox。
不知道 Sample 存在文件中，不知道 Outbox 存在 SQLite 中。

v2 变更：
  1. 新增 async run_user_turn() 使用 AsyncModelClient
  2. 新增 async run_wake_turn() 用于主动回合（M4+）
  3. 保留 v1 sync run() 兼容
  4. 每个回合创建独立 TurnContext
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.domain.ports.sample_reader import SampleReader, AllSamples
from app.domain.ports.model_client import ModelClient, AsyncModelClient
from app.domain.models.context_builder import ContextBuilder
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.turn import PreparedTurn, ModelCompletionInput, TurnContext
from app.domain.models.chat_completion import (
    ChatCompletionResponse,
    to_internal_max_output_tokens,
)
from app.domain.models.sample import SampleReadError
from app.infrastructure.logging import get_logger

import dataclasses


@dataclasses.dataclass(frozen=True)
class ActiveTurnResult:
    """主动回合结果。

    数据合同来源：架构文档 5.8 ActiveTurnResult。
    """
    trigger_id: str
    outcome: str  # "message_enqueued" | "no_message" | "failed" | "expired"
    event_id: Optional[str] = None
    error_code: Optional[str] = None


class TurnRunner:
    """唯一回合编排器。

    被动回合返回 ChatCompletionResponse。
    主动回合返回 ActiveTurnResult。
    """

    def __init__(
        self,
        sample_reader: SampleReader,
        context_builder: ContextBuilder,
        model_client: ModelClient | AsyncModelClient,
        outbox_store=None,
        tool_registry=None,
        model_tool_loop=None,
        memory_port=None,
    ):
        self._sample_reader = sample_reader
        self._context_builder = context_builder
        self._model_client = model_client
        self._outbox_store = outbox_store
        self._tool_registry = tool_registry
        self._model_tool_loop = model_tool_loop
        self._memory_port = memory_port
        self._logger = get_logger("turn_runner")

    @property
    def tool_registry(self):
        """暴露 tool_registry 供测试断言。"""
        return self._tool_registry

    @property
    def model_tool_loop(self):
        """暴露 model_tool_loop 供测试断言。"""
        return self._model_tool_loop

    @property
    def memory_port(self):
        """暴露 memory_port 供测试断言。"""
        return self._memory_port

    def run(self, trigger: UserTrigger | TimerTrigger):
        """执行一个回合（v1 同步兼容）。

        指令:
          1. 记录 started_at
          2. 读取四份 Sample
          3. 构造上下文
          4. 调用上游模型
          5. 被动回合返回模型响应
          6. 主动回合把有效消息写入 Outbox (M4 实现)
        """
        started_at = datetime.now(timezone.utc)

        # 1. 读取 Sample
        samples = self._sample_reader.read_all()

        # 2. 构造上下文
        prepared = self._context_builder.build(samples, trigger)

        # 3. 调用模型
        response = self._model_client.complete(
            ModelCompletionInput(
                messages=prepared.messages,
                temperature=self._choose_temperature(trigger),
                max_output_tokens=self._choose_max_output_tokens(trigger),
            )
        )

        # 4. 被动回合
        if trigger.type == "user":
            self._log_turn(trigger, prepared.sample_versions, response, started_at)
            return response

        # 5. 主动回合
        return self._handle_active_turn(trigger, response, prepared)

    async def run_user_turn(self, trigger: UserTrigger) -> ChatCompletionResponse:
        """异步执行用户回合（v2）。

        数据合同来源：架构文档 6.6 TurnRunner v2。

        指令:
          1. 创建独立 TurnContext
          2. 读取四份 Sample
          3. ContextBuilder 生成初始 messages
          4. 调用 AsyncModelClient（等待期间让出 HTTP 执行权）
          5. 用户回合返回最终 OpenAI Chat 响应
          6. 销毁 TurnContext

        并发规则:
          - TurnRunner 本身不持有全局可变 messages
          - 用户回合不获取 ActiveTurnGate
        """
        started_at = datetime.now(timezone.utc)
        turn_id = str(uuid.uuid4())

        self._logger.info("turn_started", extra={
            "turn_id": turn_id,
            "trigger_type": "user",
            "request_id": trigger.request_id,
        })

        # 1. 读取 Sample
        samples = self._sample_reader.read_all()

        # 1b. 记忆引擎 recall（如果启用）
        memory_recall_text = None
        if self._memory_port is not None:
            try:
                raw_messages = trigger.chat_request.get("messages", [])
                recall = await self._memory_port.recall(trigger, raw_messages)
                memory_recall_text = recall.text if recall and recall.text else None
                self._logger.info("memory_recall_completed", extra={
                    "turn_id": turn_id,
                    "request_id": trigger.request_id,
                    "recall_mode": recall.mode if recall else None,
                    "recall_degraded": recall.degraded if recall else False,
                })
            except Exception as e:
                self._logger.warning("memory_recall_failed", extra={
                    "turn_id": turn_id,
                    "request_id": trigger.request_id,
                    "error": str(e),
                })

        # 2. 构造上下文
        prepared = self._context_builder.build(
            samples, trigger, memory_recall_text=memory_recall_text
        )

        # 3. 调用异步模型
        response = await self._model_client.complete(
            ModelCompletionInput(
                messages=prepared.messages,
                temperature=self._choose_temperature(trigger),
                max_output_tokens=self._choose_max_output_tokens(trigger),
            )
        )

        # 4. 日志
        self._log_turn_async(trigger, prepared.sample_versions, response, started_at, turn_id)

        # 4b. 记忆引擎 after_turn（如果启用）
        if self._memory_port is not None:
            try:
                await self._memory_port.after_turn(trigger, response)
            except Exception as e:
                self._logger.warning("memory_after_turn_failed", extra={
                    "turn_id": turn_id,
                    "request_id": trigger.request_id,
                    "error": str(e),
                })

        return response

    async def run_wake_turn(self, trigger) -> ActiveTurnResult:
        """异步执行主动唤醒回合（v2，M4+ 完整实现）。

        数据合同来源：架构文档 6.6 TurnRunner v2 + 7.2 主动唤醒回合。

        指令:
          1. 创建独立 TurnContext
          2. 读取四份 Sample
          3. ContextBuilder 生成初始 messages
          4. 调用 AsyncModelClient
          5. <NO_MESSAGE> 则不写 Outbox
          6. 普通文本：enqueue_once，提交成功后通知长轮询
          7. 销毁 TurnContext

        并发规则:
          - WakeController 在调用前负责 ActiveTurnGate
          - 主动回合不获取用户回合锁
        """
        started_at = datetime.now(timezone.utc)
        turn_id = str(uuid.uuid4())

        self._logger.info("turn_started", extra={
            "turn_id": turn_id,
            "trigger_type": "wake",
            "wake_id": getattr(trigger, "trigger_id", None),
        })

        # 1. 读取 Sample
        samples = self._sample_reader.read_all()

        # 2. 构造上下文
        prepared = self._context_builder.build(samples, trigger)

        # 3. 调用异步模型
        try:
            response = await self._model_client.complete(
                ModelCompletionInput(
                    messages=prepared.messages,
                    temperature=None,
                    max_output_tokens=None,
                )
            )
        except Exception as e:
            self._logger.error("wake_turn_model_failed", extra={
                "turn_id": turn_id,
                "wake_id": trigger.trigger_id,
                "error": str(e),
            })
            return ActiveTurnResult(
                trigger_id=trigger.trigger_id,
                outcome="failed",
                error_code="model_error",
            )

        # 4. 处理主动回合结果
        return await self._handle_active_turn_async(trigger, response, prepared, turn_id)

    def _choose_temperature(self, trigger: UserTrigger | TimerTrigger) -> Optional[float]:
        """被动回合使用前端 temperature；主动回合用 None。"""
        if trigger.type == "user":
            return trigger.chat_request.get("temperature")
        return None

    def _choose_max_output_tokens(self, trigger: UserTrigger | TimerTrigger) -> Optional[int]:
        """被动回合使用前端 max_output_tokens；主动回合用 None。"""
        if trigger.type == "user":
            from app.domain.models.chat_completion import parse_chat_request, to_internal_max_output_tokens
            # chat_request is already a raw dict; extract max_output_tokens
            max_ct = trigger.chat_request.get("max_completion_tokens")
            max_tk = trigger.chat_request.get("max_tokens")
            if max_ct is not None:
                return max_ct
            return max_tk
        return None

    def _handle_active_turn(self, trigger: TimerTrigger, response, prepared: PreparedTurn):
        """主动回合处理（v1 同步）。"""
        if self._outbox_store is None:
            raise RuntimeError(
                "outbox_store is required for active turns but was not provided"
            )

        content = response.first_assistant_text().strip()

        if content == "<NO_MESSAGE>":
            self._logger.info("active_turn_no_message",
                              extra={"trigger_id": trigger.trigger_id})
            return ActiveTurnResult(
                trigger_id=trigger.trigger_id,
                outcome="no_message",
            )

        from app.domain.models.outbox import NewOutboxMessage

        message = NewOutboxMessage(
            event_id=str(uuid.uuid4()),
            trigger_id=trigger.trigger_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            content=content,
            metadata={
                "model": response.model,
                "sample_versions": prepared.sample_versions,
                "upstream_response_id": response.id,
            },
        )

        saved = self._outbox_store.enqueue_once(message)
        self._logger.info("active_turn_enqueued",
                          extra={"trigger_id": trigger.trigger_id, "event_id": saved.event_id})

        return ActiveTurnResult(
            trigger_id=trigger.trigger_id,
            outcome="message_enqueued",
            event_id=saved.event_id,
        )

    async def _handle_active_turn_async(self, trigger, response, prepared, turn_id: str) -> ActiveTurnResult:
        """主动回合处理（v2 异步）。

        指令:
          1. <NO_MESSAGE> → outcome="no_message", 不写 Outbox
          2. 正常文本 → enqueue_once → outcome="message_enqueued"
          3. 写入失败 → outcome="failed"
          4. M6 将在 commit 后调用 Notifier
        """
        if self._outbox_store is None:
            raise RuntimeError(
                "outbox_store is required for active turns but was not provided"
            )

        content = response.first_assistant_text().strip()

        if content == "<NO_MESSAGE>":
            self._logger.info("active_turn_no_message", extra={
                "turn_id": turn_id,
                "trigger_id": trigger.trigger_id,
            })
            return ActiveTurnResult(
                trigger_id=trigger.trigger_id,
                outcome="no_message",
            )

        from app.domain.models.outbox import NewOutboxMessage

        message = NewOutboxMessage(
            event_id=str(uuid.uuid4()),
            trigger_id=trigger.trigger_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            content=content,
            metadata={
                "model": response.model,
                "sample_versions": prepared.sample_versions,
                "upstream_response_id": response.id,
                "turn_id": turn_id,
            },
        )

        try:
            saved = self._outbox_store.enqueue_once(message)
        except Exception as e:
            self._logger.error("active_turn_enqueue_failed", extra={
                "turn_id": turn_id,
                "trigger_id": trigger.trigger_id,
                "error": str(e),
            })
            return ActiveTurnResult(
                trigger_id=trigger.trigger_id,
                outcome="failed",
                error_code="outbox_write_failed",
            )

        self._logger.info("active_turn_enqueued", extra={
            "turn_id": turn_id,
            "trigger_id": trigger.trigger_id,
            "event_id": saved.event_id,
        })

        return ActiveTurnResult(
            trigger_id=trigger.trigger_id,
            outcome="message_enqueued",
            event_id=saved.event_id,
        )

    def _log_turn(self, trigger, sample_versions, response, started_at):
        """记录回合日志（结构化）。"""
        duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
        self._logger.info(
            "turn_completed",
            extra={
                "trigger_type": trigger.type,
                "correlation_id": getattr(trigger, "request_id", None),
                "duration_ms": duration_ms,
                "sample_versions": sample_versions,
                "upstream_model": response.model,
                "upstream_response_id": response.id,
                "outcome": "success",
            },
        )

    def _log_turn_async(self, trigger, sample_versions, response, started_at, turn_id: str):
        """记录异步回合日志（结构化，带 turn_id）。"""
        duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
        self._logger.info(
            "turn_completed",
            extra={
                "turn_id": turn_id,
                "trigger_type": "user",
                "request_id": trigger.request_id,
                "duration_ms": duration_ms,
                "sample_versions": sample_versions,
                "upstream_model": response.model,
                "upstream_response_id": response.id,
                "outcome": "success",
            },
        )
