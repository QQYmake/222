# 沉的 VPS 中转网关

OpenAI Chat Completions 兼容的 API 网关服务。VPS 作为"沉"的持续运行主体，从四份只读 Sample 中读取状态，注入为 system message，转发对话请求；定时触发主动回合，把模型生成的主动消息写入 Outbox，通过 API 暴露给前端。

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
                                           │  2. build()         │
                                           │  3. complete()      │
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
HTTP/Scheduler 适配器  (app/adapters/)
        ↓
应用层 TurnRunner       (app/application/)
        ↓
领域接口 (端口)         (app/domain/ports/)
        ↓
领域模型               (app/domain/models/)
        ↓
基础设施实现            (app/infrastructure/, app/adapters/)
```

禁止反向依赖：ContextBuilder 不读文件，TurnRunner 不执行 SQL，Sample Repository 不调用模型，OutboxController 不触发 TurnRunner。

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
  │  输入: Request headers + JSON body
  │  指令:
  │    1. authenticate_gateway_request() → 校验 Bearer token
  │    2. parse_chat_request(body) → 校验 stream/消息体/token字段冲突
  │    3. 构造 UserTrigger { request_id: uuid, chat_request: raw_body }
  │  输出: UserTrigger
  │
  ▼
TurnRunner.run(UserTrigger)
  │  输入: UserTrigger
  │  指令:
  │    1. sample_reader.read_all() → AllSamples
  │    2. context_builder.build(samples, trigger) → PreparedTurn
  │    3. model_client.complete(ModelCompletionInput) → ChatCompletionResponse
  │    4. trigger.type == "user" → 返回 response
  │  输出: ChatCompletionResponse
  │
  ├─▶ SampleReader.read_all()
  │     输入: 无 (固定路径映射)
  │     指令:
  │       1. read identity → 失败抛 SampleReadError (不降级)
  │       2. read preferences → 失败降级为空值
  │       3. read memories → 失败降级为空值
  │       4. read working_state → 失败降级为空值
  │     输出: AllSamples { identity, preferences, memories, working_state }
  │
  ├─▶ ContextBuilder.build(AllSamples, UserTrigger)
  │     输入: AllSamples + UserTrigger
  │     指令:
  │       1. sort_memories() → priority 降序, created_at 升序
  │       2. fit_within_budget() → 字符预算截断 (默认 12000)
  │       3. render_state_xml() → <chen_state> 块, XML 转义
  │       4. 提取前端 system message → <frontend_instructions>
  │       5. 合并: [base_prompt + state_xml + frontend_instructions]
  │       6. 被动回合: [server_system] + conversation_messages
  │     输出: PreparedTurn { messages: [ChatMessage], sample_versions: {type: int} }
  │
  ├─▶ ModelClient.complete(ModelCompletionInput)
  │     输入: messages + temperature + max_output_tokens
  │     指令:
  │       1. 构造上游 POST /chat/completions (stream=false)
  │       2. 使用服务器 UPSTREAM_API_KEY
  │       3. httpx.post() → 解析响应
  │       4. 失败抛 UpstreamError(502) / UpstreamTimeout(504)
  │     输出: ChatCompletionResponse { id, model, choices, usage }
  │
  ▼
ChatController → JSONResponse(200, response.to_dict())
  │  错误映射:
  │    SampleReadError → 503 state_unavailable
  │    UpstreamTimeout → 504 upstream_timeout
  │    UpstreamError   → 502 upstream_error
  │    AuthError        → 401 invalid_api_key
  │    StreamError      → 400 unsupported_stream
  │    字段冲突          → 400 conflicting_token_fields
  ▼
前端 ← OpenAI 兼容 JSON
```

### 流 2：主动回合（定时器触发）

```
LocalScheduler._loop()  (daemon 线程)
  │  触发: 每 ACTIVE_TURN_INTERVAL_MINUTES 分钟
  │  指令:
  │    1. _floor_to_interval(now) → 对齐到时间槽
  │    2. trigger_id = "timer:" + slot.isoformat()  (幂等键)
  │    3. _active_lock.locked() → 跳过 (上次未结束)
  │    4. 构造 TimerTrigger { trigger_id, fired_at, instruction }
  │
  ▼
TurnRunner.run(TimerTrigger)
  │  输入: TimerTrigger
  │  指令:
  │    1. sample_reader.read_all() → AllSamples (同被动回合)
  │    2. context_builder.build(samples, trigger)
  │       → 主动回合: [server_system, timer_user_message]
  │       → timer_user_message 包含 instruction + <NO_MESSAGE> 约定
  │    3. model_client.complete() → ChatCompletionResponse
  │    4. trigger.type == "timer" → _handle_active_turn()
  │
  ▼
TurnRunner._handle_active_turn(TimerTrigger, response, prepared)
  │  输入: 模型响应 + PreparedTurn
  │  指令:
  │    1. content = response.first_assistant_text().strip()
  │    2. content == "<NO_MESSAGE>" → outcome="no_message", 不写 Outbox
  │    3. 正常文本 → 构造 NewOutboxMessage → enqueue_once()
  │  输出: ActiveTurnResult { trigger_id, outcome, event_id? }
  │
  ├─▶ OutboxStore.enqueue_once(NewOutboxMessage)
  │     输入: { event_id: uuid, trigger_id, created_at, content, metadata }
  │     指令:
  │       1. INSERT ... ON CONFLICT(trigger_id) DO NOTHING
  │       2. SELECT WHERE trigger_id = ? → 返回已存在或刚插入的行
  │     输出: OutboxMessage { cursor, event_id, trigger_id, ... }
  │     幂等: 同一 trigger_id 重复调用返回相同行
  │
  ▼
Scheduler 日志: active_turn_completed { trigger_id, outcome }
  │  异常不传播 → active_turn_failed, 等待下一周期
  ▼
(无 HTTP 响应, 结果在 Outbox 中)
```

