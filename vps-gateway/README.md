# 沉的 VPS 中转网关

OpenAI Chat Completions 兼容的 API 网关服务。VPS 作为"沉"的持续运行主体，从四份只读 Sample 中读取状态，注入为 system message，转发对话请求；定时触发主动回合，把模型生成的主动消息写入 Outbox，通过 API 暴露给前端。

v3 引入认知记忆引擎：基于 ebbingflow 组件 vendoring，实现意图分类、多轨检索、记忆注入、表面生成和 2am 沉淀管线。`MEMORY_ENABLED=false` 时系统行为与 v2 完全一致。

## 总体架构

```
                    四份只读 Sample (JSON)                 认知记忆引擎 (v3)
                    ┌────────────────────────┐           ┌─────────────────────────────┐
                    │ identity / preferences  │           │ MemoryEngine                │
                    │ memories / working_state│           │  IntentClassifier           │
                    └───────────┬────────────┘           │  RetrievalPipeline (@4)     │
                                │                         │  SurfaceGenerator (@6)      │
                                │                         │  ConsolidationPipeline (2am)│
                                │                         │  BufferManager (@a/@d/@e)   │
                                │                         │  ebbingflow vendored (18)   │
                                │                         └──────────┬──────────────────┘
                                │                                    │
                    ┌───────────▼────────────┐                       │ recall / after_turn
┌──────────┐  POST  │ ChatController         │                       │
│ 前端/测试 │───────→│ 鉴权+解析 → UserTrigger│                       │
└──────────┘  /v1/  └──────────┬─────────────┘                       │
                               │                                       │
┌──────────┐  Timer           │                                       │
│Scheduler │──────────────────┤                                       │
└──────────┘                  ▼                                       │
                     ┌─────────────────────┐    memory_recall_text    │
                     │ TurnRunner          │←─────────────────────────┘
                     │  1. read_all()      │
                     │  2. recall() [v3]   │
                     │  3. build()         │
                     │  4. complete()      │
                     │  5. after_turn() [v3]│
                     └────┬──────────┬─────┘
                    被动返回 │          │ 主动结果
                          │          ▼
                          │  ┌──────────────┐
                          │  │ OutboxStore  │
                          │  │ (SQLite WAL) │
                          │  └──────┬───────┘
                          │         │
                          │  GET /v1/outbox
                          │         ▼
                          │   前端/测试端
                          ▼
                   OpenAI Chat Response
```

### 依赖方向

```
HTTP/Scheduler/Memory 适配器  (app/adapters/)
        ↓
应用层 TurnRunner + MemoryEngine  (app/application/)
        ↓
领域接口 (端口)                 (app/domain/ports/)
        ↓
领域模型                       (app/domain/models/)
        ↓
基础设施实现                    (app/infrastructure/, app/adapters/)
```

禁止反向依赖：ContextBuilder 不读文件，TurnRunner 不执行 SQL，Sample Repository 不调用模型，OutboxController 不触发 TurnRunner，vendored ebbingflow 组件不引用 vps-gateway config.py。

---

## 数据流详解

### 流 1：被动回合（用户发消息）

