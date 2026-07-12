"""Outbox API Controller: GET /v1/outbox。

数据合同来源：架构文档 6.7 Outbox API Controller。

职责：鉴权、解析查询参数、调用 OutboxStore.list_after、返回 JSON。
不含业务逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.adapters.http.auth import authenticate_gateway_request, AuthenticationError
from app.adapters.http.errors import openai_error
from app.infrastructure.logging import get_logger


def create_outbox_router(outbox_store, gateway_api_key: str, longpoll_service=None) -> APIRouter:
    """创建 Outbox API 路由。

    指令:
      1. GET /v1/outbox?after=N&limit=M → 鉴权 → 查询 → 响应
      2. after 默认 0, 非负整数
      3. limit 默认 20, clamp 到 1..100
      4. 空页时 next_cursor == 传入的 after
      5. GET /v1/outbox/longpoll?after=N&timeout=T&reader_id=R → 长轮询（如果 longpoll_service 可用）
    """
    router = APIRouter()
    logger = get_logger("outbox_controller")

    @router.get("/v1/outbox")
    async def handle_outbox_query(request: Request):
        # 1. 鉴权
        try:
            authenticate_gateway_request(dict(request.headers), gateway_api_key)
        except AuthenticationError:
            return openai_error(401, "invalid_api_key", "Invalid or missing API key")

        # 2. 解析查询参数
        query_params = request.query_params

        # after: 非负整数, 默认 0
        after_str = query_params.get("after", "0")
        try:
            after = int(after_str)
            if after < 0:
                raise ValueError
        except ValueError:
            return openai_error(400, "invalid_request",
                                f"after must be a non-negative integer, got: {after_str}")

        # limit: 正整数, 默认 20, clamp 到 1..100
        limit_str = query_params.get("limit", "20")
        try:
            limit = int(limit_str)
        except ValueError:
            return openai_error(400, "invalid_request",
                                f"limit must be an integer, got: {limit_str}")

        # 3. 查询
        page = outbox_store.list_after(after, limit)

        # 4. 响应
        return JSONResponse(
            status_code=200,
            content={
                "items": [msg.to_dict() for msg in page.items],
                "next_cursor": page.next_cursor,
            },
        )

    @router.get("/v1/outbox/longpoll")
    async def handle_outbox_longpoll(request: Request):
        """长轮询端点。

        数据输入: after, timeout, reader_id (查询参数)
        数据输出: LongPollResponse
        指令:
          1. 鉴权
          2. 调用 OutboxLongPollService
          3. reader_busy → 409
          4. 有消息 → 200 + item
          5. 超时 → 200 + item=null
        """
        if longpoll_service is None:
            return openai_error(503, "service_unavailable", "Long poll is not enabled")

        # 1. 鉴权
        try:
            authenticate_gateway_request(dict(request.headers), gateway_api_key)
        except AuthenticationError:
            return openai_error(401, "invalid_api_key", "Invalid or missing API key")

        # 2. 解析参数
        query_params = request.query_params
        after_str = query_params.get("after", "0")
        try:
            after = int(after_str)
            if after < 0:
                raise ValueError
        except ValueError:
            return openai_error(400, "invalid_request",
                                f"after must be a non-negative integer, got: {after_str}")

        timeout_str = query_params.get("timeout", "30")
        try:
            timeout = int(timeout_str)
            if timeout < 1 or timeout > 30:
                raise ValueError
        except ValueError:
            return openai_error(400, "invalid_request",
                                f"timeout must be 1..30, got: {timeout_str}")

        reader_id = query_params.get("reader_id", "chen-primary")

        # 3. 执行长轮询
        response = await longpoll_service.long_poll(
            after=after,
            timeout_seconds=timeout,
            reader_id=reader_id,
        )

        # 4. 响应
        if response.reader_busy:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "reader_busy",
                    "item": None,
                    "next_cursor": response.next_cursor,
                },
            )

        return JSONResponse(
            status_code=200,
            content={
                "item": response.item.to_dict() if response.item else None,
                "next_cursor": response.next_cursor,
                "timed_out": response.timed_out,
            },
        )

    return router