### 流 3：Outbox 查询

```
前端 → GET /v1/outbox?after=0&limit=20
  │  Headers: Authorization: Bearer <GATEWAY_API_KEY>
  │  Query: after (游标, 默认0), limit (默认20)
  │
  ▼
OutboxController.handle_outbox_query()
  │  输入: query_params { after, limit }
  │  指令:
  │    1. authenticate_gateway_request()
  │    2. after = 非负整数 (默认0)
  │    3. limit = 正整数 → clamp(1, 100)
  │    4. outbox_store.list_after(after, limit)
  │  输出: { items: [...], next_cursor: int }
  │
  ├─▶ OutboxStore.list_after(after_cursor, limit)
  │     输入: after_cursor (int), limit (int)
  │     指令:
  │       1. safe_limit = max(1, min(limit, 100))
  │       2. SELECT * WHERE cursor > ? ORDER BY cursor ASC LIMIT ?
  │       3. next_cursor = items[-1].cursor if items else after_cursor
  │       4. 不删除消息
  │     输出: OutboxPage { items: [OutboxMessage], next_cursor: int }
  │
  ▼
前端 ← { items: [{cursor, event_id, trigger_id, created_at, content, metadata}], next_cursor }
  │  翻页: 用 next_cursor 作为下一次的 after
  │  空页: next_cursor == 传入的 after (游标不变)
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
| `ports/sample_reader.py` | Sample 读取端口 | `SampleReader`(ABC), `AllSamples` |
| `ports/sample_publisher.py` | Sample 发布端口（预留） | `SamplePublisher`(ABC) |
| `ports/model_client.py` | 模型调用端口 | `ModelClient`(ABC) |
| `ports/outbox_store.py` | Outbox 存储端口 | `OutboxStore`(ABC) |

### 应用层 — `app/application/`

| 文件 | 职责 | 核心类型 |
|------|------|----------|
| `turn_runner.py` | 唯一回合编排器 | `TurnRunner.run()`, `ActiveTurnResult` |
| `app.py` | 依赖注入工厂 | `create_app(config) → FastAPI` |

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

### 基础设施 — `app/infrastructure/`

| 文件 | 职责 |
|------|------|
| `config.py` | `Config.load_from_env()`, `Config.validate()` |
| `logging.py` | `get_logger()` — 结构化日志 |

### 启动入口 — `app/main.py`

加载 `.env` → `Config.load_from_env()` → `create_app(config)` → `uvicorn.run()`

### Sample 数据 — `samples/`

| 文件 | 内容 |
|------|------|
| `identity.sample.json` | name, self_description, values, boundaries, relationship_definition |
| `preferences.sample.json` | communication_preferences, stable_likes, stable_dislikes, interaction_rules |
| `memories.sample.json` | items[{id, content, category, priority, created_at}] |
| `working_state.sample.json` | current_focus, emotion_summary, pending_items, next_wake_at |

---

## 环境配置

复制 `.env.example` 为 `.env`，填入实际值：

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

---

## 运行与测试

```bash
# 安装依赖
uv sync

# 运行测试 (264 个)
uv run pytest

# 启动服务
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 或
uv run python -m app.main
```

---

## 当前实现状态

| 里程碑 | 内容 | 测试数 | 状态 |
|--------|------|--------|------|
| M1 | 四份 Sample + FileSampleRepository | 83 | 完成 |
| M2 | ContextBuilder + OpenAIUpstreamClient | 98 | 完成 |
| M3 | POST /v1/chat/completions + TurnRunner | 23 | 完成 |
| M4 | SQLiteOutboxStore + GET /v1/outbox | 45 | 完成 |
| M5 | LocalScheduler + 主动回合调度 | 15 | 完成 |
| M6 | 集成部署 + 真实 API 冒烟测试 | — | 完成 |

全量回归：264 passed
真实 API 冒烟测试：PASS (DeepSeek deepseek-v4-flash)

---

## 后续可拓展方向

### 1. 长驻状态计算区

**当前状态：** `SamplePublisher` 端口已预留（`app/domain/ports/sample_publisher.py`），但无实现。Sample 由人工修改 JSON 文件。

**扩展路径：**

```
对话历史 / Outbox 事件 / 外部输入
        │
        ▼
  长驻状态计算区 (新模块 app/application/state_worker.py)
  - 记忆整理：从对话中提取关键事实，更新 memories.sample.json
  - 情绪演化：基于交互频率/内容调整 working_state
  - 过期清理：低优先级记忆降级或移除
        │
        ▼
  SamplePublisher.publish_sample(type, snapshot)
  - 原子写入 JSON 文件（或替换为其他存储）
  - 更新 version 号
        │
        ▼
  下一回合 TurnRunner 读到新 Sample
