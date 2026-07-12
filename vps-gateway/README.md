# 沉的 VPS 中转网关
OpenAI Chat Completions 兼容的 API 网关服务。VPS 作为"沉"的持续运行主体，从四份只读 Sample 中读取状态，注入为 system message，转发对话请求；定时触发主动回合，把模型生成的主动消息写入 Outbox，通过 API 暴露给前端。

v3 引入认知记忆引擎：在 v2 基线上增量添加 ebbingflow 记忆组件、ChromaDB 向量检索、SQLite 递归 CTE 图谱、意图分类、多轨检索重排、@e 周期生成、2am 沉淀管线。MEMORY_ENABLED=false 时系统行为与 v2 完全一致。

## 总体架构
```
                        四份只读 Sample (JSON)
                        ┌────────────────────────────┐
                        │ identity / preferences      │
                        │ memories / working_state    │
                        └─────────────┬──────────────┘
                                      │ FileSampleRepository.read_all()
                                      ▼
┌────────────┐  POST /v1/chat/completions  ┌─────────────────────┐
│ 前端/测试端 │ ──────────────────────────→ │ ChatController      │
└────────────┘                             └──────────┬──────────┘
                                           鉴权+解析 → UserTrigger
┌────────────┐  TimerTrigger (定时)                    │
│ Scheduler  │ ───────────────────────────────────────→ │
└────────────┘                                        ▼
                                           ┌─────────────────────┐
                                           │ TurnRunner          │
                                           │  1. read_all()      │
                                           │  2. recall() ──→ MemoryEngine (v3)
                                           │  3. build()         │    ├─ IntentClassifier
                                           │  4. complete()      │    ├─ RetrievalPipeline (@4)
                                           │  5. after_turn() ──→│    ├─ SurfaceGenerator (@e)
                                           └────┬──────────┬─────┘    ├─ ConsolidationPipeline (2am)
                                          被动返回 │          │ 主动结果  └─ BufferManager (@a/@d/@e)
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

### V3 认知记忆引擎——特殊指代速查

V3 架构文档使用一组 `@` / `R` / `W` / `γ` 特殊指代描述记忆引擎的数据区域、检索阶段和沉淀阶段。以下为完整含义标注，供快速理解架构文档和代码。

#### 缓冲区区域（`@` 前缀）

| 指代 | 全称 | 代码位置 | 含义 | 读写规则 |
|-----|------|---------|------|---------|
| `@a` | 记忆原料区 buffer_raw | `BufferStore.append_raw()` | 跨平台跨窗口的今日聊天记录副本，每回合结束后追加 | 每回合写；2am 沉淀后清空 |
| `@d` | 记忆缓存区 buffer_recall | `BufferStore.append_recall()` | @4 查询路径生成并润色后的记忆内容 | @4 写入；已读**不删**；新窗口衔接读最近 15 条；2am 清空 |
| `@e` | 记忆浮现区 buffer_surface | `BufferStore.append_surface()` | @e 周期生成器产出的关联记忆浮现 | @e 生成器写；@6 路径读后**即删** |

#### 查询路径阶段（`R` 前缀，@4 路径内部）

| 指代 | 阶段名 | 使用的模型 | 含义 |
|-----|--------|-----------|------|
| `R2` | 查询向量化 | MEM_EMBED | 将用户最后一条消息嵌入为查询向量 |
| `R3` | 并行多轨检索 | — | 同时执行向量检索（ChromaDB）、BM25 关键词检索（rank-bm25）、图谱遍历（SQLite 递归 CTE）、事件仓库查询 |
| `R4` | HybridScorer 重排 | — | 对 R3 各轨候选结果混合打分排序 |
| `R5` | @d 记忆生成 | MEM_GEN | LLM 基于 R4 排序结果 + 原始对话生成记忆文本（[MEMORY] 事实区 + [NARRATIVE] 叙事区） |
| `R6` | @e 浮现生成 | MEM_SURF | @e 周期生成器中 LLM 语义拼装 @d 原料（与 R2-R5 同属检索体系但独立运行） |
| `R7` | 润色 | MEM_POLISH | 对 R5 生成的原始 @d 内容执行文学化润色，保留准确信息仅改表达风格 |

#### 超时降级

| 指代 | 含义 |
|-----|------|
| `γ` | @4 检索管线 20s 超时后的降级策略：后台 `asyncio.Task` 继续运行不阻塞主 LLM 请求；前台返回 `mode=degraded` 的空记忆，`<memories>` 为空 |

#### 沉淀管线阶段（`W` 前缀，2am 独立后台任务）

| 指代 | 阶段名 | 使用的模型 | 含义 |
|-----|--------|-----------|------|
| `W1` | 事件/事实抽取 | MEM_EXTRACT | 从 @a 原料 + @d 缓存中提取结构化事件和事实 |
| `W2` | 人格观察与演化 | MEM_PERSONA | 基于 @a 和 W1 事件更新人格画像快照 |
| `W3` | 结构化事件校验 | — | 规则校验 + 可选 LLM 轻量校验 W1 输出 |
| `W4` | Saga 聚类归并 | MEM_SAGA | 将事件聚类归并到长期 Saga 主线 |
| `W5` | 消息向量化 | MEM_EMBED | 将 @a 消息批量向量化存入 ChromaDB |
| `W6` | 润色 | MEM_POLISH | 对 W2 人格快照和 W4 Saga 更新执行文学化润色 |

W1-W6 完成后清空 @a 和 @d 缓冲区。W1 失败时中断管线且不清空；W2-W6 部分失败时已完成步骤持久化、后续跳过、仍清空。

#### 其他特殊指代

| 指代 | 含义 |
|-----|------|
| `@4` | 查询路径：R2→R3→R4→R5→R7 完整链路，生成润色后 @d 内容写入缓冲区 |
| `@6` | 无查询路径：直接读 @e 浮现区内容拼接转发，替换 `<memories>` |
| `@e 周期生成器` | 独立后台任务，~15 分钟轮询（含随机感），扫描 @d → 随机选材 → LLM 语义拼装 → 润色 → 写 @e |
| `2am 沉淀管线` | 每日凌晨 2 点触发的 W1-W6 批处理管线，是 MemoryEngine 内部定时器，不受 WakeController 时间约束 |
| `X-Memory-Mode: new_window` | 新窗口衔接：请求头携带此值时，读取最近 15 条 @d 拼接为记忆文本，模拟无缝换窗 |
| `memory_recall 工具` | 仅主动唤醒回合暴露给主 LLM 的 VPS 工具，调用时触发 @4 完整流程，返回润色后 @d 内容；用户回合不暴露 |
| `MEMORY_ENABLED` | 环境变量开关。`false` 时记忆引擎完全不初始化，系统行为与 v2 完全一致 |

### 依赖方向
```
HTTP/Scheduler/Memory 适配器  (app/adapters/)
        ↓
