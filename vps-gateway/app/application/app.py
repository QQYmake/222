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
from app.adapters.tools.registry import ToolRegistry
from app.adapters.tools.tool_dispatcher import ToolDispatcher
from app.adapters.tools.wake_tool_definitions import (
    SCHEDULE_WAKEUP_DEF,
    LIST_WAKEUPS_DEF,
    CANCEL_WAKEUP_DEF,
)
from app.adapters.tools.wake_tools import (
    ScheduleWakeupExecutor,
    ListWakeupsExecutor,
    CancelWakeupExecutor,
)
from app.adapters.wakeups.sqlite_wake_job_store import SQLiteWakeJobStore
from app.application.turn_runner import TurnRunner
from app.application.model_tool_loop import ModelToolLoop
from app.application.active_turn_gate import ActiveTurnGate
from app.application.wake_start_policy import WakeStartPolicy
from app.application.schedule_admission_policy import ScheduleAdmissionPolicy
from app.application.wake_planner import WakePlanner
from app.application.wake_controller import WakeController
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

    # 1b. 唤醒任务存储
    wake_job_store = SQLiteWakeJobStore(config.wake_jobs_database_path)

    # 1c. 工具注册表 — 注册唤醒工具
    tool_registry = ToolRegistry(test_tools_enabled=False)
    schedule_admission_policy = ScheduleAdmissionPolicy()
    tool_registry.register(
        SCHEDULE_WAKEUP_DEF,
        ScheduleWakeupExecutor(wake_job_store, schedule_admission_policy),
    )
    tool_registry.register(
        LIST_WAKEUPS_DEF,
        ListWakeupsExecutor(wake_job_store),
    )
    tool_registry.register(
        CANCEL_WAKEUP_DEF,
        CancelWakeupExecutor(wake_job_store),
    )

    # 1d. 工具调度器 + 模型工具循环
    tool_dispatcher = ToolDispatcher(tool_registry)
    model_tool_loop = ModelToolLoop(
        model_client=async_model_client,
        dispatcher=tool_dispatcher,
    )

    # 2. 领域服务
    context_builder = ContextBuilder(memory_char_budget=config.memory_char_budget)

    # 2b. 记忆引擎（如果启用）
    memory_port = None
    memory_recall_executor = None
    if config.memory_enabled:
        from app.domain.ports.memory_engine import MemoryEngineConfig
        from app.application.memory.memory_engine import MemoryEngine
        from app.application.memory.buffer_manager import BufferManager
        from app.adapters.memory.sqlite_buffer_store import SQLiteBufferStore
        from app.adapters.tools.memory_recall_tool import MemoryRecallExecutor
        from app.adapters.memory.ebbingflow.llm_bridge import LLMBridge, LLMBridgeConfig
        from app.application.memory.intent_classifier import IntentClassifier
        from app.application.memory.retrieval_pipeline import RetrievalPipeline
        from app.application.memory.surface_generator import SurfaceGenerator
        from app.application.memory.consolidation_pipeline import ConsolidationPipeline
        from app.application.memory.polish_bridge import PolishBridge

        buffer_store = SQLiteBufferStore(config.memory_db_path)

        mem_config = MemoryEngineConfig(
            db_path=config.memory_db_path,
            retrieval_timeout=config.memory_retrieval_timeout,
            surface_interval=config.memory_surface_interval,
            consolidation_hour=config.memory_consolidation_hour,
            enabled=True,
            embed_type=config.mem_embed_type,
            embed_model=config.mem_embed_model,
            intent_model_config={
                "base_url": config.mem_intent_base_url,
                "api_key": config.mem_intent_api_key,
                "model": config.mem_intent_model,
            },
            gen_model_config={
                "base_url": config.mem_gen_base_url,
                "api_key": config.mem_gen_api_key,
                "model": config.mem_gen_model,
            },
            surf_model_config={
                "base_url": config.mem_surf_base_url,
                "api_key": config.mem_surf_api_key,
                "model": config.mem_surf_model,
            },
            extract_model_config={
                "base_url": config.mem_extract_base_url,
                "api_key": config.mem_extract_api_key,
                "model": config.mem_extract_model,
            },
            persona_model_config={
                "base_url": config.mem_persona_base_url,
                "api_key": config.mem_persona_api_key,
                "model": config.mem_persona_model,
            },
            saga_model_config={
                "base_url": config.mem_saga_base_url,
                "api_key": config.mem_saga_api_key,
                "model": config.mem_saga_model,
            },
            polish_model_config={
                "base_url": config.mem_polish_base_url,
                "api_key": config.mem_polish_api_key,
                "model": config.mem_polish_model,
            },
        )

        buffer_manager = BufferManager(buffer_store)

        # 创建 LLMBridge 实例（使用 gen 模型配置作为默认记忆 LLM）
        gen_cfg = mem_config.gen_model_config
        llm_bridge = LLMBridge(
            config=LLMBridgeConfig(
                base_url=gen_cfg["base_url"],
                api_key=gen_cfg["api_key"],
                model=gen_cfg["model"],
            ),
            category="memory",
        )

        # 润色桥
        polish_bridge = PolishBridge(llm_bridge=llm_bridge)

        # 意图分类器
        intent_classifier = IntentClassifier(llm_bridge=llm_bridge)

        # 检索管线（@4 查询路径）
        retrieval_pipeline = RetrievalPipeline(
            llm_bridge=llm_bridge,
            buffer_manager=buffer_manager,
            polish_bridge=polish_bridge,
        )

        # @e 周期生成器
        surface_generator = SurfaceGenerator(
            llm_bridge=llm_bridge,
            buffer_manager=buffer_manager,
            polish_bridge=polish_bridge,
        )

        # 沉淀管线（依赖较多，使用延迟导入）
        try:
            from app.adapters.memory.sqlite_graph_store import SQLiteGraphStore
            from app.adapters.memory.sqlite_persona_store import SQLitePersonaStore
            from app.adapters.memory.ebbingflow.event_extractor import EventExtractor
            from app.adapters.memory.ebbingflow.persona_manager import PersonaManager
            from app.adapters.memory.ebbingflow.saga_manager import SagaManager
            from app.adapters.memory.ebbingflow.vector_storer import VectorStorer

            graph_store = SQLiteGraphStore(config.memory_db_path)
            persona_store = SQLitePersonaStore(config.memory_db_path)

            event_extractor = EventExtractor()
            persona_manager = PersonaManager(persona_store=persona_store)
            saga_manager = SagaManager()
            vector_storer = VectorStorer()

            consolidation_pipeline = ConsolidationPipeline(
                buffer_manager=buffer_manager,
                event_extractor=event_extractor,
                persona_manager=persona_manager,
                saga_manager=saga_manager,
                vector_storer=vector_storer,
                polish_bridge=polish_bridge,
                graph_store=graph_store,
                persona_store=persona_store,
            )
        except Exception as exc:
            logger.warning("consolidation_pipeline_init_failed: %s", exc)
            consolidation_pipeline = None

        memory_port = MemoryEngine(
            config=mem_config,
            buffer_manager=buffer_manager,
            intent_classifier=intent_classifier,
            llm_bridge=llm_bridge,
            retrieval_pipeline=retrieval_pipeline,
            surface_generator=surface_generator,
            consolidation_pipeline=consolidation_pipeline,
        )
        memory_recall_executor = MemoryRecallExecutor(memory_port)

        # 注册 memory_recall 工具（仅主动唤醒回合暴露）
        from app.adapters.tools.memory_recall_tool import MEMORY_RECALL_DEF
        tool_registry.register(MEMORY_RECALL_DEF, memory_recall_executor)
        tool_registry.register_for_wake_only(MEMORY_RECALL_DEF.name)

    # 3. 应用编排 — 注入 async model client + tool_registry + model_tool_loop + memory_port
    turn_runner = TurnRunner(
        sample_reader=sample_reader,
        context_builder=context_builder,
        model_client=async_model_client,
        outbox_store=outbox_store,
        tool_registry=tool_registry,
        model_tool_loop=model_tool_loop,
        memory_port=memory_port,
    )

    # 3b. 主动回合组件
    active_turn_gate = ActiveTurnGate()
    wake_start_policy = WakeStartPolicy()
    wake_planner = WakePlanner(store=wake_job_store, policy=schedule_admission_policy)
    wake_controller = WakeController(
        store=wake_job_store,
        gate=active_turn_gate,
        start_policy=wake_start_policy,
        turn_runner=turn_runner,
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
    app.state.tool_registry = tool_registry
    app.state.tool_dispatcher = tool_dispatcher
    app.state.model_tool_loop = model_tool_loop
    app.state.wake_job_store = wake_job_store
    app.state.active_turn_gate = active_turn_gate
    app.state.wake_start_policy = wake_start_policy
    app.state.schedule_admission_policy = schedule_admission_policy
    app.state.wake_planner = wake_planner
    app.state.wake_controller = wake_controller
    app.state.turn_runner = turn_runner
    app.state.memory_port = memory_port

    return app