```
前端 → POST /v1/chat/completions
  │  Headers: Authorization: Bearer <GATEWAY_API_KEY>
  │  Body: { model, messages, temperature?, max_completion_tokens?, stream }
  │
  ▼
ChatController.handle_chat_completions()
  │  1. authenticate_gateway_request() → 校验 Bearer token
  │  2. parse_chat_request(body) → 校验 stream/消息体/token字段冲突
  │  3. 构造 UserTrigger { request_id, chat_request }
  │  4. X-Memory-Mode: new_window → 标记新窗口衔接
  │
  ▼
TurnRunner.run_user_turn(UserTrigger)
  │  1. sample_reader.read_all() → AllSamples
  │  2. [v3] memory_port.recall() → MemoryRecall (意图分类 → @4/@6 路径)
  │     或 memory_port.recall_new_window() → 读最近 15 条 @d
  │  3. context_builder.build(samples, trigger, memory_recall_text) → PreparedTurn
  │  4. model_client.complete(ModelCompletionInput) → ChatCompletionResponse
  │  5. [v3] memory_port.after_turn() → 对话写入 @a 缓冲区
  │  6. 返回 response
  │
  ├─▶ [v3] MemoryEngine.recall(turn_id, user_text, mode="user")
  │     1. IntentClassifier.classify(user_text)
  │        → 规则层优先 → LLM 兜底 → 置信度路由
  │     2. @4 查询路径 (intent=query):
  │        R2 嵌入 → R3 并行多轨检索(向量+BM25+图谱) → R4 HybridScorer 重排
  │        → R5 LLM 生成 → R7 润色 → 写入 @d → 返回润色文本
  │        超时降级 γ: timeout 后返回 degraded, 后台 Task 继续运行
  │     3. @6 无查询路径 (intent=no_query):
  │        SurfaceGenerator 选材(@d) → LLM 生成 → 润色 → 写入 @e(已读即删) → 返回
  │     4. 返回 MemoryRecall { mode, text, source_recall_ids, metadata }
  │
  ├─▶ ContextBuilder.build(AllSamples, UserTrigger, memory_recall_text)
  │     1. sort_memories() → priority 降序, created_at 升序
  │     2. [v3] memory_recall_text 有值时替换 <memories> 来源
  │     3. fit_within_budget() → 字符预算截断
  │     4. render_state_xml() → <chen_state> 块, XML 转义
  │     5. 合并: [base_prompt + state_xml + frontend_instructions]
  │     6. 被动回合: [server_system] + conversation_messages
  │
  ▼
ChatController → JSONResponse(200, response.to_dict())
  │  错误映射: 503/504/502/401/400
  ▼
前端 ← OpenAI 兼容 JSON
```

### 流 2：主动回合（定时器触发）

```
LocalScheduler._loop()  (daemon 线程)
  │  触发: 每 ACTIVE_TURN_INTERVAL_MINUTES 分钟
  │  1. _floor_to_interval(now) → 对齐到时间槽
  │  2. trigger_id = "timer:" + slot.isoformat()  (幂等键)
  │  3. _active_lock.locked() → 跳过 (上次未结束)
  │  4. 构造 TimerTrigger { trigger_id, fired_at, instruction }
  │
  ▼
TurnRunner.run_wake_turn(TimerTrigger)
  │  1. sample_reader.read_all() → AllSamples
  │  2. [v3] memory_port.recall(turn_id, instruction, mode="wake")
  │     → @6 无查询路径 + 暴露 memory_recall 工具
  │  3. context_builder.build(samples, trigger, memory_recall_text)
  │  4. [v3] ToolRegistry.schemas_for_wake() → 包含 memory_recall 工具
  │  5. model_client.complete() 或 ModelToolLoop → ChatCompletionResponse
  │  6. _handle_active_turn() → Outbox
  │  7. [v3] memory_port.after_turn() → 写入 @a
  │
  ▼
TurnRunner._handle_active_turn(TimerTrigger, response, prepared)
  │  1. content == "<NO_MESSAGE>" → outcome="no_message", 不写 Outbox
  │  2. 正常文本 → OutboxStore.enqueue_once()
  │  3. 异常不传播 → active_turn_failed
  ▼
(无 HTTP 响应, 结果在 Outbox 中)
```

### 流 3：Outbox 查询

```
前端 → GET /v1/outbox?after=0&limit=20
  │  Headers: Authorization: Bearer <GATEWAY_API_KEY>
  │
  ▼
OutboxController.handle_outbox_query()
  │  1. authenticate_gateway_request()
  │  2. after = 非负整数 (默认0), limit = clamp(1, 100)
  │  3. outbox_store.list_after(after, limit)
  │  输出: { items: [...], next_cursor: int }
  ▼
前端 ← { items: [{cursor, event_id, trigger_id, created_at, content, metadata}], next_cursor }
```