应用层 TurnRunner / MemoryEngine  (app/application/)
        ↓
领域接口 (端口)         (app/domain/ports/)
        ↓
领域模型               (app/domain/models/)
        ↓
基础设施实现            (app/infrastructure/, app/adapters/)
```
禁止反向依赖：ContextBuilder 不读文件，TurnRunner 不执行 SQL，Sample Repository 不调用模型，OutboxController 不触发 TurnRunner，vendored ebbingflow 组件不引用 vps-gateway config.py。

---
## 数据流详解

### 流 1：被动回合（用户发消息）
```
前端 → POST /v1/chat/completions
  │  Headers: Authorization: Bearer <GATEWAY_API_KEY>
  │  Body: { model, messages, temperature?, max_completion_tokens?, stream }
  │  Optional: X-Memory-Mode: new_window (v3 新窗口衔接)
  │
  ▼
ChatController.handle_chat_completions()
  │  1. authenticate_gateway_request() → 校验 Bearer token
  │  2. parse_chat_request(body) → 校验 stream/消息体/token字段冲突
  │  3. 构造 UserTrigger { request_id, chat_request }
  │
  ▼
TurnRunner.run(UserTrigger)
  │  1. sample_reader.read_all() → AllSamples
  │  2. [v3] memory_port.recall(trigger, messages) → memory_recall_text
  │     ├─ MEMORY_ENABLED=false → None (跳过)
  │     ├─ X-Memory-Mode: new_window → 读最近 15 条 @d
  │     ├─ IntentClassifier → "query" → @4 路径 (R2-R7)
  │     └─ IntentClassifier → "no_query" → @6 路径 (读 @e)
  │  3. context_builder.build(samples, trigger, memory_recall_text)
  │     └─ memory_recall_text 有值时替换 <memories> 来源
  │  4. model_client.complete() → ChatCompletionResponse
  │  5. [v3] memory_port.after_turn(trigger, messages, response)
  │     └─ 追加 @a 原料
  │
  ▼
