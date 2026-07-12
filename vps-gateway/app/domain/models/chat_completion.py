"""ChatCompletion 请求解析与响应校验。

数据合同来源：架构文档 5.6 / 5.7。
"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional


class UnsupportedStreamError(Exception):
    """stream=true 不被首版支持。"""


class ConflictingTokenFieldsError(Exception):
    """max_completion_tokens 和 max_tokens 不可同时出现。"""


class ToolLoopLimitError(Exception):
    """工具循环达到上限（轮次或总次数）。

    架构不变量 5：达到工具上限后不再调用模型。
    """
    def __init__(self, reason: str, tool_round: int = 0, total_tool_calls: int = 0):
        self.reason = reason
        self.tool_round = tool_round
        self.total_tool_calls = total_tool_calls
        super().__init__(f"ToolLoopLimitError: {reason} (round={tool_round}, calls={total_tool_calls})")


@dataclasses.dataclass(frozen=True)
class ToolCallFunction:
    """OpenAI tool_call.function 结构。"""

    name: str
    arguments: str  # 原始 JSON 字符串

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": self.arguments}


@dataclasses.dataclass(frozen=True)
class ToolCall:
    """OpenAI tool_call 结构。

    数据合同来源：架构文档 5.3 ToolCall。
    """

    id: str
    type: str = "function"
    function: Optional[ToolCallFunction] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": (self.function or ToolCallFunction("", "")).to_dict(),
        }


@dataclasses.dataclass(frozen=True)
class ChatCompletionRequest:
    """OpenAI Chat 请求（网关接受的最小子集）。"""

    model: str
    messages: list[dict[str, Any]]
    temperature: Optional[float]
    max_completion_tokens: Optional[int]
    max_tokens: Optional[int]
    stream: Optional[bool]


def parse_chat_request(raw: dict[str, Any]) -> ChatCompletionRequest:
    """解析并校验 OpenAI Chat 请求体。

    指令:
      1. 校验 messages 存在且非空
      2. 校验每条 message: role in (system/user/assistant), content 非空字符串
      3. stream 只允许 false 或不传; true → raise UnsupportedStreamError
      4. max_completion_tokens 和 max_tokens 不可同时出现 → raise ConflictingTokenFieldsError
      5. model 字段允许任意字符串
    """
    messages = raw.get("messages")
    if not messages or not isinstance(messages, list):
        raise ValueError("messages must be a non-empty list")

    valid_roles = {"system", "user", "assistant"}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError(f"messages[{i}] must be an object")
        role = msg.get("role")
        if role not in valid_roles:
            raise ValueError(f"messages[{i}].role must be one of {valid_roles}")
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError(f"messages[{i}].content must be a non-empty string")

    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")

    stream = raw.get("stream")
    if stream is True:
        raise UnsupportedStreamError("stream=true is not supported in v1")

    max_completion_tokens = raw.get("max_completion_tokens")
    max_tokens = raw.get("max_tokens")
    if max_completion_tokens is not None and max_tokens is not None:
        raise ConflictingTokenFieldsError(
            "max_completion_tokens and max_tokens cannot both be present"
        )

    temperature = raw.get("temperature")

    return ChatCompletionRequest(
        model=model,
        messages=messages,
        temperature=temperature,
        max_completion_tokens=max_completion_tokens,
        max_tokens=max_tokens,
        stream=stream,
    )


def to_internal_max_output_tokens(request: ChatCompletionRequest) -> Optional[int]:
    """提取统一的 max_output_tokens。

    指令:
      1. max_completion_tokens 优先
      2. 回退到 max_tokens
      3. 都没有则 None
    """
    if request.max_completion_tokens is not None:
        return request.max_completion_tokens
    return request.max_tokens


@dataclasses.dataclass(frozen=True)
class Choice:
    """单个选择项。v2 扩展：支持 tool_calls。"""

    index: int
    message_role: str
    message_content: Optional[str]
    finish_reason: str
    tool_calls: Optional[list[dict]] = None


@dataclasses.dataclass(frozen=True)
class ChatCompletionResponse:
    """OpenAI Chat 响应（标准化）。"""

    id: str
    object: str
    created: int
    model: str
    choices: list[Choice]
    usage: Optional[dict[str, Any]]

    def first_assistant_text(self) -> str:
        """返回第一个 assistant 文本结果。"""
        for choice in self.choices:
            if choice.message_role == "assistant":
                return choice.message_content or ""
        return ""

    def first_assistant_tool_calls(self) -> Optional[list[dict]]:
        """返回第一个 assistant 的 tool_calls，无则 None。"""
        for choice in self.choices:
            if choice.message_role == "assistant" and choice.tool_calls:
                return choice.tool_calls
        return None

    def to_dict(self) -> dict[str, Any]:
        """转换为 OpenAI 兼容 dict。"""
        choices: list[dict[str, Any]] = []
        for c in self.choices:
            msg: dict[str, Any] = {"role": c.message_role, "content": c.message_content}
            if c.tool_calls is not None:
                msg["tool_calls"] = c.tool_calls
            choices.append({
                "index": c.index,
                "message": msg,
                "finish_reason": c.finish_reason,
            })
        result: dict[str, Any] = {
            "id": self.id,
            "object": self.object,
            "created": self.created,
            "model": self.model,
            "choices": choices,
        }
        if self.usage is not None:
            result["usage"] = self.usage
        return result


def validate_chat_completion_response(parsed: dict[str, Any]) -> ChatCompletionResponse:
    """校验上游返回的 OpenAI Chat 响应。

    指令:
      1. 校验 choices 非空
      2. 校验至少一个 assistant 文本结果
      3. 校验 id, model 存在
    """
    if not isinstance(parsed, dict):
        raise ValueError("response must be a JSON object")

    resp_id = parsed.get("id")
    if not resp_id or not isinstance(resp_id, str):
        raise ValueError("response.id is required")

    model = parsed.get("model")
    if not model or not isinstance(model, str):
        raise ValueError("response.model is required")

    choices = parsed.get("choices")
    if not choices or not isinstance(choices, list):
        raise ValueError("response.choices must be a non-empty list")

    parsed_choices: list[Choice] = []
    for ch in choices:
        msg = ch.get("message", {})
        content = msg.get("content", "")
        if not isinstance(content, (str, type(None))):
            raise ValueError("choice.message.content must be a string or null")
        tool_calls = msg.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise ValueError("choice.message.tool_calls must be a list if present")
        parsed_choices.append(
            Choice(
                index=ch.get("index", 0),
                message_role=msg.get("role", "assistant"),
                message_content=content,
                finish_reason=ch.get("finish_reason", "stop"),
                tool_calls=tool_calls,
            )
        )

    # v2: assistant 有 tool_calls 时允许 content 为 null
    has_assistant = any(
        c.message_role == "assistant" and (c.message_content or c.tool_calls)
        for c in parsed_choices
    )
    if not has_assistant:
        raise ValueError("response must contain at least one assistant text result or tool_calls")

    return ChatCompletionResponse(
        id=resp_id,
        object=parsed.get("object", "chat.completion"),
        created=parsed.get("created", 0),
        model=model,
        choices=parsed_choices,
        usage=parsed.get("usage"),
    )
