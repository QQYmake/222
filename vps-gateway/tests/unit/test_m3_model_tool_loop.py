"""M3 ModelToolLoop 测试。

验证场景覆盖架构文档 12.1.1—2、7—11：
- 多轮工具调用
- 同一响应多工具按顺序执行
- 超过 5 轮上限
- 超过 10 次调用上限
- 已执行 9 次后模型一次请求 3 个工具：整批不执行
- 首次响应一次请求 11 个工具：整批不执行
- 已执行 9 次后模型请求 1 个工具：执行第 10 个后终止
- 工具失败结果回灌模型
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.models.turn import ChatMessage, ModelCompletionInput, TurnContext
from app.domain.models.chat_completion import (
    ChatCompletionResponse,
    Choice,
    ToolCall,
    ToolCallFunction,
    ToolLoopLimitError,
)
from app.domain.models.tool import ToolDefinition, ToolCall as ToolCallModel, ToolResult, ToolExecutionContext
from app.adapters.tools.registry import ToolRegistry
from app.adapters.tools.tool_dispatcher import ToolDispatcher
from app.application.model_tool_loop import ModelToolLoop
from app.domain.ports.model_client import AsyncModelClient


# ── helpers ──────────────────────────────────────────────────────────────────

def _assistant_msg(tool_calls: list[dict] | None = None, content: str | None = None) -> ChatMessage:
    return ChatMessage(role="assistant", content=content, tool_calls=tool_calls)


def _tool_msg(tool_call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=tool_call_id)


def _user_msg(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content)


def _make_response(
    tool_calls: list[ToolCall] | None = None,
    content: str | None = None,
    finish_reason: str = "stop",
) -> ChatCompletionResponse:
    choice = Choice(
        index=0,
        message_role="assistant",
        message_content=content,
        finish_reason=finish_reason,
        tool_calls=[tc.to_dict() for tc in tool_calls] if tool_calls else None,
    )
    return ChatCompletionResponse(
        id="resp-1",
        object="chat.completion",
        created=0,
        model="test-model",
        choices=[choice],
        usage={},
    )


def _make_tool_call(call_id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments=json.dumps(args)),
    )


class FakeModelClient(AsyncModelClient):
    """按预设序列返回响应的 mock client。"""

    def __init__(self, responses: list[ChatCompletionResponse]):
        self._responses = list(responses)
        self.call_count = 0
        self.call_inputs: list[ModelCompletionInput] = []

    async def complete(self, request: ModelCompletionInput) -> ChatCompletionResponse:
        self.call_inputs.append(request)
        idx = min(self.call_count, len(self._responses) - 1)
        self.call_count += 1
        return self._responses[idx]

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _make_registry(test_tools: bool = True) -> ToolRegistry:
    """构造含 echo_test / get_server_time 的 registry。"""
    from app.adapters.tools.test_tools import EchoTestExecutor
    from app.adapters.tools.get_server_time import GetServerTimeExecutor

    echo_def = ToolDefinition(
        name="echo_test",
        description="echo",
        parameters={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        enabled_in_production=False,
        timeout_seconds=5,
        max_result_chars=1000,
    )
    time_def = ToolDefinition(
        name="get_server_time",
        description="get time",
        parameters={"type": "object", "properties": {}},
        enabled_in_production=True,
        timeout_seconds=5,
        max_result_chars=1000,
    )
    registry = ToolRegistry(test_tools_enabled=test_tools)
    registry.register(echo_def, EchoTestExecutor())
    registry.register(time_def, GetServerTimeExecutor())
    return registry


def _make_loop(
    responses: list[ChatCompletionResponse],
    max_rounds: int = 5,
    max_calls: int = 10,
    test_tools: bool = True,
) -> ModelToolLoop:
    registry = _make_registry(test_tools=test_tools)
    dispatcher = ToolDispatcher(registry)
    client = FakeModelClient(responses)
    return ModelToolLoop(
        model_client=client,
        dispatcher=dispatcher,
        max_rounds=max_rounds,
        max_calls=max_calls,
    )


# ── 12.1.1 三轮工具调用 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_three_round_tool_calls():
    """验证 12.1.1：LLM 连续调用工具三轮，调用顺序和 tool_call_id 配对正确。"""
    loop = _make_loop([
        _make_response(tool_calls=[_make_tool_call("c1", "echo_test", {"msg": "round1"})]),
        _make_response(tool_calls=[_make_tool_call("c2", "echo_test", {"msg": "round2"})]),
        _make_response(tool_calls=[_make_tool_call("c3", "echo_test", {"msg": "round3"})]),
        _make_response(content="done"),
    ])

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    result = await loop.run(ctx)

    assert result.choices[0].message_content == "done"
    assert ctx.tool_round == 3
    assert ctx.total_tool_calls == 3
    # 验证 tool_call_id 配对：每次调用 LLM 时 messages 中有对应的 tool message
    last_input = loop.model_client.call_inputs[-1]
    tool_msgs = [m for m in last_input.messages if m.role == "tool"]
    assert len(tool_msgs) == 3
    assert tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[1].tool_call_id == "c2"
    assert tool_msgs[2].tool_call_id == "c3"


# ── 12.1.2 同一响应多工具按顺序执行 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_tools_in_one_response_ordered():
    """验证 12.1.2：同一响应含多个工具，严格按返回顺序执行。"""
    loop = _make_loop([
        _make_response(tool_calls=[
            _make_tool_call("c1", "echo_test", {"msg": "first"}),
            _make_tool_call("c2", "echo_test", {"msg": "second"}),
            _make_tool_call("c3", "echo_test", {"msg": "third"}),
        ]),
        _make_response(content="all done"),
    ])

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    result = await loop.run(ctx)

    assert result.choices[0].message_content == "all done"
    assert ctx.total_tool_calls == 3
    # 验证顺序：tool messages 按 c1, c2, c3 排列
    last_input = loop.model_client.call_inputs[-1]
    tool_msgs = [m for m in last_input.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2", "c3"]


# ── 12.1.7 超过五轮 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exceeds_max_rounds():
    """验证 12.1.7：超过 5 轮不再调用模型，抛 ToolLoopLimitError。"""
    # 6 轮 tool_calls，第 7 轮才有 content
    responses = [_make_response(tool_calls=[_make_tool_call(f"c{i}", "echo_test", {"msg": str(i)})]) for i in range(6)]
    responses.append(_make_response(content="done"))
    loop = _make_loop(responses, max_rounds=5)

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    with pytest.raises(ToolLoopLimitError) as exc_info:
        await loop.run(ctx)

    assert "max_rounds" in str(exc_info.value).lower() or "round" in str(exc_info.value).lower()
    assert ctx.tool_round == 5
    assert loop.model_client.call_count == 6  # 5 轮 + 第 6 次 LLM 后超限


# ── 12.1.8 超过十次调用 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exceeds_max_calls():
    """验证 12.1.8：超过 10 次工具调用不再执行，抛 ToolLoopLimitError。"""
    # 5 轮，每轮 2 个工具 = 10 次，第 6 轮再来 1 个 = 11
    responses = []
    for i in range(5):
        responses.append(_make_response(tool_calls=[
            _make_tool_call(f"c{i}a", "echo_test", {"msg": str(i) + "a"}),
            _make_tool_call(f"c{i}b", "echo_test", {"msg": str(i) + "b"}),
        ]))
    responses.append(_make_response(tool_calls=[_make_tool_call("c11", "echo_test", {"msg": "11"})]))
    responses.append(_make_response(content="done"))
    loop = _make_loop(responses, max_calls=10)

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    with pytest.raises(ToolLoopLimitError):
        await loop.run(ctx)

    assert ctx.total_tool_calls == 10


# ── 12.1.9 已执行 9 次后模型一次请求 3 个工具 ──────────────────────────────

@pytest.mark.asyncio
async def test_batch_exceeds_limit_partial():
    """验证 12.1.9：已执行 9 次后模型请求 3 个工具，整批 3 个均不执行。"""
    # 前 9 次：3 轮 × 3 工具
    responses = []
    for i in range(3):
        responses.append(_make_response(tool_calls=[
            _make_tool_call(f"c{i}a", "echo_test", {"msg": str(i) + "a"}),
            _make_tool_call(f"c{i}b", "echo_test", {"msg": str(i) + "b"}),
            _make_tool_call(f"c{i}c", "echo_test", {"msg": str(i) + "c"}),
        ]))
    # 第 4 轮：3 个工具 → 9 + 3 = 12 > 10 → 整批拒绝
    responses.append(_make_response(tool_calls=[
        _make_tool_call("d1", "echo_test", {"msg": "d1"}),
        _make_tool_call("d2", "echo_test", {"msg": "d2"}),
        _make_tool_call("d3", "echo_test", {"msg": "d3"}),
    ]))
    responses.append(_make_response(content="done"))
    loop = _make_loop(responses, max_calls=10)

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    with pytest.raises(ToolLoopLimitError):
        await loop.run(ctx)

    assert ctx.total_tool_calls == 9  # 第 10 次未执行


# ── 12.1.10 首次响应一次请求 11 个工具 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_first_response_eleven_tools():
    """验证 12.1.10：首次响应请求 11 个工具，整批不执行。"""
    tool_calls = [_make_tool_call(f"c{i}", "echo_test", {"msg": str(i)}) for i in range(11)]
    loop = _make_loop([
        _make_response(tool_calls=tool_calls),
        _make_response(content="done"),
    ])

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    with pytest.raises(ToolLoopLimitError):
        await loop.run(ctx)

    assert ctx.total_tool_calls == 0  # 整批未执行


# ── 12.1.11 已执行 9 次后模型请求 1 个工具 ──────────────────────────────────

@pytest.mark.asyncio
async def test_tenth_call_then_stop():
    """验证 12.1.11：已执行 9 次后请求 1 个工具，执行第 10 个后终止，不再调用模型。"""
    # 前 9 次
    responses = []
    for i in range(3):
        responses.append(_make_response(tool_calls=[
            _make_tool_call(f"c{i}a", "echo_test", {"msg": str(i) + "a"}),
            _make_tool_call(f"c{i}b", "echo_test", {"msg": str(i) + "b"}),
            _make_tool_call(f"c{i}c", "echo_test", {"msg": str(i) + "c"}),
        ]))
    # 第 4 轮：1 个工具 → 9 + 1 = 10 → 执行第 10 个，不再调用模型
    responses.append(_make_response(tool_calls=[_make_tool_call("d1", "echo_test", {"msg": "d1"})]))
    responses.append(_make_response(content="done"))
    loop = _make_loop(responses, max_calls=10)

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    with pytest.raises(ToolLoopLimitError):
        await loop.run(ctx)

    assert ctx.total_tool_calls == 10
    # 第 10 个执行后不再调用 LLM
    assert loop.model_client.call_count == 4


# ── 工具失败回灌模型 ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_failure_returned_to_model():
    """验证工具失败不重试，转换为 tool message 回灌模型。"""
    from app.adapters.tools.test_tools import FailTestExecutor

    fail_def = ToolDefinition(
        name="fail_test",
        description="always fails",
        parameters={"type": "object", "properties": {}},
        enabled_in_production=False,
        timeout_seconds=5,
        max_result_chars=1000,
    )
    registry = ToolRegistry(test_tools_enabled=True)
    registry.register(fail_def, FailTestExecutor())

    dispatcher = ToolDispatcher(registry)
    client = FakeModelClient([
        _make_response(tool_calls=[_make_tool_call("f1", "fail_test", {})]),
        _make_response(content="handled failure"),
    ])
    loop = ModelToolLoop(client, dispatcher, max_rounds=5, max_calls=10)

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    result = await loop.run(ctx)

    assert result.choices[0].message_content == "handled failure"
    # tool message 中包含错误信息
    last_input = client.call_inputs[-1]
    tool_msgs = [m for m in last_input.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "tool_failed" in tool_msgs[0].content or "fail" in tool_msgs[0].content.lower()


# ── 无工具调用直接返回 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_tool_calls_immediate_return():
    """验证无 tool_calls 时直接返回最终响应。"""
    loop = _make_loop([_make_response(content="hello")])

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    result = await loop.run(ctx)

    assert result.choices[0].message_content == "hello"
    assert ctx.tool_round == 0
    assert ctx.total_tool_calls == 0
    assert loop.model_client.call_count == 1


# ── 12.1.3 工具名称不存在 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_not_found_in_loop():
    """验证工具名不存在时返回错误 tool message 并回灌模型。"""
    loop = _make_loop([
        _make_response(tool_calls=[_make_tool_call("x1", "nonexistent_tool", {})]),
        _make_response(content="handled"),
    ])

    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    result = await loop.run(ctx)

    assert result.choices[0].message_content == "handled"
    last_input = loop.model_client.call_inputs[-1]
    tool_msgs = [m for m in last_input.messages if m.role == "tool"]
    assert "not found" in tool_msgs[0].content.lower() or "tool_not_found" in tool_msgs[0].content


# ── 12.1.12 生产配置不暴露测试工具 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_production_excludes_test_tools():
    """验证 12.1.12：生产配置下 echo_test 不出现在工具 Schema 中。"""
    loop = _make_loop([_make_response(content="ok")], test_tools=False)
    # 在 loop.run 时，传入的 tools 应不包含 test tools
    # 因为 ModelToolLoop 从 registry.schemas() 获取工具
    ctx = TurnContext.create("user", "req-1", [_user_msg("hi")], {})
    await loop.run(ctx)

    # 检查传给 model client 的 tools 不含 echo_test
    first_input = loop.model_client.call_inputs[0]
    if first_input.tools:
        tool_names = [t.get("function", {}).get("name") for t in first_input.tools]
        assert "echo_test" not in tool_names
        assert "fail_test" not in tool_names
        assert "delay_test" not in tool_names
