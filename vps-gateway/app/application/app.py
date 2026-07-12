"""应用工厂: 组装所有依赖、创建 FastAPI 实例。

数据合同来源：架构文档 6.17 AppLifecycle + 3.1 依赖方向。

职责：只做依赖注入和路由注册，不含业务逻辑。

v2 变更：
  1. 使用 AsyncOpenAIUpstreamClient 替代同步 OpenAIUpstreamClient
  2. FastAPI lifespan 管理共享 Async HTTP Client 和 Scheduler 生命周期
  3. 构造函数不启动后台任务（架构不变量 16）
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.infrastructure.config import Config
from app.adapters.samples.file_sample_repository import FileSampleRepository
from app.adapters.models.openai_upstream_client import OpenAIUpstreamClient
from app.adapters.models.async_openai_client import AsyncOpenAIUpstreamClient
from app.adapters.outbox.sqlite_outbox_store import SQLiteOutboxStore
from app.adapters.http.chat_controller import create_chat_router
from app.adapters.http.outbox_controller import create_outbox_router
from app.adapters.scheduler.local_scheduler import LocalScheduler
from app.application.turn_runner import TurnRunner
from app.domain.models.context_builder import ContextBuilder
from app.infrastructure.logging import get_logger


def create_app(config=None) -> FastAPI:
    """组装所有依赖并创建 FastAPI 应用。

    数据输入: Config (从环境变量加载，可选，默认从环境创建)
    数据输出: FastAPI 实例 (已注册所有路由)
    指令:
      1. 校验配置
      2. 创建 SampleReader (FileSampleRepository)
      3. 创建 AsyncModelClient (AsyncOpenAIUpstreamClient)
      4. 创建 OutboxStore (SQLiteOutboxStore) — 短连接
      5. 创建 ContextBuilder
      6. 创建 TurnRunner (注入以上依赖)
      7. 创建 LocalScheduler (不启动)
      8. 注册 Chat Controller 路由
      9. 注册 Outbox Controller 路由
     10. lifespan: startup → start async client + scheduler; shutdown → stop
    """
    logger = get_logger("app_factory")

    if config is None:
        config = Config.load_from_env()

    # 0. 配置校验
    config.validate()

    # 1. 适配器
    sample_reader = FileSampleRepository(config.sample_directory)
    async_model_client = AsyncOpenAIUpstreamClient(config=config)
    outbox_store = SQLiteOutboxStore(config.outbox_database_path)

    # 2. 领域服务
    context_builder = ContextBuilder(memory_char_budget=config.memory_char_budget)

    # 3. 应用编排 — 注入 async model client
    turn_runner = TurnRunner(
        sample_reader=sample_reader,
        context_builder=context_builder,
        model_client=async_model_client,
        outbox_store=outbox_store,
    )

    # 4. 调度器 — 不在构造时启动
    scheduler = LocalScheduler(
        turn_runner=turn_runner,
        interval_minutes=config.active_turn_interval_minutes,
        instruction=config.active_turn_instruction,
        enabled=config.active_turn_enabled,
    )

    # 5. HTTP 路由
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # --- startup ---
        # 1. 创建共享 Async HTTP Client
        await async_model_client.start()
        logger.info("async_model_client_started")

        # 2. 启动 Scheduler（只在 enabled 时）
        app.state.scheduler_started = False
        if config.active_turn_enabled:
            scheduler.start()
            app.state.scheduler_started = True

        logger.info("app_started", extra={
            "host": config.gateway_host,
            "port": config.gateway_port,
            "active_turn_enabled": config.active_turn_enabled,
        })

        yield

        # --- shutdown ---
        # 1. 停止接收新后台任务
        scheduler.shutdown()
        app.state.scheduler_started = False

        # 2. 关闭 Async HTTP Client
        await async_model_client.close()
        logger.info("async_model_client_closed")

        logger.info("app_stopped")

    app = FastAPI(title="VPS Gateway", lifespan=lifespan)
    app.include_router(create_chat_router(turn_runner, config.gateway_api_key))

    # 创建长轮询服务（如果配置启用）
    longpoll_service = None
    if getattr(config, 'outbox_longpoll_enabled', True):
        from app.adapters.outbox.in_process_notifier import InProcessNotifier
        from app.application.single_reader_gate import SingleReaderGate
        from app.application.outbox_longpoll_service import OutboxLongPollService
        notifier = InProcessNotifier()
        gate = SingleReaderGate()
        longpoll_service = OutboxLongPollService(
            store=outbox_store,
            notifier=notifier,
            gate=gate,
            default_timeout_seconds=getattr(config, 'outbox_longpoll_timeout_seconds', 30),
        )
        app.state.notifier = notifier
        app.state.single_reader_gate = gate
        app.state.longpoll_service = longpoll_service

    app.include_router(create_outbox_router(outbox_store, config.gateway_api_key, longpoll_service))
    app.state.scheduler = scheduler
    app.state.config = config
    app.state.async_model_client = async_model_client
    app.state.scheduler_started = False

    return app