### 流 4 [v3]：新窗口衔接

```
前端 → POST /v1/chat/completions
  │  Headers: X-Memory-Mode: new_window
  │
  ▼
ChatController → UserTrigger (携带 new_window 标记)
  │
  ▼
TurnRunner.run_user_turn()
  │  [v3] memory_port.recall_new_window(turn_id)
  │     1. 读最近 15 条 @d (已读不删)
  │     2. 拼接为上下文文本
  │     3. 返回 MemoryRecall { mode="new_window", text }
  │  → ContextBuilder 注入为 memory_recall_text
  │  → 主 LLM 感知到历史记忆上下文
  ▼
前端 ← OpenAI 兼容 JSON (包含记忆衔接)
```

### 流 5 [v3]：2am 沉淀管线

```
MemoryEngine 内部定时器 (独立后台任务, 非 WakeJob)
  │  触发: 每天凌晨 memory_consolidation_hour (默认 2:00)
  │  不受 08:00—24:00 唤醒窗口约束
  │
  ▼
ConsolidationPipeline.run(consolidation_id)
  │  W1: 扫描 @a 缓冲区 → 提取事件 (EventExtractor + LLMBridge)
  │  W2: 向量化事件 → 写入 ChromaDB (VectorStorer)
  │  W3: 构建知识图谱 → SQLite 递归 CTE (GraphStore)
  │  W4: 更新 Persona (PersonaManager → PersonaStore)
  │  W5: 整合 Saga 叙事 (SagaManager)
  │  W6: 持久化 → 写入图谱/向量/Persona
  │  清理: 清空 @a/@d 缓冲区 (短事务, 不影响正在进行的 recall)
  │
  │  W1 失败 → 中断管线, 不清空 @a/@d
  │  W2-W6 部分失败 → 已完成步骤持久化, 清空 @a/@d
  │  @a 为空 → 跳过管线
  ▼
日志: consolidation_started/completed/failed, buffer_cleared
```

### 流 6 [v3]：memory_recall 工具（主动唤醒回合）

```
主动唤醒回合中主 LLM 调用 memory_recall 工具
  │
  ▼
MemoryRecallExecutor.execute(query)
  │  1. 调用 memory_port.recall_as_tool(turn_id, query)
  │  2. MemoryEngine 触发 @4 查询路径
  │     → R2-R7 完整检索+生成+润色流程
  │  3. 返回润色后的 @d 文本
  │  约束: 最多 5 轮/10 次/单工具 15 秒 (沿用 v2 工具循环)
  ▼
主 LLM 获得记忆检索结果 → 继续生成
```

---

## 模块与代码索引

### 领域层 — `app/domain/`