```

**需要实现的接口：**
- `SamplePublisher.publish(sample_type: str, data: dict)` — 原子发布
- 状态计算引擎（规则系统 or LLM 辅助）
- 触发时机：被动回合结束后 / 定时周期内

**涉及代码变更：**
- 新增 `app/application/state_worker.py`
- 实现 `app/adapters/samples/file_sample_publisher.py`
- `app/application/app.py` 注入 SamplePublisher
- TurnRunner 在被动回合结束后触发状态计算（可选）

### 2. Outbox HTTP 长轮询

**当前状态：** 前端通过 `GET /v1/outbox?after=N` 轮询，不支持长轮询。

**扩展路径：**

在现有游标模型上增加长轮询端点：
- `GET /v1/outbox/longpoll?after=N&timeout=30`
- 无新消息时 hold 住连接直到超时或有新消息
- 基于 `asyncio.Event` 或 `threading.Condition` 实现

**涉及代码变更：**
- `app/adapters/http/outbox_controller.py` 新增长轮询路由
- `SQLiteOutboxStore` 增加 `wait_for_new(after_cursor, timeout)` 方法
- 注意 uvicorn worker 的并发限制

### 3. 流式响应（SSE）

**当前状态：** `stream=true` 被拒绝并返回 400。

**扩展路径：**

```
前端 → POST /v1/chat/completions (stream=true)
  │
  ▼
ChatController → 转发上游 SSE 流
  - 透传上游 chunk（不修改内容）
  - 仅注入 system message 到首次请求
  - 逐 chunk 返回给前端
```

**涉及代码变更：**
- `app/domain/models/chat_completion.py` 增加流式解析
- `app/adapters/models/openai_upstream_client.py` 增加 `complete_stream()` 方法
- `app/adapters/http/chat_controller.py` 增加 SSE 响应分支
- `TurnRunner.run()` 增加流式分支

### 4. 多模型路由

**当前状态：** 固定单一上游模型，`model` 字段被忽略。

**扩展路径：**

- 前端传入的 `model` 字段映射到不同上游端点
- 配置改为 `UPSTREAM_MODELS = '{"fast": "...", "smart": "..."}'`
- `ModelClient` 改为 `ModelRouter`，根据 `model` 字段选择上游

**涉及代码变更：**
- `app/infrastructure/config.py` 多模型配置
- `app/domain/ports/model_client.py` 增加 `ModelRouter`
- `app/adapters/models/` 新增多个客户端或路由适配器

### 5. Operit 插件集成

**当前状态：** 架构预留了插件层，无实现。

**扩展路径：**

- Operit 作为前端客户端，通过 OpenAI 兼容协议调用网关
- Operit 通过 `GET /v1/outbox` 拉取主动消息
- 可选：Operit 发送 ACK 标记消息已读（当前 Outbox 消息不删除）

**涉及代码变更：**
- 新增 `DELETE /v1/outbox/{cursor}` 或 `POST /v1/outbox/ack`
- `OutboxStore` 增加 `acknowledge(cursor)` 方法
- 需考虑消息生命周期管理

### 6. 状态自动回写

**当前状态：** Sample 是只读的，需要人工修改。

**扩展路径：**

TurnRunner 在被动回合结束后，可选地触发状态回写：
- 更新 `working_state` 的 `current_focus` 和 `emotion_summary`
- 追加新的 `memories` 条目
- 通过 `SamplePublisher` 原子发布

**涉及代码变更：**
- `TurnRunner` 增加 `post_turn_hook` 机制
- `StateWorker` 实现回写规则
- `FileSampleRepository` 扩展为可读写（或新建 `FileSamplePublisher`）

### 7. 多实例部署

**当前状态：** 单进程，SQLite 单文件，scheduler 单线程。

**扩展路径：**

- SQLite → PostgreSQL（更换 `OutboxStore` 适配器）
- `LocalScheduler` → 分布式锁 + 远程调度器
- `threading.Event` → Redis pub/sub 或消息队列
- `FileSampleRepository` → 共享存储（S3 / 数据库）

**端口接口已隔离：** 所有 IO 操作通过 `SampleReader` / `OutboxStore` / `ModelClient` 抽象，替换适配器不影响领域层和应用层。

---

## 架构约束（不可违反）

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
