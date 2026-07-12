"""Chat API Controller: OpenAI Chat 兼容 HTTP 边界。

数据合同来源：架构文档 6.6 Chat API Controller。

职责：只做鉴权、协议校验、调用 TurnRunner 和错误映射。不含业务逻辑。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.adapters.http.auth import authenticate_gateway_request, AuthenticationError
from app.adapters.http.errors import openai_error
from app.domain.models.trigger import UserTrigger
from app.domain.models.chat_completion import (
    parse_chat_request,
    UnsupportedStreamError,
    ConflictingTokenFieldsError,
)
from app.domain.models.sample import SampleReadError
from app.domain.models.errors import UpstreamError, UpstreamTimeout
from app.infrastructure.logging import get_logger


def create_chat_router(turn_runner, gateway_api_key: str) -> APIRouter:
    """创建 Chat API 路由。

    指令:
      1. POST /v1/chat/completions → 鉴权 → 解析 → TurnRunner → 响应
      2. 错误映射到 OpenAI 兼容格式
    """
    router = APIRouter()
    logger = get_logger("chat_controller")

    @router.post("/v1/chat/completions")
    async def handle_chat_completions(request: Request):
        # 1. 鉴权
        try:
            authenticate_gateway_request(dict(request.headers), gateway_api_key)
        except AuthenticationError:
            return openai_error(401, "invalid_api_key", "Invalid or missing API key")

        # 2. 解析请求体
        try:
            body = await request.json()
        except Exception:
            return openai_error(400, "invalid_request", "Invalid JSON body")

        # 2a. v2: 前端 tools/tool_choice 不允许
        if body.get("tools") or body.get("tool_choice"):
            return openai_error(
                400, "client_tools_not_allowed",
                "Client-side tools are not allowed. All tools are managed by the server.",
            )

        # 3. 解析并校验 OpenAI Chat 请求
        try:
            chat_request = parse_chat_request(body)
        except UnsupportedStreamError:
            return openai_error(400, "unsupported_stream",
                                "stream=true is not supported in v1")
        except ConflictingTokenFieldsError:
            return openai_error(400, "conflicting_token_fields",
                                "max_completion_tokens and max_tokens cannot both be present")
        except ValueError as e:
            return openai_error(400, "invalid_request", str(e))

        # 4. 构造 UserTrigger
        trigger = UserTrigger(
            request_id=str(uuid.uuid4()),
            chat_request=body,
        )

        # 5. 调用 TurnRunner — v2 优先 async，兼容 sync
        try:
            if hasattr(turn_runner, 'run_user_turn'):
                response = await turn_runner.run_user_turn(trigger)
            else:
                response = turn_runner.run(trigger)
            return JSONResponse(status_code=200, content=response.to_dict())
        except SampleReadError as e:
            logger.error("sample_read_failed", extra={"reason": e.reason})
            return openai_error(
                503, "state_unavailable",
                f"{e.sample_type} sample is unavailable: {e.reason}",
            )
        except UpstreamTimeout:
            logger.error("upstream_timeout")
            return openai_error(504, "upstream_timeout", "Model request timed out")
        except UpstreamError as e:
            logger.error("upstream_error", extra={"status_code": e.status_code})
            return openai_error(502, "upstream_error", "Model provider failed")
        except Exception as e:
            if "ToolLoopLimit" in type(e).__name__:
                return openai_error(422, "tool_loop_limit_exceeded", str(e))
            raise

    return router