ChatController → JSONResponse(200, response.to_dict())
```

### 流 2：主动回合（定时器触发）
```
LocalScheduler._loop()  (daemon 线程)
  │  每 ACTIVE_TURN_INTERVAL_MINUTES 分钟触发
  │  1. _floor_to_interval(now) → 对齐到时间槽
  │  2. trigger_id = "timer:" + slot.isoformat() (幂等键)
  │  3. _active_lock.locked() → 跳过 (上次未结束)
  │
  ▼
TurnRunner.run(TimerTrigger)
  │  1. read_all() → AllSamples
  │  2. [v3] recall() → @6 路径 (主动回合默认无查询)
  │  3. build() → [server_system, timer_user_message]
  │  4. complete() → ChatCompletionResponse
  │  5. _handle_active_turn()
  │     ├─ content == "<NO_MESSAGE>" → outcome="no_message"
  │     └─ 正常文本 → OutboxStore.enqueue_once()
  │  6. [v3] after_turn() → 追加 @a
  │
  ▼
OutboxStore → GET /v1/outbox → 前端
```

### 流 3：主动唤醒回合（v3 新增）
```
WakeTrigger (主动唤醒)
  │  1. 伪用户输入 → IntentClassifier → @6 (明确无查询)
  │  2. memory_recall 工具暴露给主 LLM
  │  3. 主 LLM 自主调用 memory_recall(query) → 触发 @4 完整流程
  │  4. 返回润色后 @d 内容给主 LLM
  │  5. 主 LLM 基于记忆内容生成回复
  │
  ▼
TurnRunner → 正常回合流程
```

### 流 4：@e 周期生成（v3 后台任务）
```
SurfaceGenerator (独立后台 asyncio.Task)
  │  ~15 分钟轮询 (含随机感)
  │  1. 扫描 @d 缓冲区
  │  2. 随机选材 (预留 SurfaceSelector 接口)
  │  3. LLM (MEM_SURF) 语义拼装
  │  4. PolishBridge (MEM_POLISH) 润色
  │  5. 写入 @e 浮现区
  │
  ▼
@6 路径读取 @e → 替换 <memories> → 读取后即删
```

### 流 5：2am 沉淀管线（v3 后台任务）
```
ConsolidationPipeline (每日 02:00 触发)
  │  W1: EventExtractor (MEM_EXTRACT) → 事件/事实
  │  W2: PersonaManager (MEM_PERSONA) → 人格快照
  │  W3: 规则校验 → 校验通过/失败标记
  │  W4: SagaManager (MEM_SAGA) → Saga 归并
  │  W5: VectorStorer (MEM_EMBED) → ChromaDB 向量存储
  │  W6: PolishBridge (MEM_POLISH) → 人格/Saga 润色
  │  → 清空 @a/@d 缓冲区
  │
  ▼
持久化: SQLite (图谱/人格/Saga) + ChromaDB (向量)
```

### 流 6：Outbox 查询
```
前端 → GET /v1/outbox?after=0&limit=20
  │  Headers: Authorization: Bearer <GATEWAY_API_KEY>
  │
  ▼
OutboxController.handle_outbox_query()
  │  1. authenticate_gateway_request()
  │  2. after = 非负整数 (默认0), limit = clamp(1, 100)
  │  3. outbox_store.list_after(after, limit)
  │  → { items: [...], next_cursor: int }
  │  不删除消息
  │
  ▼