| 文件 | 职责 | 核心类型 |
|------|------|----------|
| `models/sample.py` | Sample 统一外壳 + 校验 + 错误 | `SampleEnvelope`, `SampleReadError`, `validate_sample()` |
| `models/identity.py` | 身份数据模型 | `IdentityData` |
| `models/preferences.py` | 偏好数据模型 | `PreferencesData` |
| `models/memories.py` | 记忆数据模型 | `MemoryItem`, `MemoriesData` |
| `models/working_state.py` | 工作状态模型 | `WorkingStateData` |
| `models/trigger.py` | 触发器 | `UserTrigger`, `TimerTrigger` |
| `models/turn.py` | 回合数据 | `ChatMessage`, `PreparedTurn`, `ModelCompletionInput` |
| `models/context_builder.py` | 纯计算：Sample → messages [v3: +memory_recall_text] | `ContextBuilder`, `xml_escape()`, `sort_memories()`, `fit_within_budget()` |
| `models/chat_completion.py` | OpenAI 请求/响应解析 | `parse_chat_request()`, `ChatCompletionResponse` |
| `models/errors.py` | 上游错误类型 | `UpstreamError`, `UpstreamTimeout` |
| `models/outbox.py` | Outbox 数据模型 | `NewOutboxMessage`, `OutboxMessage`, `OutboxPage` |
| `models/tool.py` [v2.5] | 工具定义模型 | `ToolDefinition`, `ToolExecutor` |
| `models/memory.py` [v3] | 记忆数据模型 | `MemoryRecall`, `SurfaceEntry`, `RawEntry`, `RecallEntry` |
| `ports/sample_reader.py` | Sample 读取端口 | `SampleReader`(ABC), `AllSamples` |
| `ports/sample_publisher.py` | Sample 发布端口（预留） | `SamplePublisher`(ABC) |
| `ports/model_client.py` | 模型调用端口 | `ModelClient`(ABC) |
| `ports/outbox_store.py` | Outbox 存储端口 | `OutboxStore`(ABC) |
| `ports/graph_store.py` [v3] | 图谱存储端口 | `GraphStore`(ABC) |
| `ports/persona_store.py` [v3] | Persona 存储端口 | `PersonaStore`(ABC) |
| `ports/buffer_store.py` [v3] | 缓冲区存储端口 | `BufferStore`(ABC) |
| `ports/memory_engine.py` [v3] | 记忆引擎端口 + 配置 | `MemoryPort`(ABC), `MemoryEngineConfig` |

### 应用层 — `app/application/`

| 文件 | 职责 | 核心类型 |
|------|------|----------|
| `turn_runner.py` | 唯一回合编排器 [v3: +memory_port注入] | `TurnRunner.run_user_turn()`, `run_wake_turn()`, `ActiveTurnResult` |
| `app.py` | 依赖注入工厂 [v3: +记忆引擎完整接线] | `create_app(config) → FastAPI` |
| `model_tool_loop.py` [v2.5] | 工具循环编排 | `ModelToolLoop` |
| `wake_controller.py` [v2.5] | 唤醒控制 | `WakeController` |
| `wake_planner.py` [v2.5] | 唤醒计划 | `WakePlanner` |
| `active_turn_gate.py` [v2.5] | 主动回合并发门 | `ActiveTurnGate` |
| `wake_start_policy.py` [v2.5] | 唤醒启动策略 | `WakeStartPolicy` |
| `schedule_admission_policy.py` [v2.5] | 调度准入策略 | `ScheduleAdmissionPolicy` |
| `memory/memory_engine.py` [v3] | 记忆引擎核心 | `MemoryEngine` |
| `memory/buffer_manager.py` [v3] | 缓冲区管理器 | `BufferManager` |
| `memory/intent_classifier.py` [v3] | 意图分类（规则+LLM+校准） | `IntentClassifier` |
| `memory/retrieval_pipeline.py` [v3] | @4 检索管线（R2-R7+降级γ） | `RetrievalPipeline` |
| `memory/polish_bridge.py` [v3] | 润色桥接 | `PolishBridge` |
| `memory/surface_generator.py` [v3] | @6 表面生成+随机选择 | `SurfaceGenerator`, `RandomSurfaceSelector` |
| `memory/consolidation_pipeline.py` [v3] | 2am 沉淀管线（W1-W6） | `ConsolidationPipeline` |

### 适配器层 — `app/adapters/`

