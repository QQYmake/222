"""TurnRunner: 唯一回合编排器。

数据合同来源：架构文档 6.5 TurnRunner。

职责：只安排顺序——读取 Sample → 构造上下文 → 调用模型 → 返回或写入 Outbox。
不知道 Sample 存在文件中，不知道 Outbox 存在 SQLite 中。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from app.domain.ports.sample_reader import SampleReader, AllSamples
from app.domain.ports.model_client import ModelClient
from app.domain.models.context_builder import ContextBuilder
from app.domain.models.trigger import UserTrigger, TimerTrigger
from app.domain.models.turn import PreparedTurn, ModelCompletionInput
from app.domain.models.chat_completion import (
    ChatCompletionResponse,
    to_internal_max_output_tokens,
)
from app.domain.models.sample import SampleReadError
from app.infrastructure.logging import get_logger

import dataclasses


@dataclasses.dataclass(frozen=True)
class ActiveTurnResult:
    """主动回合结果。"""

    trigger_id: str
    outcome: str  # "message_enqueued" | "no_message" | "failed"
    event_id: Optional[str] = None


class TurnRunner:
    """唯一回合编排器。

    被动回合返回 ChatCompletionResponse。
    主动回合返回 ActiveTurnResult。
    """

    def __init__(
        self,
        sample_reader: SampleReader,
        context_builder: ContextBuilder,
        model_client: ModelClient,
        outbox_store=None,
    ):
        self._sample_reader = sample_reader
        self._context_builder = context_builder
        self._model_client = model_client
        self._outbox_store = outbox_store
        self._logger = get_logger("turn_runner")

    def run(self, trigger: UserTrigger | TimerTrigger):
        """执行一个回合。

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
        """主动回合处理。

        指令:
          1. outbox_store 为 None 时拒绝执行
          2. <NO_MESSAGE> → outcome="no_message", 不写 Outbox
          3. 正常文本 → enqueue_once → outcome="message_enqueued"
          4. M3 阶段暂时只支持被动回合, 主动回合完整实现在 M4
        """
        if self._outbox_store is None:
            raise RuntimeError(
                "outbox_store is required for active turns but was not provided"
            )

        # M4 will implement the full active turn logic here
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