前端 ← { items, next_cursor }
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
| `models/context_builder.py` | 纯计算：Sample → messages | `ContextBuilder`, `xml_escape()`, `sort_memories()`, `fit_within_budget()` |
| `models/chat_completion.py` | OpenAI 请求/响应解析 | `parse_chat_request()`, `ChatCompletionResponse` |
| `models/errors.py` | 上游错误类型 | `UpstreamError`, `UpstreamTimeout` |
| `models/outbox.py` | Outbox 数据模型 | `NewOutboxMessage`, `OutboxMessage`, `OutboxPage` |
| `models/tool.py` | 工具定义与执行器接口 | `ToolDefinition`, `ToolExecutor` |
| `models/memory.py` | v3 记忆数据模型 | `MemoryRecall`, `MemorySurface`, `MemoryEngineConfig` |
| `ports/sample_reader.py` | Sample 读取端口 | `SampleReader`(ABC), `AllSamples` |
| `ports/sample_publisher.py` | Sample 发布端口（预留） | `SamplePublisher`(ABC) |
| `ports/model_client.py` | 模型调用端口 | `ModelClient`(ABC) |
| `ports/outbox_store.py` | Outbox 存储端口 | `OutboxStore`(ABC) |
| `ports/graph_store.py` | v3 图谱存储端口 | `GraphStore`(ABC) |
| `ports/persona_store.py` | v3 人格存储端口 | `PersonaStore`(ABC) |
| `ports/buffer_store.py` | v3 缓冲区端口 (@a/@d/@e) | `BufferStore`(ABC) |
| `ports/memory_engine.py` | v3 记忆引擎端口 | `MemoryPort`(ABC) |

### 应用层 — `app/application/`
| 文件 | 职责 | 核心类型 |
|------|------|----------|
| `turn_runner.py` | 回合编排器 | `TurnRunner.run()`, `ActiveTurnResult` |
| `app.py` | 依赖注入工厂 | `create_app(config) → FastAPI` |
| `model_tool_loop.py` | 工具循环 | `ModelToolLoop` |
| `wake_controller.py` | 主动唤醒控制 | `WakeController` |
| `wake_planner.py` | 唤醒计划 | `WakePlanner` |
| `active_turn_gate.py` | 主动回合并发门 | `ActiveTurnGate` |
| `wake_start_policy.py` | 唤醒启动策略 | `WakeStartPolicy` |
| `schedule_admission_policy.py` | 调度准入策略 | `ScheduleAdmissionPolicy` |
| `memory/memory_engine.py` | v3 记忆引擎编排器 | `MemoryEngine` |
| `memory/buffer_manager.py` | v3 @a/@d/@e 缓冲管理 | `BufferManager` |
| `memory/intent_classifier.py` | v3 意图分类（规则+LLM兜底+校准） | `IntentClassifier` |
| `memory/retrieval_pipeline.py` | v3 @4 查询路径 R2-R7 | `RetrievalPipeline` |
| `memory/polish_bridge.py` | v3 润色桥 (R7/W6) | `PolishBridge` |
| `memory/surface_generator.py` | v3 @e 周期生成 | `SurfaceGenerator`, `RandomSurfaceSelector` |
| `memory/consolidation_pipeline.py` | v3 2am 沉淀 W1-W6 | `ConsolidationPipeline` |