| 文件 | 职责 | 核心类型 |
|------|------|----------|
| `samples/file_sample_repository.py` | 从 JSON 文件读取 Sample | `FileSampleRepository` |
| `models/openai_upstream_client.py` | httpx 调用上游模型 | `OpenAIUpstreamClient` |
| `outbox/sqlite_outbox_store.py` | SQLite 持久化 Outbox | `SQLiteOutboxStore` |
| `http/chat_controller.py` | POST /v1/chat/completions | `create_chat_router()` |
| `http/outbox_controller.py` | GET /v1/outbox | `create_outbox_router()` |
| `http/auth.py` | Bearer token 鉴权 | `authenticate_gateway_request()` |
| `http/errors.py` | OpenAI 兼容错误格式 | `openai_error()` |
| `scheduler/local_scheduler.py` | 定时主动回合 | `LocalScheduler` |
| `tools/registry.py` [v2.5] | 工具注册表 [v3: +register_for_wake_only] | `ToolRegistry` |
| `tools/tool_dispatcher.py` [v2.5] | 工具分发器 | `ToolDispatcher` |
| `tools/wake_tool_definitions.py` [v2.5] | 唤醒工具定义 | `WAKE_TOOL_DEFINITIONS` |
| `tools/wake_tools.py` [v2.5] | 唤醒工具执行器 | `ScheduleWakeupExecutor` 等 |
| `tools/memory_recall_tool.py` [v3] | memory_recall 工具定义+执行器 | `MEMORY_RECALL_DEFINITION`, `MemoryRecallExecutor` |
| `wakeups/sqlite_wake_job_store.py` [v2.5] | SQLite 唤醒任务存储 | `SQLiteWakeJobStore` |
| `memory/sqlite_buffer_store.py` [v3] | SQLite 缓冲区适配器 (@a/@d/@e) | `SQLiteBufferStore` |
| `memory/sqlite_graph_store.py` [v3] | SQLite 图谱适配器 (递归 CTE) | `SQLiteGraphStore` |
| `memory/sqlite_persona_store.py` [v3] | SQLite Persona 适配器 | `SQLitePersonaStore` |
| `memory/chroma_vector_store.py` [v3] | ChromaDB 向量存储初始化 | `init_chromadb()` |
| `memory/ebbingflow/` [v3] | ebbingflow vendored 组件 (18 个) | 见下表 |

### ebbingflow Vendored 组件 — `app/adapters/memory/ebbingflow/`

| 文件 | 来源 | 类型 | 说明 |
|------|------|------|------|
| `llm_bridge.py` | bridge/llm.py | 适配 | 解耦 token_monitor → 可选回调; +classify_intent/generate/embed 真实 API |
| `knowledge_engine.py` | memory/knowledge_engine.py | 适配 | Neo4j → SQLite 递归 CTE; 配置注入 |
| `persona_manager.py` | memory/identity/manager.py | 适配 | Neo4j → SQLite; 配置注入 |
| `sql_pool.py` | memory/sql/pool.py | 适配 | asyncpg → sqlite3 短连接; 连接池简化 |
| `event_slots.py` | memory/event/slots.py | 直接复用 | 事件槽位定义 |
| `hybrid_scorer.py` | memory/scoring/hybrid_scorer.py | 直接复用 | 混合评分器 |
| `identity_canonical.py` | memory/identity/canonical.py | 直接复用 | 身份规范化 |
| `identity_schema.py` | memory/identity/schema.py | 直接复用 | 身份 Schema |
| `identity_state_reducer.py` | memory/identity/state_reducer.py | 直接复用 | 状态归约器 |
| `identity_resolver.py` | memory/identity/resolver.py | 直接复用 | 实体解析器 |
| `identity_inference.py` | memory/identity/inference.py | 直接复用 | 身份推理 |
| `identity_evolution.py` | memory/identity/evolution.py | 直接复用 | 身份演化 (stub) |
| `identity_conflict_resolver.py` | memory/identity/conflict_resolver.py | 直接复用 | 冲突解决器 |
| `event_extractor.py` | memory/event/extractor.py | 直接复用 | 事件提取器 |
| `saga_manager.py` | memory/integration/saga_manager.py | 直接复用 | Saga 叙事管理器 |
| `episode_manager.py` | memory/integration/episode_manager.py | 直接复用 | Episode 管理器 |
| `vector_storer.py` | memory/vector/storer.py | 直接复用 | 向量存储器 (ChromaDB) |
| `vector_retriever.py` | memory/vector/retriever.py | 直接复用 | 向量检索器 |
| `sql_event_repository.py` | memory/sql/event_repository.py | 直接复用 | SQL 事件仓库 |
| `_config_stub.py` [v3 新增] | — | 新开发 | 从环境变量读取 8 模型配置 |

