"""ModelToolLoop：模型—工具循环。

数据合同来源：架构文档 6.5 ModelToolLoop。

指令：
  1. 初始化 tool_round=0、total_tool_calls=0
  2. 调用 LLM
  3. 无 tool_calls：返回最终响应
  4. 有 tool_calls：在执行整批之前检查轮次与总次数
  5. 若 total_tool_calls + batch_size > 10：整批不执行，抛 ToolLoopLimitError
  6. 保存 assistant(tool_calls) 到本轮 messages
  7. 按返回顺序交给 ToolDispatcher 执行每个工具
  8. 将每个 ToolResult 追加为 tool message
  9. tool_round += 1，total_tool_calls 增加本批数量
 10. 若 total_tool_calls == 10：返回明确上限错误，不再调用 LLM
 11. 未达到上限：重新调用 LLM

约束：
- 最多 5 轮。
- 总工具调用最多 10 次。
- 同一批工具不并行。
- 调用额度不足以容纳整批时，整批拒绝；禁止只执行一部分。
- 工具失败结果交还模型；不自动重试。
- 上限错误不再回灌模型，直接结束当前回合。
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from app.domain.models.chat_completion import ChatCompletionResponse, ToolLoopLimitError

if TYPE_CHECKING:
    from app.domain.models.turn import TurnContext
    from app.domain.ports.model_client import AsyncModelClient
    from app.adapters.tools.tool_dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


class ModelToolLoop:
    """模型—工具循环执行器。

    架构文档 6.5：每个回合拥有独立的 TurnContext，
    messages、tool history 和计数器不共享。
    """

    def __init__(
        self,
        model_client: "AsyncModelClient",
        dispatcher: "ToolDispatcher",
        max_rounds: int = 5,
        max_calls: int = 10,
    ):
        self._client = model_client
        self._dispatcher = dispatcher
        self._max_rounds = max_rounds
        self._max_calls = max_calls

    @property
    def model_client(self) -> "AsyncModelClient":
        """暴露 model_client 供测试断言。"""
        return self._client

    async def run(self, ctx: "TurnContext", tools: list[dict] | None = None) -> ChatCompletionResponse:
        """执行模型—工具循环。

        指令来源：架构文档 6.5 ModelToolLoop。

        返回最终 ChatCompletionResponse，或抛 ToolLoopLimitError。
        """
        # 步骤 1：初始化已在 TurnContext.create 中完成
        # 步骤 2：调用 LLM
        response = await self._call_llm(ctx, tools)

        while True:
            # 步骤 3：无 tool_calls → 返回最终响应
            tool_calls_raw = response.first_assistant_tool_calls()
            if tool_calls_raw is None:
                return response

            # 步骤 4：有 tool_calls，在执行整批之前检查轮次与总次数
            batch_size = len(tool_calls_raw)

            # 步骤 4a：检查轮次上限
            if ctx.tool_round >= self._max_rounds:
                raise ToolLoopLimitError(
                    f"max_rounds_exceeded: {ctx.tool_round} >= {self._max_rounds}",
                    tool_round=ctx.tool_round,
                    total_tool_calls=ctx.total_tool_calls,
                )

            # 步骤 5：若 total_tool_calls + batch_size > max_calls → 整批不执行
            if ctx.total_tool_calls + batch_size > self._max_calls:
                raise ToolLoopLimitError(
                    f"max_calls_exceeded: {ctx.total_tool_calls} + {batch_size} > {self._max_calls}",
                    tool_round=ctx.tool_round,
                    total_tool_calls=ctx.total_tool_calls,
                )

            # 步骤 6：保存 assistant(tool_calls) 到本轮 messages
            assistant_msg = self._extract_assistant_message(response)
            ctx.messages.append(assistant_msg)

            # 步骤 7：按返回顺序执行每个工具（不并行）
            for tc_raw in tool_calls_raw:
                tool_call_id = tc_raw.get("id", "")
                tool_name = tc_raw.get("function", {}).get("name", "")
                arguments_json = tc_raw.get("function", {}).get("arguments", "{}")

                logger.info(
                    "tool_call_started turn_id=%s tool_call_id=%s name=%s",
                    ctx.turn_id, tool_call_id, tool_name,
                )

                from app.domain.models.tool import ToolCall as ToolCallModel, ToolExecutionContext
                call = ToolCallModel(
                    id=tool_call_id,
                    name=tool_name,
                    arguments_json=arguments_json,
                )
                exec_ctx = ToolExecutionContext(
                    turn_id=ctx.turn_id,
                    trigger_type=ctx.trigger_type,
                    trigger_id=ctx.trigger_id,
                )

                result = await self._dispatcher.execute(call, exec_ctx)

                logger.info(
                    "tool_call_completed turn_id=%s tool_call_id=%s ok=%s duration_ms=%d",
                    ctx.turn_id, tool_call_id, result.ok, result.duration_ms,
                )

                # 步骤 8：将 ToolResult 追加为 tool message
                ctx.messages.append(self._tool_result_to_message(result))

            # 步骤 9：更新计数器
            ctx.tool_round += 1
            ctx.total_tool_calls += batch_size

            # 步骤 10：若 total_tool_calls == max_calls → 不再调用 LLM
            if ctx.total_tool_calls >= self._max_calls:
                raise ToolLoopLimitError(
                    f"max_calls_reached: {ctx.total_tool_calls} >= {self._max_calls}",
                    tool_round=ctx.tool_round,
                    total_tool_calls=ctx.total_tool_calls,
                )

            # 步骤 11：未达到上限 → 重新调用 LLM
            response = await self._call_llm(ctx, tools)

    async def _call_llm(
        self, ctx: "TurnContext", tools: list[dict] | None
    ) -> ChatCompletionResponse:
        """调用 LLM 并返回响应。"""
        from app.domain.models.turn import ModelCompletionInput

        request = ModelCompletionInput(
            messages=list(ctx.messages),
            temperature=None,
            max_output_tokens=None,
            tools=tools,
            tool_choice="auto" if tools else None,
        )
        return await self._client.complete(request)

    def _extract_assistant_message(self, response: ChatCompletionResponse):
        """从响应中提取 assistant 消息（含 tool_calls）。"""
        from app.domain.models.turn import ChatMessage

        choice = response.choices[0]
        return ChatMessage(
            role=choice.message_role,
            content=choice.message_content,
            tool_calls=choice.tool_calls,
        )

    def _tool_result_to_message(self, result):
        """将 ToolResult 转换为 tool role 的 ChatMessage。"""
        from app.domain.models.turn import ChatMessage

        return ChatMessage(
            role="tool",
            content=result.content,
            tool_call_id=result.tool_call_id,
        )