### 适配器层 — `app/adapters/`
| 文件 | 职责 | 核心类型 |
|------|------|----------|
| `samples/file_sample_repository.py` | 从 JSON 文件读取 Sample | `FileSampleRepository` |
| `models/openai_upstream_client.py` | httpx 调用上游模型 | `OpenAIUpstreamClient` |
| `outbox/sqlite_outbox_store.py` | SQLite 持久化 Outbox | `SQLiteOutboxStore` |
| `tools/registry.py` | 工具注册表 | `ToolRegistry` |
| `tools/tool_dispatcher.py` | 工具分发 | `ToolDispatcher` |
| `tools/wake_tools.py` | 唤醒工具 | `schedule_wakeup`, `list_wakeups`, `cancel_wakeup` |
| `tools/wake_tool_definitions.py` | 唤醒工具定义 | `WAKE_TOOL_DEFINITIONS` |
| `tools/memory_recall_tool.py` | v3 memory_recall 工具 | `MemoryRecallToolExecutor` |
| `wakeups/sqlite_wake_job_store.py` | 唤醒任务 SQLite 存储 | `SQLiteWakeJobStore` |
| `scheduler/local_scheduler.py` | 定时主动回合 | `LocalScheduler` |
| `http/chat_controller.py` | POST /v1/chat/completions | `create_chat_router()` |
| `http/outbox_controller.py` | GET /v1/outbox | `create_outbox_router()` |
| `http/auth.py` | Bearer token 鉴权 | `authenticate_gateway_request()` |
| `http/errors.py` | OpenAI 兼容错误格式 | `openai_error()` |
| `memory/sqlite_buffer_store.py` | v3 @a/@d/@e SQLite 适配器 | `SQLiteBufferStore` |
| `memory/sqlite_graph_store.py` | v3 图谱 SQLite 递归 CTE | `SQLiteGraphStore` |
| `memory/sqlite_persona_store.py` | v3 人格 SQLite 适配器 | `SQLitePersonaStore` |
| `memory/chroma_vector_store.py` | v3 ChromaDB 向量存储 | `ChromaVectorStore` |
| `memory/ebbingflow/` | v3 ebbingflow vendoring (15直接复用+3适配) | `LLMBridge`, `EventExtractor`, `PersonaManager`, `SagaManager`, `VectorStorer`, `HybridScorer` 等 |

### 基础设施 — `app/infrastructure/`
| 文件 | 职责 |
|------|------|
| `config.py` | `Config.load_from_env()`, `Config.validate()` — 含 v3 记忆引擎 28 个配置字段 |
| `logging.py` | `get_logger()` — 结构化日志 |

### 启动入口 — `app/main.py`
加载 `.env` → `Config.load_from_env()` → `create_app(config)` → `uvicorn.run()`

### Sample 数据 — `samples/`
| 文件 | 内容 |
|------|------|
| `identity.sample.json` | name, self_description, values, boundaries, relationship_definition |
| `preferences.sample.json` | communication_preferences, stable_likes, stable_dislikes, interaction_rules |
| `memories.sample.json` | items[{id, content, category, priority, created_at}] — v3 降级为种子数据 |
| `working_state.sample.json` | current_focus, emotion_summary, pending_items, next_wake_at |

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
| `MEMORY_ENABLED` | 启用记忆引擎 | `false` |
| `MEMORY_DB_PATH` | 记忆 SQLite 路径 | `./data/memory.sqlite3` |
| `MEMORY_RETRIEVAL_TIMEOUT` | @4 检索超时(秒) | `20.0` |
| `MEMORY_SURFACE_INTERVAL` | @e 生成周期(秒) | `3600.0` |
| `MEMORY_CONSOLIDATION_HOUR` | 2am 沉淀触发小时 | `2` |
| `MEM_EMBED_TYPE` | 嵌入方式 (local/api) | `local` |
| `MEM_EMBED_MODEL` | 嵌入模型名 | `paraphrase-multilingual-MiniLM-L12-v2` |
| `MEM_INTENT_*` | 意图分类模型 (base_url/api_key/model) | (空) |
| `MEM_GEN_*` | @d 记忆生成模型 (R5) | (空) |
| `MEM_SURF_*` | @e 浮现生成模型 (R6) | (空) |
| `MEM_EXTRACT_*` | 事件抽取模型 (W1) | (空) |
| `MEM_PERSONA_*` | 人格观察模型 (W2) | (空) |
| `MEM_SAGA_*` | Saga 归并模型 (W4) | (空) |
| `MEM_POLISH_*` | 文学润色模型 (R7/W6) | (空) |

---
## 运行与测试
```bash
# 安装依赖
uv sync

# 运行全量测试 (541 passed + 3 skipped)
uv run pytest

# 启动服务 (MEMORY_ENABLED=false, 行为同 v2)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 启动服务 (MEMORY_ENABLED=true, 记忆引擎激活)
MEMORY_ENABLED=true uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 真实 API 冒烟测试 (需配置 .env 中的 MEM_* 凭据)
PYTHONPATH=. uv run python scripts/smoke_test_memory.py
```