### 基础设施 — `app/infrastructure/`

| 文件 | 职责 |
|------|------|
| `config.py` | `Config.load_from_env()`, `Config.validate()` [v3: +28 个 MEM_* 字段] |
| `logging.py` | `get_logger()` — 结构化日志 |

### 启动入口 — `app/main.py`

加载 `.env` → `Config.load_from_env()` → `create_app(config)` → `uvicorn.run()`

### Sample 数据 — `samples/`

| 文件 | 内容 | v3 变化 |
|------|------|---------|
| `identity.sample.json` | name, self_description, values, boundaries, relationship_definition | 不变 |
| `preferences.sample.json` | communication_preferences, stable_likes, stable_dislikes, interaction_rules | 不变 |
| `memories.sample.json` | items[{id, content, category, priority, created_at}] | 降级为种子数据（首次初始化导入 @a） |
| `working_state.sample.json` | current_focus, emotion_summary, pending_items, next_wake_at | 不变 |

---

## 环境配置

复制 `.env.example` 为 `.env`，填入实际值：

### v2 基础配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `GATEWAY_HOST` | 监听地址 | `127.0.0.1` |
| `GATEWAY_PORT` | 监听端口 | `8000` |
| `GATEWAY_API_KEY` | 网关鉴权密钥 | (必填) |
| `UPSTREAM_BASE_URL` | 上游模型 API 地址 | (必填) |
| `UPSTREAM_API_KEY` | 上游模型 API Key | (必填) |
| `UPSTREAM_MODEL` | 上游模型名称 | (必填) |
| `UPSTREAM_TIMEOUT_SECONDS` | 上游超时 | `30` |
| `UPSTREAM_TOKEN_LIMIT_FIELD` | token 限制字段名 | `max_completion_tokens` |
| `SAMPLE_DIRECTORY` | Sample JSON 目录 | `./samples` |
| `MEMORY_CHAR_BUDGET` | 记忆字符预算 | `12000` |
| `OUTBOX_DATABASE_PATH` | SQLite 文件路径 | `./data/outbox.sqlite3` |
| `ACTIVE_TURN_ENABLED` | 启用主动回合 | `true` |
| `ACTIVE_TURN_INTERVAL_MINUTES` | 主动回合间隔(分钟) | `60` |
| `ACTIVE_TURN_INSTRUCTION` | 主动回合指令文本 | (见 .env) |
| `DEFAULT_TEMPERATURE` | 默认温度 | `0.7` |
| `DEFAULT_MAX_OUTPUT_TOKENS` | 默认最大输出 token | `1200` |

