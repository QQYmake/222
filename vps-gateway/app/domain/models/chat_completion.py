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
    """单个选择项。"""

    index: int
    message_role: str
    message_content: str
    finish_reason: str


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
                return choice.message_content
        return ""

    def to_dict(self) -> dict[str, Any]:
        """转换为 OpenAI 兼容 dict。"""
        result: dict[str, Any] = {
            "id": self.id,
            "object": self.object,
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": c.index,
                    "message": {"role": c.message_role, "content": c.message_content},
                    "finish_reason": c.finish_reason,
                }
                for c in self.choices
            ],
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
        if not isinstance(content, str):
            raise ValueError("choice.message.content must be a string")
        parsed_choices.append(
            Choice(
                index=ch.get("index", 0),
                message_role=msg.get("role", "assistant"),
                message_content=content,
                finish_reason=ch.get("finish_reason", "stop"),
            )
        )

    has_assistant = any(c.message_role == "assistant" and c.message_content for c in parsed_choices)
    if not has_assistant:
        raise ValueError("response must contain at least one assistant text result")

    return ChatCompletionResponse(
        id=resp_id,
        object=parsed.get("object", "chat.completion"),
        created=parsed.get("created", 0),
        model=model,
        choices=parsed_choices,
        usage=parsed.get("usage"),
    )