### v3 记忆引擎新增依赖
| 依赖 | 用途 |
|------|------|
| `chromadb` | 向量存储（ChromaDB 嵌入式模式） |
| `rank-bm25` | BM25 关键词检索 |
| `sentence-transformers` | 本地嵌入模型（MEM_EMBED_TYPE=local 时） |

---
## 当前实现状态

### v2 基线（M1-M7）
| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | 四份 Sample + FileSampleRepository | 完成 |
| M2 | ContextBuilder + OpenAIUpstreamClient | 完成 |
| M3 | POST /v1/chat/completions + TurnRunner | 完成 |
| M4 | SQLiteOutboxStore + GET /v1/outbox | 完成 |
| M5 | LocalScheduler + 主动回合调度 | 完成 |
| M6 | 集成部署 + 真实 API 冒烟测试 | 完成 |
| M7 | 唤醒系统 + 工具循环 + 并发隔离 | 完成 |

### v3 认知记忆引擎（M0-M10）
| 里程碑 | 内容 | 新增测试 | 状态 |
|--------|------|---------|------|
| M0 | app.py v2 组件接线补全 | 12 | 完成 |
| M1 | 记忆基础设施（端口+SQLite适配器+ChromaDB） | 27 | 完成 |
| M2 | ebbingflow 组件 vendoring（15直接复用+3适配） | 49 | 完成 |
| M3 | MemoryEngine 骨架 + BufferManager | 23 | 完成 |
| M4 | IntentClassifier（规则+LLM兜底+校准） | 18 | 完成 |
| M5 | @4 查询路径 R2-R7 + 超时降级 γ + 润色 | 9 | 完成 |
| M6 | @6 无查询路径 + SurfaceGenerator | 9 | 完成 |
| M7 | memory_recall 工具 + 主动回合 + 新窗口衔接 | 7 | 完成 |
| M8 | ConsolidationPipeline W1-W6 | 4 | 完成 |
| M9 | ContextBuilder 适配 + TurnRunner 注入 + app.py 接线 | 3 | 完成 |
| M10 | 全量回归 + 向后兼容 + 真实 API 冒烟 | — | 完成 |

全量回归：541 passed + 3 skipped
真实 API 冒烟：PASS (DeepSeek，意图分类 + @4 查询路径 + @6 无查询路径 + 新窗口衔接 + ContextBuilder 注入)
MEMORY_ENABLED=false 向后兼容：PASS

---
## 架构约束（不可违反）

### v2 不变量（#1-#18，保留 17 条）
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
12. **前端 tools/tool_choice → 400** — 不允许前端指定工具
13. **工具循环约束** — 最多 5 轮、10 次调用、单工具 15 秒
14. **主动回合串行** — 两个主动回合永远串行
15. **唤醒时间约束** — 08:00-24:00 内允许
16. **Outbox 事件不通知长轮询** — 写入失败不通知
17. **单进程单实例** — 禁止多 worker

### v3 新增不变量（#18-#28）
18. **主 LLM 不感知记忆引擎** — 记忆注入是 system message 层文本替换
19. **@4 和 @6 互斥** — 同一回合只读取单个区域，不跨区
20. **@e 已读即删；@d 已读不删** — 仅 2am 清空
21. **@a 跨平台跨窗口** — 不分前端不分对话窗口，2am 随沉淀清空
22. **memory_recall 仅主动唤醒回合暴露** — 用户回合不暴露
23. **memories.sample.json 降级为种子数据** — MemoryEngine 激活后 `<memories>` 来源由记忆引擎接管
24. **意图分类规则优先** — LLM 兜底仅在不明确时触发；周期校准不参与实时路由
25. **MEMORY_ENABLED=false 行为与 v2 一致**
26. **@4 超时降级 γ** — 后台继续，前台无记忆转发，不阻塞主 LLM
27. **2am 沉淀是内部定时器** — 不是 WakeJob，不受 08:00-24:00 约束
28. **vendored ebbingflow 不引用 config.py** — 所有配置通过构造注入；Neo4j 适配为 SQLite 递归 CTE