### v3 记忆引擎配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MEMORY_ENABLED` | 启用记忆引擎（false 时行为与 v2 一致） | `false` |
| `MEMORY_DB_PATH` | 记忆 SQLite 路径 | `./data/memory.sqlite3` |
| `MEMORY_RETRIEVAL_TIMEOUT` | @4 检索超时(秒) | `20` |
| `MEMORY_SURFACE_INTERVAL` | @e 周期生成间隔(秒) | `3600` |
| `MEMORY_CONSOLIDATION_HOUR` | 2am 沉淀时间(小时) | `2` |
| `MEM_EMBED_TYPE` | 嵌入类型（local/api） | `local` |
| `MEM_EMBED_MODEL` | 本地嵌入模型名 | `paraphrase-multilingual-MiniLM-L12-v2` |
| `MEM_INTENT_BASE_URL` | 意图分类 LLM 端点 | (空) |
| `MEM_INTENT_API_KEY` | 意图分类 LLM Key | (空) |
| `MEM_INTENT_MODEL` | 意图分类 LLM 模型 | (空) |
| `MEM_GEN_BASE_URL` | 生成 LLM 端点 | (空) |
| `MEM_GEN_API_KEY` | 生成 LLM Key | (空) |
| `MEM_GEN_MODEL` | 生成 LLM 模型 | (空) |
| `MEM_SURF_BASE_URL` | 表面生成 LLM 端点 | (空) |
| `MEM_SURF_API_KEY` | 表面生成 LLM Key | (空) |
| `MEM_SURF_MODEL` | 表面生成 LLM 模型 | (空) |
| `MEM_EXTRACT_BASE_URL` | 事件提取 LLM 端点 | (空) |
| `MEM_EXTRACT_API_KEY` | 事件提取 LLM Key | (空) |
| `MEM_EXTRACT_MODEL` | 事件提取 LLM 模型 | (空) |
| `MEM_PERSONA_BASE_URL` | Persona LLM 端点 | (空) |
| `MEM_PERSONA_API_KEY` | Persona LLM Key | (空) |
| `MEM_PERSONA_MODEL` | Persona LLM 模型 | (空) |
| `MEM_SAGA_BASE_URL` | Saga LLM 端点 | (空) |
| `MEM_SAGA_API_KEY` | Saga LLM Key | (空) |
| `MEM_SAGA_MODEL` | Saga LLM 模型 | (空) |
| `MEM_POLISH_BASE_URL` | 润色 LLM 端点 | (空) |
| `MEM_POLISH_API_KEY` | 润色 LLM Key | (空) |
| `MEM_POLISH_MODEL` | 润色 LLM 模型 | (空) |

---

## 运行与测试

```bash
# 安装依赖
uv sync

# 运行测试 (541 passed + 3 skipped)
UPSTREAM_BASE_URL="" uv run pytest

# 启动服务（MEMORY_ENABLED=false，行为与 v2 一致）
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 启动服务（MEMORY_ENABLED=true，记忆引擎激活）
MEMORY_ENABLED=true uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 真实 API 冒烟测试（需要 .env 中配置 8 个 MEM_* 模型凭据）
PYTHONPATH=. uv run python scripts/smoke_test_memory.py
```

---

## 当前实现状态

### v2 里程碑（基线）

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | 四份 Sample + FileSampleRepository | 完成 |
| M2 | ContextBuilder + OpenAIUpstreamClient | 完成 |
| M3 | POST /v1/chat/completions + TurnRunner | 完成 |
| M4 | SQLiteOutboxStore + GET /v1/outbox | 完成 |
| M5 | LocalScheduler + 主动回合调度 | 完成 |
| M6 | 集成部署 + 真实 API 冒烟测试 | 完成 |
| M7 | 并发控制 + 工具循环 + 唤醒系统 | 完成 |

### v3 里程碑（认知记忆引擎）

| 里程碑 | 内容 | 新增测试 | 状态 |
|--------|------|----------|------|
| M0 | app.py v2 组件接线补全（ToolRegistry/WakeController/WakePlanner/ModelToolLoop） | 12 | 完成 |
| M1 | 记忆基础设施（GraphStore/PersonaStore/BufferStore 端口 + SQLite 适配器 + ChromaDB） | 27 | 完成 |
| M2 | ebbingflow 组件 vendoring（15 直接复用 + 3 适配 + LLMBridge 解耦） | 49 | 完成 |
| M3 | MemoryPort + MemoryEngine 骨架 + BufferManager | 23 | 完成 |
| M4 | IntentClassifier（规则层 + LLM 兜底 + 周期校准） | 18 | 完成 |
| M5 | @4 查询路径完整链路（R2-R7 + 超时降级 γ + 润色） | 9 | 完成 |
| M6 | @6 无查询路径 + SurfaceGenerator + RandomSurfaceSelector | 9 | 完成 |
| M7 | memory_recall 工具 + 主动回合工具暴露 + 新窗口衔接 | 7 | 完成 |
| M8 | ConsolidationPipeline（W1-W6 + 清理） | 4 | 完成 |
| M9 | ContextBuilder 适配 + TurnRunner 注入 MemoryPort + app.py v3 接线 | 3 | 完成 |
| M10 | 全量回归 + 真实 API 冒烟 + 操作性冒烟 + 向后兼容验证 | — | 完成 |

