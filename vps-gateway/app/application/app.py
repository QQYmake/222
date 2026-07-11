"""应用工厂: 组装所有依赖、创建 FastAPI 实例。

数据合同来源：架构文档 8 应用组装。

职责：只做依赖注入和路由注册，不含业务逻辑。
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from app.infrastructure.config import Config
from app.adapters.samples.file_sample_repository import FileSampleRepository
from app.adapters.models.openai_upstream_client import OpenAIUpstreamClient
from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
from app.adapters.http.chat_controller import create_chat_router
from app.adapters.http.outbox_controller import create_outbox_router
from app.application.turn_runner import TurnRunner
from app.domain.models.context_builder import ContextBuilder
from app.infrastructure.logging import get_logger


def create_app(config: Config) -> FastAPI:
    """组装所有依赖并创建 FastAPI 应用。

    数据输入: Config (从环境变量加载)
    数据输出: FastAPI 实例 (已注册所有路由)
    指令:
      1. 创建 SampleReader (FileSampleRepository)
      2. 创建 ModelClient (OpenAIUpstreamClient)
      3. 创建 OutboxStore (SQLiteOutboxStore)
      4. 创建 ContextBuilder
      5. 创建 TurnRunner (注入以上依赖)
      6. 注册 Chat Controller 路由
      7. 注册 Outbox Controller 路由
    """
    logger = get_logger("app_factory")

    # 1. 适配器
    sample_reader = FileSampleRepository(config.sample_directory)
    model_client = OpenAIUpstreamClient(
        base_url=config.upstream_base_url,
        api_key=config.upstream_api_key,
        model=config.upstream_model,
        timeout_seconds=config.upstream_timeout_seconds,
        token_limit_field=config.upstream_token_limit_field,
    )
    outbox_store = SQLiteOutboxStore(config.outbox_database_path)

    # 2. 领域服务
    context_builder = ContextBuilder(memory_char_budget=config.memory_char_budget)

    # 3. 应用编排
    turn_runner = TurnRunner(
        sample_reader=sample_reader,
        context_builder=context_builder,
        model_client=model_client,
        outbox_store=outbox_store,
    )

    # 4. HTTP 路由
    app = FastAPI(title="VPS Gateway")
    app.include_router(create_chat_router(turn_runner, config.gateway_api_key))
    app.include_router(create_outbox_router(outbox_store, config.gateway_api_key))

    logger.info("app_created", extra={
        "host": config.gateway_host,
        "port": config.gateway_port,
    })

    return app
