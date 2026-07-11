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


def create_outbox_router(outbox_store, gateway_api_key: str) -> APIRouter:
    """创建 Outbox API 路由。

    指令:
      1. GET /v1/outbox?after=N&limit=M → 鉴权 → 查询 → 响应
      2. after 默认 0, 非负整数
      3. limit 默认 20, clamp 到 1..100
      4. 空页时 next_cursor == 传入的 after
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

    return router