全量回归：541 passed + 3 skipped（v2 基线 380 + v3 新增 161）
真实 API 冒烟测试：PASS（DeepSeek，意图分类 + @4 查询路径 + @6 无查询路径 + 新窗口衔接 + ContextBuilder 注入）
MEMORY_ENABLED=false 向后兼容：验证通过

---

## 记忆引擎缓冲区模型

| 区域 | 内容 | 读写规则 | 清理时机 |
|------|------|----------|----------|
| @a | 原始对话缓冲（跨平台跨窗口） | after_turn 追加 | 2am 沉淀后清空 |
| @d | recall 生成结果（润色后） | @4/@6 写入; 已读不删 | 2am 沉淀后清空 |
| @e | 表面生成内容 | @6 写入; 已读即删 | 读取时删除 |

---

## 架构约束（不可违反）

### v2 不变量（保留 17 条）

1. **identity 不可降级** — 缺失/损坏时拒绝请求（503），不使用空值
2. **其余三类可降级** — 降级为 `version=0, source=fallback_empty`，记录警告
3. **trigger_id 幂等** — 同一 trigger_id 在 Outbox 中至多一条消息
4. **`<NO_MESSAGE>` 不写 Outbox** — 主动回合模型可拒绝输出
5. **XML 转义** — 所有 Sample 内容注入 system message 前必须转义
6. **记忆排序+截断** — priority 降序 → created_at 升序 → 字符预算截断
7. **不删除 Outbox 消息** — 查询不修改数据
8. **limit clamp** — Outbox 查询 limit 限制在 1..100
9. **API Key 不入 Sample/日志** — 上游 Key 只在 `OpenAIUpstreamClient` 中使用
10. **无硬编码绝对路径** — 所有路径来自环境变量
11. **本地与 VPS 同一套代码** — 只替换 `.env`
12. **前端 tools/tool_choice → 400** — client_tools_not_allowed
13. **工具循环约束** — 最多 5 轮/10 次/单工具 15 秒
14. **唤醒窗口** — 08:00—24:00（2am 沉淀不受此约束）
15. **主动回合串行** — 两个主动回合永远串行；冲突任务 expired
16. **用户回合与一个主动回合可并行**
17. **部署端口固定 5000，本地开发默认 8000**

### v3 新增不变量（11 条）

18. **MEMORY_ENABLED=false 向后兼容** — 系统行为与 v2 完全一致
19. **主 LLM 不感知记忆引擎** — 记忆注入是 system message 文本替换
20. **@4 和 @6 互斥** — 同一回合只读取单个区域，不跨区
21. **@e 已读即删；@d 已读不删** — 仅 2am 清空
22. **@a 跨平台跨窗口** — 不分前端不分对话窗口；2am 随沉淀清空
23. **memory_recall 工具仅主动唤醒回合暴露** — 用户回合不暴露
24. **memories.sample.json 降级为种子数据** — MemoryEngine 激活后 `<memories>` 来源由记忆引擎接管
25. **意图分类规则优先** — LLM 兜底仅在不明确时触发；周期校准不参与实时路由
26. **@4 超时降级 γ** — 后台 asyncio.Task 继续运行，不阻塞主 LLM 请求
27. **vendored ebbingflow 不引用 config.py** — 所有配置通过构造注入
28. **Neo4j 适配为 SQLite 递归 CTE** — 不保留 Neo4j driver 依赖
