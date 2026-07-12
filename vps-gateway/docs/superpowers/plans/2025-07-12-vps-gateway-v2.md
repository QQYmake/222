# VPS 中转网关 v2 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 v1 基础上增量升级为 v2，增加 VPS 工具循环、持久化唤醒计划和单读取者 Outbox 长轮询，保证用户回合与主动回合互不阻塞、主动回合永不并发。

**Architecture:** 单进程异步 FastAPI 服务。用户回合和主动回合通过独立 TurnContext 隔离；工具全部由 VPS ToolRegistry 注册执行；唤醒计划持久化到 SQLite；Outbox 使用短连接 + 进程内 Notifier 实现长轮询。

**Tech Stack:** Python 3.12 / FastAPI / httpx(async) / SQLite3(WAL) / uv / pytest / asyncio

## Global Constraints

- 依赖方向：适配器 → 应用层 → 领域接口 → 基础设施，禁止反向
- 运行形态：单进程、单实例；禁止 `--workers > 1`
- SQLite 每次操作使用短连接；等待期间不持有 connection 或 transaction
- FastAPI lifespan 是 Scheduler 唯一启动与停止入口；构造函数不得启动后台任务
- 工具全部由 VPS 注册执行；前端 tools/tool_choice 不得创建或覆盖服务器工具
- 工具循环：最多 5 轮、10 次、单工具 15 秒；同一批 tool_calls 按返回顺序执行
- 两个主动回合永远串行；冲突任务直接 expired，不排队补跑
- 唤醒时区 Asia/Shanghai；允许时间 08:00—24:00；最短间隔 20 分钟；每天最多 10 次
- Outbox 必须先 commit 后 notify；同时只允许一个活动长轮询读取者
- v2 不启用 Sample 自动回写和状态演化
- TDD：先写测试再实现
- 不自动 commit、push 或创建 PR

## 启动基线

- v1 测试：264 passed, 0 failed, 0 skipped (1.05s)
- Git 工作区：clean
- Python 3.12.3 / FastAPI / httpx / uv / pytest

## v1 → v2 差异清单

| 维度 | v1 现状 | v2 目标 |
|---|---|---|
| ModelClient | 同步 httpx.Client | 异步 httpx.AsyncClient |
| TurnRunner.run() | 同步 | 异步 async def |
| SQLite | 长连接 self._conn | 每次操作短连接 |
| Scheduler 启动 | 构造函数中 start() | 仅 FastAPI lifespan startup |
| 调度器 | threading 线程 LocalScheduler | asyncio WakeScheduler + WakeController |
| 工具系统 | 无 | ToolRegistry + ToolDispatcher + 4 工具 |
| 模型循环 | 单次调用 | ModelToolLoop（5轮/10次/15秒） |
| 唤醒计划 | 固定间隔 timer | 持久化 WakeJob + 策略 + 规划器 |
| Outbox | list_after 分页 | claim_one + Notifier + 长轮询 |
| ChatMessage | role+content | +tool_calls/tool_call_id |
| HTTP | /v1/outbox 分页 | /v1/outbox/longpoll 长轮询 |

---

## 数据流图（必需）

### 1. 用户回合工具循环

```
前端 POST /v1/chat/completions
  → ChatController 校验（拒绝客户端 tools）
  → TurnRunner.create_turn_context(trigger_type=user)
  → 读取 Sample + ContextBuilder → 初始 messages
  → ModelToolLoop.run(turn_context, registry)
      LOOP:
        → AsyncModelClient.complete(messages, tools)
        → LLM 返回 tool_calls?
            NO  → 返回最终 response
            YES → 检查 tool_round < 5 且 total+batch <= 10
                  → 保存 assistant(tool_calls) 到 messages
                  → FOR EACH tool_call IN ORDER:
                      → ToolDispatcher.execute(call, ctx)
                      → 追加 tool message 到 messages
                  → tool_round += 1, total += batch
                  → 继续循环
  → 返回最终 ChatCompletionResponse 给前端
```

### 2. 主动唤醒工具循环

```
WakeScheduler 扫描到期 pending WakeJob 批次
  → WakeController.process_batch(due_jobs)
      → 超出 START_GRACE → expired(missed_deadline)
      → WakeStartPolicy 过滤不可启动任务 → expired(各原因)
      → 选择最早候选
      → ActiveTurnGate.try_acquire()
          FAIL → 本批全部到期任务 expired(active_turn_running)
          OK   → 候选 pending → running (条件更新)
                 → 本批其余到期任务 expired(active_turn_running)
                 → asyncio.create_task(active_turn_runner(wake_trigger))
                 → 扫描循环立即返回
  [后台任务]
  → TurnRunner.create_turn_context(trigger_type=wake)
  → 读取 Sample + ContextBuilder → 初始 messages
  → ModelToolLoop.run(turn_context, registry)
      → 同用户回合工具循环逻辑
  → <NO_MESSAGE> → completed, 不写 Outbox
  → 普通文本 → OutboxStore.enqueue_once → commit → Notifier.notify
  → 异常 → failed
  → ActiveTurnGate.release()
  → WakeJobStore.transition(running → completed/failed)
```

### 3. schedule_wakeup 写入到 WakeController 执行

```
[模型回合中]
  LLM tool_call(schedule_wakeup, {wake_id, requested_at, reason})
  → ToolDispatcher.execute()
  → schedule_wakeup executor:
      → ScheduleAdmissionPolicy.validate(requested_at, now, daily_records)
          REJECT → 返回错误 ToolResult (错误码 + 最早允许时间)
          ACCEPT → WakeJobStore.schedule_once(job)
                   → 幂等写入 pending WakeJob
      → 返回成功 ToolResult (含 wake_id, scheduled_at)
  → tool message 回灌 LLM

[后续扫描循环]
  WakeScheduler.scan()
  → WakeJobStore.due_jobs(now, grace)
  → WakeController.process_batch(due_jobs)
  → 到期任务进入主动唤醒工具循环（见数据流图 2）
```

### 4. Outbox commit 到长轮询返回

```
[主动回合后台任务]
  TurnRunner → OutboxStore.enqueue_once(message)
  → SQLite 短连接: INSERT → COMMIT → 关闭连接
  → 返回 OutboxMessage (含 cursor)
  → Notifier.notify_committed(cursor)
      → 更新 current_version
      → 唤醒等待中的 wait_for_change()

[前端长轮询]
  GET /v1/outbox/longpoll?after=N&timeout=30
  → OutboxLongPollService.poll(after, timeout, reader_id)
      → SingleReaderGate.try_acquire()
          FAIL → 返回 reader_busy (409)
          OK   → LOOP:
                   → notifier.current_version() → v
                   → SQLite 短连接: claim_one(after, reader_id)
                       → 有消息 → claimed → 释放 Gate → 返回 item
                       → 无消息 → 关闭连接
                   → remaining = deadline - monotonic_now
                   → remaining <= 0 → 超时 → 释放 Gate → 返回 null
                   → wait_for_change(v, remaining)
                   → 被唤醒 → 继续循环（不重置 deadline）
```

### 5. 用户回合与主动回合并行 + 两个主动回合冲突

```
并发场景 A: 用户回合 + 主动回合并行（允许）
  HTTP Task A: TurnRunner(user_trigger) → AsyncModelClient.complete() → 等待 LLM
  [同时]
  Wake Task B: ActiveTurnGate.try_acquire() → OK → TurnRunner(wake_trigger) → AsyncModelClient.complete()
  → 两个 LLM 请求可重叠
  → TurnContext A 和 B 独立，messages/计数不共享
  → A 不获取 ActiveTurnGate

并发场景 B: 两个主动回合冲突（禁止并行）
  Wake Task B 正在运行 (Gate 已锁)
  Wake Task C 到期
  → WakeController → ActiveTurnGate.try_acquire() → FAIL (已锁)
  → Task C → expired(active_turn_running)
  → Task C 不等待、不排队、不补跑
```

---

## 反馈点

| 里程碑 | 反馈点 | 证明方式 |
|---|---|---|
| M0 | 用户 LLM 等待不再阻塞 Outbox HTTP | 异步 TurnRunner + async ModelClient；HTTP 请求等待 LLM 时另一个 HTTP 请求可响应 |
| M3 | 纯测试工具循环闭环 | Mock LLM 返回 tool_calls，验证顺序执行、轮次计数、上限终止 |
| M5 | 主动唤醒串行；用户/主动回合可并行 | ActiveTurnGate 单锁；用户回合不获取 Gate；两个到期任务一个 running 一个 expired |
| M6 | Outbox commit 后唤醒长轮询 | enqueue_once commit → notify → 等待中的 longpoll 立即返回 |
| M7 | 全量回归、重启恢复；真实工具冒烟 | 全量 pytest 通过；重启后 pending WakeJob 恢复；真实 get_server_time 闭环 |

---

## M0: 异步 ModelClient、TurnRunner、lifespan；SQLite 短连接

**依赖:** v1
**验收:** 12.4.1, 12.4.4—6

### Task M0-1: ChatMessage 扩展 tool_calls/tool_call_id（为 M1 预留，此处仅扩展数据结构）

修改位置：
- `app/domain/models/turn.py`（现有）

数据输入：
- 来源：架构文档 5.1 ChatMessage
- 类型：ChatMessage dataclass
- 约束：assistant 有 tool_calls 时允许 content=None；tool 必须有 tool_call_id；system/user 不允许 tool_calls

处理指令：
1. ChatMessage 增加 tool_calls: list[dict] | None = None, tool_call_id: str | None = None
2. 保持 frozen dataclass
3. 不破坏现有 ChatMessage(role, content) 用法

数据输出：
- 类型：扩展后的 ChatMessage
- 去向：ContextBuilder, TurnRunner, ModelToolLoop
- 可观察结果：现有测试仍通过

接口关系：
v1 ContextBuilder → ChatMessage(扩展) → v1 TurnRunner

测试：
- 现有 264 测试全部通过（不新增破坏）
- 新增：ChatMessage 可携带 tool_calls=None, tool_call_id=None（默认值）

### Task M0-2: AsyncModelClient 端口与适配器

修改位置：
- `app/domain/ports/model_client.py`（现有，改为 async）
- `app/adapters/models/openai_upstream_client.py`（现有，改为 async）
- `app/domain/models/turn.py`（扩展 ModelCompletionInput）

数据输入：
- 来源：架构文档 6.1 AsyncModelClient
- 类型：ModelCompletionInput(messages, tools, tool_choice, temperature, max_output_tokens)
- 约束：异步发送；等待期间让出 HTTP 执行权；保留 assistant content/tool_calls/finish_reason

处理指令：
1. ModelClient.complete 改为 `async def complete(self, input) -> ChatCompletionResponse`
2. ModelCompletionInput 增加 tools: list[dict] | None, tool_choice: str | None
3. OpenAIUpstreamClient 改用 httpx.AsyncClient
4. 构造上游请求体：model, messages(含 tool_calls/tool_call_id), tools, tool_choice, temperature, stream=false
5. 超时/协议错误转换为 UpstreamError/UpstreamTimeout
6. 支持注入外部 httpx.AsyncClient（lifespan 创建共享 client）
7. validate_chat_completion_response 扩展：解析 tool_calls

数据输出：
- 类型：ChatCompletionResponse（扩展 Choice 含 tool_calls, finish_reason）
- 去向：ModelToolLoop, TurnRunner
- 可观察结果：异步 complete 可被 await

接口关系：
TurnRunner → AsyncModelClient → httpx.AsyncClient → 上游 LLM

测试：
- 先失败：`async def test_async_model_client_complete()` — Mock httpx.AsyncClient，验证 async 调用
- 先失败：`async def test_model_client_passes_tools()` — 验证 tools 字段出现在请求体
- 先失败：`async def test_model_client_parses_tool_calls()` — 验证响应中 tool_calls 被解析
- 通过判据：async complete 返回含 tool_calls 的 ChatCompletionResponse

### Task M0-3: ChatCompletionResponse 扩展 tool_calls

修改位置：
- `app/domain/models/chat_completion.py`（现有）

数据输入：
- 来源：架构文档 5.3 ToolCall, 5.1 ChatMessage
- 类型：Choice dataclass
- 约束：assistant 可携带 tool_calls；tool_calls 为 list[dict] 或 None

处理指令：
1. Choice 增加 tool_calls: list[dict] | None = None
2. validate_chat_completion_response 解析 message.tool_calls
3. to_dict() 输出 tool_calls（如有）
4. 新增 first_assistant_tool_calls() -> list[dict] | None
5. 保持现有 first_assistant_text() 行为

数据输出：
- 类型：扩展后的 Choice / ChatCompletionResponse
- 去向：ModelToolLoop
- 可观察结果：含 tool_calls 的响应可被解析

接口关系：
AsyncModelClient → ChatCompletionResponse(扩展) → ModelToolLoop

测试：
- 先失败：`test_validate_response_with_tool_calls()` — 含 tool_calls 的响应被正确解析
- 先失败：`test_choice_to_dict_includes_tool_calls()` — to_dict 输出 tool_calls
- 通过判据：tool_calls 非空时 Choice.tool_calls 为 list

### Task M0-4: TurnRunner 改为异步 + TurnContext

修改位置：
- `app/application/turn_runner.py`（现有，改为 async）
- `app/domain/models/turn.py`（新增 TurnContext）

数据输入：
- 来源：架构文档 5.4 TurnContext, 6.6 TurnRunner v2
- 类型：TurnContext(turn_id, trigger_type, trigger_id, messages, sample_versions, tool_round, total_tool_calls, started_at)
- 约束：每个回合独立 TurnContext；不共享 messages/计数器

处理指令：
1. 新增 TurnContext dataclass（frozen）
2. TurnRunner.run 改为 `async def run(self, trigger) -> ChatCompletionResponse | ActiveTurnResult`
3. 创建独立 TurnContext
4. 调用 async model_client.complete()
5. 用户回合返回最终响应
6. 主动回合：<NO_MESSAGE> 不写 Outbox；正常文本 enqueue_once + notify
7. TurnRunner 接受 tool_registry（可为 None，M0 阶段不使用工具循环）
8. TurnRunner 接受 outbox_notifier（主动回合 commit 后 notify）

数据输出：
- 类型：ChatCompletionResponse | ActiveTurnResult
- 去向：ChatController / WakeController
- 可观察结果：async run 可被 await

接口关系：
ChatController → async TurnRunner → AsyncModelClient
WakeController → async TurnRunner → OutboxStore + Notifier

测试：
- 先失败：`async def test_turn_runner_async_user_turn()` — Mock async model_client，验证 await
- 先失败：`async def test_turn_runner_async_active_turn_enqueues()` — 验证主动回合写 Outbox
- 通过判据：现有 v1 测试适配 async 后通过

### Task M0-5: SQLite 短连接改造

修改位置：
- `app/adapters/outbox/sqlite_outbox_store.py`（现有）

数据输入：
- 来源：架构文档 7.4 Outbox 边界
- 类型：SQLiteOutboxStore
- 约束：每次操作打开独立短连接；不长期共享 connection

处理指令：
1. 移除 self._conn 长连接
2. 每次操作：sqlite3.connect(path) → execute → commit → close
3. enqueue_once: 短连接 INSERT + ON CONFLICT + SELECT
4. list_after: 短连接 SELECT
5. 新增 claim_one(after_cursor, reader_id) → OutboxMessage | None（M6 完整实现，此处预留接口）
6. _ensure_table 在首次操作时执行（或用 classmethod 初始化）
7. WAL 模式在每次连接时设置

数据输出：
- 类型：OutboxMessage / OutboxPage
- 去向：TurnRunner / OutboxLongPollService
- 可观察结果：操作结束后无活跃 connection

接口关系：
TurnRunner → SQLiteOutboxStore(短连接) → SQLite

测试：
- 先失败：`test_sqlite_outbox_short_connection()` — 操作后连接已关闭
- 先失败：`test_sqlite_outbox_enqueue_still_idempotent()` — 幂等性保持
- 通过判据：现有 outbox 测试适配后通过

### Task M0-6: FastAPI lifespan 改造 + AppFactory v2

修改位置：
- `app/application/app.py`（现有）
- `app/adapters/scheduler/local_scheduler.py`（现有，保留 v1 但不在构造时启动）
- `app/infrastructure/lifecycle.py`（新增）

数据输入：
- 来源：架构文档 6.17 AppLifecycle
- 类型：FastAPI lifespan context manager
- 约束：构造函数不启动后台任务；lifespan startup 启动 Scheduler；shutdown 停止

处理指令：
1. OpenAIUpstreamClient 接受外部 httpx.AsyncClient（不在构造时创建）
2. create_app 中 lifespan startup:
   a. 创建共享 httpx.AsyncClient
   b. 初始化 OutboxNotifier（M6）、SingleReaderGate（M6）、ActiveTurnGate（M5）
   c. WakeJobStore.recover_after_restart()（M4，M0 阶段跳过）
   d. 启动 Scheduler（M0 阶段保留 v1 LocalScheduler 但改为 lifespan 启动）
3. lifespan shutdown:
   a. 停止 Scheduler
   b. 关闭 httpx.AsyncClient
4. LocalScheduler 构造函数不再自动 start()
5. create_app 返回 app 后，未调用 lifespan 时不启动 Scheduler

数据输出：
- 类型：FastAPI app
- 去向：uvicorn
- 可观察结果：create_app 不启动后台任务；startup 后 Scheduler 运行

接口关系：
main.py → create_app → lifespan(startup→Scheduler, shutdown→cleanup)

测试：
- 先失败：`async def test_create_app_does_not_start_scheduler()` — 未 startup 时 Scheduler 未启动
- 先失败：`async def test_lifespan_starts_and_stops_scheduler()` — startup 后运行，shutdown 后停止
- 先失败：`async def test_lifespan_closes_http_client()` — shutdown 后 client 已关闭
- 通过判据：12.4.4（未 startup 不启动 Scheduler）、12.4.5（单次 startup 一个 Controller）、12.4.6（shutdown 关闭）

### M0 全量回归

运行：`uv run pytest -v`
预期：所有 v1 测试适配 async 后通过 + 新增 M0 测试通过
反馈点 M0：用户 LLM 等待不再阻塞 Outbox HTTP — `async test_user_turn_waiting_llm_outbox_responds()`

---

## M1: Chat/tool 数据合同扩展

**依赖:** M0
**验收:** ChatMessage/ToolCall/ToolResult 合同单元测试

### Task M1-1: ToolCall, ToolResult, ToolDefinition 领域模型

修改位置：
- `app/domain/models/tool.py`（新增）
- `app/domain/models/turn.py`（扩展 ToolExecutionContext）

数据输入：
- 来源：架构文档 5.2, 5.3
- 类型：ToolCall(id, name, arguments_json), ToolResult(tool_call_id, ok, content, error_code, truncated, duration_ms), ToolDefinition(name, description, parameters, enabled_in_production, timeout_seconds, max_result_chars)
- 约束：见架构 5.2/5.3

处理指令：
1. 定义 ToolCall frozen dataclass
2. 定义 ToolResult frozen dataclass
3. 定义 ToolDefinition frozen dataclass
4. 定义 ToolExecutionContext(turn_id, trigger_type, trigger_id, tool_round, total_tool_calls) — 传递给工具执行器
5. 定义 ToolLoopLimitError(Exception)
6. 定义工具错误码常量: tool_not_found, invalid_arguments, tool_timeout, tool_failed

数据输出：
- 类型：ToolCall / ToolResult / ToolDefinition / ToolExecutionContext
- 去向：ToolRegistry, ToolDispatcher, ModelToolLoop
- 可观察结果：可构造并序列化

接口关系：
ModelToolLoop → ToolCall → ToolDispatcher → ToolResult → ModelToolLoop

测试：
- 先失败：`test_tool_call_creation()` — ToolCall 字段正确
- 先失败：`test_tool_result_with_error()` — 错误 ToolResult 含 error_code
- 先失败：`test_tool_definition_defaults()` — 默认值正确
- 通过判据：所有数据合同字段与架构文档一致

### Task M1-2: ChatMessage 校验规则扩展

修改位置：
- `app/domain/models/chat_completion.py`（现有）
- `app/domain/models/turn.py`（现有）

数据输入：
- 来源：架构文档 5.1
- 类型：ChatMessage 校验
- 约束：assistant 有 tool_calls 时允许 content=None；tool 必须有 tool_call_id 和字符串 content；system/user 不允许 tool_calls

处理指令：
1. 新增 validate_chat_message(msg: ChatMessage) 函数
2. 校验规则按架构 5.1
3. parse_chat_request 扩展：允许 assistant 消息携带 tool_calls
4. 不破坏现有 v1 校验（system/user/assistant content 非空）

数据输出：
- 类型：校验函数
- 去向：ChatController, ModelToolLoop
- 可观察结果：非法消息被拒绝

接口关系：
ChatController → validate_chat_message → TurnRunner

测试：
- 先失败：`test_assistant_with_tool_calls_content_null_ok()` — assistant tool_calls + content=None 通过
- 先失败：`test_tool_message_without_tool_call_id_rejected()` — tool 无 tool_call_id 被拒
- 先失败：`test_system_with_tool_calls_rejected()` — system 携带 tool_calls 被拒
- 通过判据：所有校验规则与架构 5.1 一致

### M1 全量回归

运行：`uv run pytest -v`

---

## M2: ToolRegistry + ToolDispatcher + 四个第一批工具

**依赖:** M1
**验收:** 12.1.3—6, 12—13

### Task M2-1: ToolExecutor 端口 + ToolRegistry

修改位置：
- `app/domain/ports/tool_executor.py`（新增）
- `app/adapters/tools/registry.py`（新增）

数据输入：
- 来源：架构文档 6.2, 6.4
- 类型：ToolExecutor(ABC), ToolRegistry
- 约束：工具名唯一；测试工具生产不暴露；schemas() 输出允许工具

处理指令：
1. ToolExecutor ABC: `async def execute(self, arguments: dict, context: ToolExecutionContext) -> object`
2. ToolRegistry:
   a. register(definition, executor) — 校验名唯一
   b. schemas() -> list[dict] — 根据 enabled_in_production 过滤
   c. resolve(name) -> ToolExecutor | None
3. ToolRegistry 构造接受 test_tools_enabled: bool
4. schemas() 返回 OpenAI function calling 格式

数据输出：
- 类型：ToolRegistry
- 去向：ModelToolLoop, AppFactory
- 可观察结果：注册后可 schemas() 和 resolve()

接口关系：
AppFactory → ToolRegistry.register → ModelToolLoop 使用 schemas()/resolve()

测试：
- 先失败：`test_registry_schemas_excludes_test_tools_in_production()` — 生产模式不返回 echo/delay/fail
- 先失败：`test_registry_resolve_returns_executor()` — 注册后可 resolve
- 先失败：`test_registry_duplicate_name_raises()` — 重复注册报错
- 通过判据：12.1.12（生产配置 echo/delay/fail 不出现）

### Task M2-2: ToolDispatcher

修改位置：
- `app/application/tool_dispatcher.py`（新增）

数据输入：
- 来源：架构文档 6.3
- 类型：ToolDispatcher(registry, timeout_seconds, max_result_chars)
- 约束：查找→解析→校验→执行→超时→截断→错误归一化

处理指令：
1. async execute(call: ToolCall, context: ToolExecutionContext) -> ToolResult
2. registry.resolve(name) — None → tool_not_found
3. json.loads(arguments_json) — 失败 → invalid_arguments
4. asyncio.wait_for(executor.execute(args, ctx), timeout) — 超时 → tool_timeout
5. 异常捕获 → tool_failed
6. 结果超 max_result_chars → 截断 + truncated=True
7. 记录 duration_ms

数据输出：
- 类型：ToolResult
- 去向：ModelToolLoop
- 可观察结果：所有错误场景返回对应 error_code

接口关系：
ModelToolLoop → ToolDispatcher → ToolRegistry → ToolExecutor

测试：
- 先失败：`async def test_dispatcher_tool_not_found()` — 不存在工具返回 tool_not_found
- 先失败：`async def test_dispatcher_invalid_arguments()` — JSON 解析失败返回 invalid_arguments
- 先失败：`async def test_dispatcher_timeout()` — 超时工具返回 tool_timeout（15秒内）
- 先失败：`async def test_dispatcher_truncation()` — 结果超长被截断
- 通过判据：12.1.3—6

### Task M2-3: get_server_time 工具

修改位置：
- `app/adapters/tools/get_server_time.py`（新增）

数据输入：
- 来源：架构文档 1.1.7
- 类型：ToolExecutor 实现
- 约束：返回当前服务器时间（ISO8601）

处理指令：
1. async execute(arguments, context) -> str
2. 返回 datetime.now(timezone.utc).isoformat()
3. enabled_in_production = True
4. timeout_seconds = 5
5. parameters: 空对象 schema

数据输出：
- 类型：ISO8601 字符串
- 去向：ToolDispatcher → ModelToolLoop → LLM
- 可观察结果：返回当前 UTC 时间

接口关系：
LLM tool_call → ToolDispatcher → get_server_time → ToolResult

测试：
- 先失败：`async def test_get_server_time_returns_iso8601()` — 返回有效 ISO8601
- 通过判据：返回值可被 datetime.fromisoformat 解析

### Task M2-4: echo_test, delay_test, fail_test 测试工具

修改位置：
- `app/adapters/tools/test_tools.py`（新增）

数据输入：
- 来源：架构文档 1.1.7
- 类型：3 个 ToolExecutor 实现
- 约束：enabled_in_production = False；仅测试配置启用

处理指令：
1. echo_test: 返回传入的 message 参数
2. delay_test: asyncio.sleep(seconds 参数)，返回 "delayed:{seconds}"
3. fail_test: 抛出 RuntimeError("intentional failure")
4. 全部 enabled_in_production = False
5. timeout_seconds = 15

数据输出：
- 类型：各工具返回值
- 去向：ToolDispatcher → ModelToolLoop
- 可观察结果：echo 回显；delay 等待；fail 抛异常

接口关系：
LLM tool_call → ToolDispatcher → test_tools → ToolResult

测试：
- 先失败：`async def test_echo_test_returns_message()` — echo 回显正确
- 先失败：`async def test_delay_test_waits()` — delay 等待指定秒
- 先失败：`async def test_fail_test_raises()` — fail 被 Dispatcher 捕获为 tool_failed
- 通过判据：12.1.6（fail_test 错误被模型收到）

### Task M2-5: ChatController 拒绝客户端工具

修改位置：
- `app/adapters/http/chat_controller.py`（现有）

数据输入：
- 来源：架构文档 6.18
- 类型：HTTP 请求校验
- 约束：请求含 tools 或 tool_choice → 400 client_tools_not_allowed

处理指令：
1. parse_chat_request 后检查 body 是否含 "tools" 或 "tool_choice"
2. 含则返回 openai_error(400, "client_tools_not_allowed", "...")
3. 不影响无 tools 的正常请求

数据输出：
- 类型：400 错误或正常流程
- 去向：前端
- 可观察结果：含 tools 的请求被拒

接口关系：
前端 → ChatController(拒绝 tools) → TurnRunner

测试：
- 先失败：`test_chat_request_with_tools_rejected()` — 400 client_tools_not_allowed
- 先失败：`test_chat_request_with_tool_choice_rejected()` — 400 client_tools_not_allowed
- 先失败：`test_chat_request_without_tools_accepted()` — 正常流程
- 通过判据：12.1.13（前端上传工具不进入 Registry）

### M2 全量回归

运行：`uv run pytest -v`

---

## M3: ModelToolLoop 顺序、上限与错误回灌

**依赖:** M2
**验收:** 12.1.1—2, 7—11

### Task M3-1: ModelToolLoop 核心循环

修改位置：
- `app/application/model_tool_loop.py`（新增）

数据输入：
- 来源：架构文档 6.5
- 类型：ModelToolLoop(model_client, tool_dispatcher, registry, max_rounds, max_calls)
- 约束：5 轮 / 10 次 / 顺序执行 / 整批检查 / 上限不回灌

处理指令：
1. async run(messages: list[ChatMessage], turn_context: TurnContext) -> ChatCompletionResponse
2. 初始 tool_round=0, total_tool_calls=0
3. 调用 model_client.complete(messages, tools=registry.schemas())
4. 无 tool_calls → 返回最终响应
5. 有 tool_calls:
   a. tool_round >= max_rounds → 抛 ToolLoopLimitError
   b. total + batch_size > max_calls → 抛 ToolLoopLimitError（整批不执行）
   c. 保存 assistant(tool_calls) 到 messages
   d. FOR EACH call IN tool_calls (按顺序):
      - dispatcher.execute(call, ctx) → ToolResult
      - 追加 tool message 到 messages
   e. tool_round += 1, total += batch_size
   f. total == max_calls → 抛 ToolLoopLimitError（不再调用 LLM）
   g. 继续循环

数据输出：
- 类型：ChatCompletionResponse
- 去向：TurnRunner
- 可观察结果：工具循环按预期执行或终止

接口关系：
TurnRunner → ModelToolLoop → AsyncModelClient + ToolDispatcher

测试：
- 先失败：`async def test_loop_three_rounds()` — 三轮工具调用，顺序和 ID 配对正确 (12.1.1)
- 先失败：`async def test_loop_multiple_tools_same_response()` — 同一响应多工具按顺序执行 (12.1.2)
- 先失败：`async def test_loop_exceeds_five_rounds()` — 第六轮抛 ToolLoopLimitError (12.1.7)
- 先失败：`async def test_loop_exceeds_ten_calls()` — 第 11 次不执行 (12.1.8)
- 先失败：`async def test_loop_batch_exceeds_at_nine()` — 已 9 次后请求 3 个，整批不执行 (12.1.9)
- 先失败：`async def test_loop_first_response_eleven_tools()` — 首次 11 个工具整批不执行 (12.1.10)
- 先失败：`async def test_loop_nine_then_one()` — 已 9 次后请求 1 个，执行第 10 个后终止 (12.1.11)
- 通过判据：所有上限边界正确

### Task M3-2: TurnRunner 集成 ModelToolLoop

修改位置：
- `app/application/turn_runner.py`（M0 已改 async）

数据输入：
- 来源：架构文档 6.6
- 类型：TurnRunner + ModelToolLoop
- 约束：用户回合和主动回合都经过 ModelToolLoop

处理指令：
1. TurnRunner 接受 model_tool_loop（可为 None，None 时直接调用 model_client）
2. async run() 中：若 model_tool_loop 存在则调用 loop.run()，否则直接 model_client.complete()
3. ToolLoopLimitError:
   a. 用户回合 → 异常传播给 ChatController → 422
   b. 主动回合 → ActiveTurnResult(outcome="failed")
4. 主动回合：ModelToolLoop 返回后检查 first_assistant_text()

数据输出：
- 类型：ChatCompletionResponse | ActiveTurnResult
- 去向：ChatController / WakeController
- 可观察结果：工具循环集成到回合编排

接口关系：
ChatController → TurnRunner → ModelToolLoop → AsyncModelClient + ToolDispatcher

测试：
- 先失败：`async def test_turn_runner_with_tool_loop_user_turn()` — 用户回合经过工具循环
- 先失败：`async def test_turn_runner_tool_loop_limit_user_422()` — 用户回合上限错误传播
- 先失败：`async def test_turn_runner_tool_loop_limit_active_failed()` — 主动回合上限错误 → failed
- 通过判据：回合编排正确集成工具循环

### Task M3-3: ChatController ToolLoopLimitError → 422

修改位置：
- `app/adapters/http/chat_controller.py`（M2 已改）

数据输入：
- 来源：架构文档 6.18
- 类型：HTTP 错误映射
- 约束：ToolLoopLimitError → 422 tool_loop_limit_exceeded

处理指令：
1. 捕获 ToolLoopLimitError → openai_error(422, "tool_loop_limit_exceeded", "...")

数据输出：
- 类型：422 错误响应
- 去向：前端
- 可观察结果：上限错误返回 422

接口关系：
TurnRunner(ToolLoopLimitError) → ChatController → 422

测试：
- 先失败：`test_chat_tool_loop_limit_returns_422()` — 422 + 正确 error code
- 通过判据：422 状态码 + tool_loop_limit_exceeded

### M3 全量回归

运行：`uv run pytest -v`
反馈点 M3：纯测试工具循环闭环 — Mock LLM 返回 tool_calls，验证完整循环

---

## M4: WakeJobStore + Admission/Start Policy + 唤醒工具

**依赖:** M3
**验收:** 12.2.3—7, 11—12

### Task M4-1: WakeJob 领域模型

修改位置：
- `app/domain/models/wake_job.py`（新增）

数据输入：
- 来源：架构文档 5.5
- 类型：WakeJob dataclass
- 约束：scheduled_at 落在 08:00—24:00；wake_id 幂等；status 枚举

处理指令：
1. WakeJob frozen dataclass: wake_id, source, requested_at, scheduled_at, reason, status, created_at, started_at, finished_at, expire_reason
2. status 枚举: pending, running, completed, expired, failed, cancelled
3. expire_reason 枚举: active_turn_running, outside_window, daily_limit, min_interval, missed_deadline, crash_recovery, null
4. RecoveryResult dataclass: recovered_running: list, expired_missed: list, expired_crash: list

数据输出：
- 类型：WakeJob / RecoveryResult
- 去向：WakeJobStore, WakeController, 唤醒工具
- 可观察结果：可构造和状态转换

接口关系：
唤醒工具 → WakeJob → WakeJobStore → WakeController

测试：
- 先失败：`test_wake_job_creation()` — 字段正确
- 先失败：`test_wake_job_status_values()` — 状态枚举完整
- 通过判据：与架构 5.5 一致

### Task M4-2: ScheduleAdmissionPolicy

修改位置：
- `app/application/schedule_admission_policy.py`（新增）

数据输入：
- 来源：架构文档 6.7
- 类型：ScheduleAdmissionPolicy(timezone, active_start, active_end, min_interval, daily_limit)
- 约束：转换为 Asia/Shanghai；检查未来时间；08:00—24:00；20分钟/10次

处理指令：
1. validate(requested_at: str, now: datetime, daily_records: list[datetime]) -> AdmissionResult
2. 转换 requested_at 为 Asia/Shanghai
3. 检查 requested_at > now（否则 not_in_future）
4. 检查 08:00 <= time < 24:00（否则 outside_active_window）
5. 检查同日 20 分钟间隔（否则 min_interval_not_met）
6. 检查同日 < 10 次（否则 daily_limit_reached）
7. 返回 accepted + normalized_time 或拒绝码 + 最早允许时间
8. 未来状态不可预测时允许写入

数据输出：
- 类型：AdmissionResult(accepted: bool, normalized_time | None, reject_code | None, earliest_allowed | None)
- 去向：唤醒工具
- 可观察结果：各拒绝码正确

接口关系：
schedule_wakeup → ScheduleAdmissionPolicy → WakeJobStore

测试：
- 先失败：`test_admission_outside_window_rejected()` — 凌晨时间被拒 (12.2.3)
- 先失败：`test_admission_min_interval_rejected()` — 间隔不足被拒 (12.2.5)
- 先失败：`test_admission_daily_limit_rejected()` — 超过 10 次被拒 (12.2.4)
- 先失败：`test_admission_not_in_future_rejected()` — 过去时间被拒
- 先失败：`test_admission_valid_accepted()` — 合法时间通过
- 通过判据：各拒绝码与架构 6.7 一致

### Task M4-3: WakeStartPolicy

修改位置：
- `app/application/wake_start_policy.py`（新增）

数据输入：
- 来源：架构文档 6.8
- 类型：WakeStartPolicy(timezone, active_start, active_end, min_interval, daily_limit, grace_seconds)
- 约束：scheduled_at <= now <= scheduled_at + grace；执行时仍 08:00—24:00；20分钟；< 10 次

处理指令：
1. check(job: WakeJob, now: datetime, last_start: datetime | None, daily_count: int) -> StartResult
2. 检查 scheduled_at <= now <= scheduled_at + grace（否则 missed_deadline）
3. 检查执行时 08:00—24:00（否则 outside_window）
4. 检查距离最近启动 >= 20 分钟（否则 min_interval）
5. 检查同日 < 10 次（否则 daily_limit）
6. 返回 can_start 或 expire_reason

数据输出：
- 类型：StartResult(can_start: bool, expire_reason: str | None)
- 去向：WakeController
- 可观察结果：各 expire_reason 正确

接口关系：
WakeController → WakeStartPolicy → expired/running 决策

测试：
- 先失败：`test_start_policy_missed_deadline()` — 超过 grace → missed_deadline (12.2.11)
- 先失败：`test_start_policy_outside_window()` — 凌晨 → outside_window (12.2.3)
- 先失败：`test_start_policy_daily_limit()` — 第 11 次 → daily_limit (12.2.4)
- 先失败：`test_start_policy_min_interval()` — 间隔不足 → min_interval (12.2.5)
- 先失败：`test_start_policy_can_start()` — 合法 → can_start=True
- 通过判据：与架构 6.8 一致

### Task M4-4: SQLiteWakeJobStore

修改位置：
- `app/adapters/wakeups/sqlite_wake_job_store.py`（新增）
- `app/domain/ports/wake_job_store.py`（新增）

数据输入：
- 来源：架构文档 6.9
- 类型：WakeJobStore(ABC) + SQLiteWakeJobStore
- 约束：短连接；幂等；条件状态转换；recover_after_restart

处理指令：
1. WakeJobStore ABC: schedule_once, due_jobs, transition, list_jobs, cancel, recover_after_restart
2. SQLiteWakeJobStore:
   a. 每次操作短连接
   b. schedule_once: INSERT ON CONFLICT(wake_id) DO NOTHING + SELECT
   c. due_jobs: SELECT pending WHERE scheduled_at <= now + grace ORDER BY scheduled_at, created_at
   d. transition: UPDATE WHERE wake_id=? AND status=expected → return bool
   e. cancel: transition(pending → cancelled)
   f. recover_after_restart:
      - running → expired(crash_recovery)
      - pending 且超过 grace → expired(missed_deadline)
      - 宽限内 pending 保留
3. 建表 DDL: wake_jobs(wake_id PK, source, requested_at, scheduled_at, reason, status, created_at, started_at, finished_at, expire_reason)

数据输出：
- 类型：WakeJob / list[WakeJob] / bool / RecoveryResult
- 去向：WakeController, 唤醒工具
- 可观察结果：幂等写入；条件转换正确

接口关系：
唤醒工具 → WakeJobStore → SQLite
WakeController → WakeJobStore → SQLite

测试：
- 先失败：`test_wake_store_schedule_once_idempotent()` — 重复 wake_id 只一条 (12.2.7)
- 先失败：`test_wake_store_due_jobs_ordered()` — 按 scheduled_at 排序
- 先失败：`test_wake_store_transition_conditional()` — 条件更新防重复启动
- 先失败：`test_wake_store_cancel_only_pending()` — 只有 pending 可取消
- 先失败：`test_wake_store_recover_running_to_expired()` — 重启 running → expired(crash_recovery) (12.2.12)
- 先失败：`test_wake_store_recover_missed_deadline()` — 超宽限 pending → expired(missed_deadline) (12.2.11)
- 先失败：`test_wake_store_restart_preserves_scheduled_at()` — 三个月后任务时间不变 (12.2.6)
- 先失败：`test_wake_store_short_connection()` — 操作后连接关闭
- 通过判据：与架构 6.9 一致

### Task M4-5: schedule_wakeup, list_wakeups, cancel_wakeup 工具

修改位置：
- `app/adapters/tools/wake_tools.py`（新增）

数据输入：
- 来源：架构文档 6.13
- 类型：3 个 ToolExecutor 实现
- 约束：经过 ScheduleAdmissionPolicy；幂等写入；只 pending 可取消

处理指令：
1. ScheduleWakeupTool(admission_policy, wake_job_store):
   a. async execute({wake_id, requested_at, reason}, ctx)
   b. admission_policy.validate(requested_at, now, daily_records)
   c. 拒绝 → 返回错误字符串（含错误码和最早允许时间）
   d. 接受 → wake_job_store.schedule_once(job) → 返回成功字符串
   e. 重复 wake_id → 返回原记录信息
2. ListWakeupsTool(wake_job_store):
   a. async execute({status?, time_range?}, ctx)
   b. 返回 JSON 字符串列表
3. CancelWakeupTool(wake_job_store):
   a. async execute({wake_id}, ctx)
   b. wake_job_store.cancel(wake_id)
   c. 非 pending → 返回错误信息
4. 全部 enabled_in_production = True

数据输出：
- 类型：字符串结果（ToolResult content）
- 去向：ToolDispatcher → ModelToolLoop → LLM
- 可观察结果：schedule 成功/拒绝/重复；list 返回列表；cancel 成功/失败

接口关系：
LLM tool_call → ToolDispatcher → wake_tools → WakeJobStore

测试：
- 先失败：`async def test_schedule_wakeup_accepted()` — 合法时间写入成功
- 先失败：`async def test_schedule_wakeup_rejected_outside_window()` — 凌晨被拒
- 先失败：`async def test_schedule_wakeup_duplicate_returns_original()` — 重复返回原记录 (12.2.7)
- 先失败：`async def test_list_wakeups_returns_ordered()` — 按 scheduled_at 升序
- 先失败：`async def test_cancel_wakeup_pending_ok()` — pending 可取消
- 先失败：`async def test_cancel_wakeup_running_rejected()` — running 不可取消
- 通过判据：与架构 6.13 一致

### Task M4-6: Config v2 扩展

修改位置：
- `app/infrastructure/config.py`（现有）

数据输入：
- 来源：架构文档 9
- 类型：Config 新增字段
- 约束：见架构 9 配置校验规则

处理指令：
1. 新增 v2 配置字段（TOOLS_ENABLED, TEST_TOOLS_ENABLED, TOOL_MAX_ROUNDS, 等）
2. 新增唤醒配置（WAKE_ENABLED, WAKE_TIMEZONE, WAKE_ACTIVE_START/END, 等）
3. 新增 Outbox 长轮询配置（OUTBOX_LONGPOLL_TIMEOUT_SECONDS, OUTBOX_READER_ID）
4. validate() 扩展：工具轮次 1..5，总次数 1..10，超时 1..15，随机 max >= min，固定间隔 >= 20min，grace >= scan_interval 且 <= 60
5. 保持 v1 字段不变

数据输出：
- 类型：扩展后的 Config
- 去向：AppFactory
- 可观察结果：v2 配置可加载和校验

接口关系：
main.py → Config(v2) → AppFactory

测试：
- 先失败：`test_config_v2_defaults()` — 默认值正确
- 先失败：`test_config_v2_validate_tool_rounds()` — 超范围拒绝
- 先失败：`test_config_v2_validate_grace()` — grace 校验
- 通过判据：与架构 9 一致

### M4 全量回归

运行：`uv run pytest -v`

---

## M5: WakePlanner + WakeController + ActiveTurnGate

**依赖:** M4
**验收:** 12.2.1—2, 8—10, 14

### Task M5-1: ActiveTurnGate

修改位置：
- `app/domain/ports/active_turn_gate.py`（新增）
- `app/application/active_turn_gate.py`（新增）

数据输入：
- 来源：架构文档 6.10
- 类型：ActiveTurnGate
- 约束：非阻塞 try_acquire；不等待；单锁

处理指令：
1. ActiveTurnGate ABC: try_acquire() -> bool, release() -> None
2. InMemoryActiveTurnGate:
   a. asyncio.Lock 或 threading.Lock
   b. try_acquire: 非阻塞获取，成功 True，失败 False
   c. release: 释放锁
3. 构造函数不启动任何任务

数据输出：
- 类型：bool
- 去向：WakeController
- 可观察结果：同时只有一个 True

接口关系：
WakeController → ActiveTurnGate → TurnRunner

测试：
- 先失败：`async def test_gate_acquire_first_succeeds()` — 首次获取成功
- 先失败：`async def test_gate_acquire_second_fails()` — 已锁时获取失败
- 先失败：`async def test_gate_release_allows_reacquire()` — 释放后可重新获取
- 通过判据：与架构 6.10 一致

### Task M5-2: WakeController

修改位置：
- `app/application/wake_controller.py`（新增）

数据输入：
- 来源：架构文档 6.11
- 类型：WakeController(wake_job_store, start_policy, active_turn_gate, turn_runner, outbox_store, notifier)
- 约束：批次处理；最早候选；Gate 不等待；后台任务

处理指令：
1. async process_batch(due_jobs: list[WakeJob], now: datetime):
   a. 超出 grace → expired(missed_deadline)
   b. start_policy 过滤 → expired(各原因)
   c. 选择最早候选
   d. gate.try_acquire()
      - FAIL → 本批全部到期任务 expired(active_turn_running)
      - OK → 候选 transition(pending → running)
        - 更新失败 → release gate，不重试
        - 更新成功 → 本批其余 expired(active_turn_running)
        - asyncio.create_task(_run_active_turn(candidate))
2. async _run_active_turn(job):
   a. TurnRunner.run(WakeTrigger)
   b. 成功 → transition(running → completed)
   c. 失败 → transition(running → failed)
   d. finally → gate.release()
3. 不等待后台任务完成

数据输出：
- 类型：状态转换结果
- 去向：WakeJobStore
- 可观察结果：同时只有一个 running

接口关系：
WakeScheduler → WakeController → ActiveTurnGate + TurnRunner

测试：
- 先失败：`async def test_two_due_jobs_one_running_one_expired()` — 两个到期，一个 running 一个 expired (12.2.1)
- 先失败：`async def test_active_turn_running_new_job_expired()` — Gate 已锁 → expired (12.2.10)
- 先失败：`async def test_missed_deadline_expired()` — 超宽限 → expired(missed_deadline) (12.2.2)
- 先失败：`async def test_candidate_transition_failure_releases_gate()` — 条件更新失败释放 Gate
- 通过判据：与架构 6.11 一致

### Task M5-3: WakePlanner

修改位置：
- `app/application/wake_planner.py`（新增）

数据输入：
- 来源：架构文档 6.12
- 类型：WakePlanner(wake_job_store, admission_policy, config)
- 约束：固定和随机各最多一条 pending；确定性 ID

处理指令：
1. async ensure_plans(now: datetime):
   a. 固定规划器:
      - 查询 pending fixed 任务
      - 无则计算下一槽位: active_start + n × interval（< active_end）
      - 当日无剩余 → 次日 08:00
      - wake_id = "fixed:" + scheduled_at
      - admission_policy.validate → schedule_once
   b. 随机规划器（如果 enabled）:
      - 查询 pending random 任务
      - 无则 [min, max] 范围抽取一次
      - 凌晨区间 → 下一活动窗口
      - wake_id = "random:" + scheduled_at + UUID
      - admission_policy.validate → schedule_once
2. 已持久化时间不重新抽取
3. tool 来源远期任务不计入 fixed/random 额度

数据输出：
- 类型：pending WakeJob
- 去向：WakeJobStore
- 可观察结果：固定 ID 在相同 now 重启重算一致

接口关系：
WakeScheduler → WakePlanner → WakeJobStore

测试：
- 先失败：`async def test_fixed_planner_next_slot()` — 计算下一槽位正确
- 先失败：`async def test_fixed_planner_id_deterministic()` — 相同 now 重算 ID 一致 (12.2.14)
- 先失败：`async def test_fixed_planner_no_duplicate()` — 已有 pending 不重复创建
- 先失败：`async def test_random_planner_persists_once()` — 抽取一次持久化，不重新随机 (12.2.8)
- 通过判据：与架构 6.12 一致

### Task M5-4: WakeScheduler（替换 v1 LocalScheduler）

修改位置：
- `app/adapters/scheduler/wake_scheduler.py`（新增）
- `app/application/app.py`（现有，替换 LocalScheduler）

数据输入：
- 来源：架构文档 6.17, 10.1
- 类型：WakeScheduler(wake_controller, wake_planner, wake_job_store, scan_interval, grace_seconds, enabled)
- 约束：asyncio 后台任务；lifespan 启动/停止；构造函数不启动

处理指令：
1. 构造函数只存依赖，不启动
2. async start():
   a. 创建 asyncio.Task 运行 _scan_loop
   b. 记录 task 引用
3. async stop():
   a. 取消 task
   b. await task 退出
4. _scan_loop:
   a. LOOP:
      - wake_planner.ensure_plans(now)
      - due_jobs = wake_job_store.due_jobs(now, grace)
      - if due_jobs: wake_controller.process_batch(due_jobs, now)
      - await asyncio.sleep(scan_interval)
5. 可被 asyncio.Event 中断

数据输出：
- 类型：后台扫描
- 去向：WakeController
- 可观察结果：lifespan startup 后运行，shutdown 后停止

接口关系：
lifespan → WakeScheduler.start/stop → WakePlanner + WakeController

测试：
- 先失败：`async def test_scheduler_not_started_in_constructor()` — 构造后未运行 (12.4.4)
- 先失败：`async def test_scheduler_starts_on_lifespan()` — startup 后运行 (12.4.5)
- 先失败：`async def test_scheduler_stops_on_shutdown()` — shutdown 后停止 (12.4.6)
- 通过判据：与架构 6.17 一致

### Task M5-5: AppFactory v2 组装（M0-M5 集成）

修改位置：
- `app/application/app.py`（现有）

数据输入：
- 来源：架构文档 6.17
- 类型：create_app v2
- 约束：lifespan 管理所有组件生命周期

处理指令：
1. 创建 ToolRegistry + 注册 4+3 工具
2. 创建 ToolDispatcher
3. 创建 ModelToolLoop
4. 创建 AsyncModelClient（注入 lifespan 创建的 httpx.AsyncClient）
5. 创建 TurnRunner（注入以上 + OutboxStore + Notifier）
6. 创建 ActiveTurnGate
7. 创建 ScheduleAdmissionPolicy, WakeStartPolicy
8. 创建 WakeJobStore
9. 创建 WakeController
10. 创建 WakePlanner
11. 创建 WakeScheduler
12. lifespan:
    a. startup: recover_after_restart → 创建 AsyncClient → scheduler.start()
    b. shutdown: scheduler.stop() → 关闭 AsyncClient
13. HTTP 路由: ChatController + OutboxController

数据输出：
- 类型：FastAPI app
- 去向：uvicorn
- 可观察结果：完整 v2 应用可启动

接口关系：
main.py → create_app(v2) → lifespan → 全部组件

测试：
- 先失败：`async def test_app_v2_lifespan_full_cycle()` — startup → 请求 → shutdown
- 通过判据：12.4.4—6

### M5 全量回归

运行：`uv run pytest -v`
反馈点 M5：主动唤醒串行；用户/主动回合可并行 — `async test_user_and_active_parallel()` + `async test_two_active_serial()`

---

## M6: Outbox claim + Notifier + SingleReader 长轮询

**依赖:** M0, M5
**验收:** 12.3 全部, 12.2.13

### Task M6-1: OutboxMessage v2 + SQLiteOutboxStore v2

修改位置：
- `app/domain/models/outbox.py`（现有，扩展）
- `app/adapters/outbox/sqlite_outbox_store.py`（M0 已改短连接）

数据输入：
- 来源：架构文档 5.6, 6.14
- 类型：OutboxMessage v2 (cursor, event_id, trigger_id, content, status, created_at, claimed_at, claimed_by, metadata)
- 约束：短连接；enqueue_once 幂等；claim_one 原子领取

处理指令：
1. OutboxMessage 增加 status, claimed_at, claimed_by 字段
2. DDL 增加 status DEFAULT 'pending', claimed_at, claimed_by 列
3. enqueue_once: INSERT ... ON CONFLICT(trigger_id) DO NOTHING + SELECT（status=pending）
4. claim_one(after_cursor, reader_id):
   a. 短连接 BEGIN TRANSACTION
   b. SELECT cursor 最小的 pending WHERE cursor > after_cursor
   c. UPDATE → status='claimed', claimed_at=now, claimed_by=reader_id
   d. COMMIT → 返回 OutboxMessage
   e. 无消息 → 返回 None
5. 保持 list_after 兼容（v1 接口）

数据输出：
- 类型：OutboxMessage | None
- 去向：OutboxLongPollService
- 可观察结果：claim 后 status=claimed，不重复返回

接口关系：
TurnRunner → enqueue_once → SQLite
OutboxLongPollService → claim_one → SQLite

测试：
- 先失败：`test_claim_one_returns_oldest_pending()` — 返回 cursor 最小的 pending (12.3.7)
- 先失败：`test_claim_one_changes_status_to_claimed()` — 领取后 status=claimed (12.3.8)
- 先失败：`test_claim_one_no_pending_returns_none()` — 无消息返回 None
- 先失败：`test_claim_one_idempotent_trigger()` — 重复 trigger_id 只一条 (12.3.5)
- 先失败：`test_claim_one_short_connection()` — 操作后连接关闭 (12.3.6)
- 通过判据：与架构 6.14 一致

### Task M6-2: OutboxNotifier

修改位置：
- `app/domain/ports/outbox_notifier.py`（新增）
- `app/adapters/outbox/in_process_notifier.py`（新增）

数据输入：
- 来源：架构文档 6.15
- 类型：OutboxNotifier(ABC) + InProcessNotifier
- 约束：只保存版本号；commit 后才 notify；唤醒等待者

处理指令：
1. OutboxNotifier ABC: current_version(), wait_for_change(after_version, timeout), notify_committed(cursor)
2. InProcessNotifier:
   a. _version: int = 0
   b. _event: asyncio.Event 或 asyncio.Condition
   c. current_version() → 返回当前版本号
   d. notify_committed(cursor) → _version += 1 → 唤醒所有等待者
   e. async wait_for_change(after_version, timeout):
      - if current_version() > after_version: return True
      - await asyncio.wait_for(event.wait(), timeout)
      - return True if 版本变化 else False (timeout)
3. 通知恰在空查询与开始等待之间时，wait 立即返回

数据输出：
- 类型：通知信号
- 去向：OutboxLongPollService
- 可观察结果：commit 后等待者被唤醒

接口关系：
TurnRunner → enqueue_once → Notifier.notify_committed
OutboxLongPollService → Notifier.wait_for_change

测试：
- 先失败：`async def test_notifier_version_increments()` — notify 后版本增加
- 先失败：`async def test_notifier_wait_returns_on_notify()` — 等待中被唤醒
- 先失败：`async def test_notifier_wait_timeout()` — 超时返回 False
- 先失败：`async def test_notifier_immediate_return_if_already_changed()` — 版本已变立即返回 (12.3.9)
- 通过判据：与架构 6.15 一致

### Task M6-3: SingleReaderGate + OutboxLongPollService

修改位置：
- `app/application/outbox_longpoll_service.py`（新增）

数据输入：
- 来源：架构文档 2.8, 6.16
- 类型：SingleReaderGate + OutboxLongPollService
- 约束：同时只一个活动读取者；循环 claim → wait → claim；不持有连接等待

处理指令：
1. SingleReaderGate:
   a. try_acquire() -> bool（非阻塞）
   b. release()
2. OutboxLongPollService(outbox_store, notifier, gate):
   a. async poll(after, timeout_seconds, reader_id) -> LongPollResponse
   b. gate.try_acquire() → False → raise ReaderBusy
   c. deadline = monotonic_now + timeout
   d. LOOP:
      - v = notifier.current_version()
      - msg = outbox_store.claim_one(after, reader_id)
      - msg != None → gate.release() → return LongPollResponse(item=msg, next_cursor=msg.cursor, timed_out=False)
      - remaining = deadline - monotonic_now
      - remaining <= 0 → gate.release() → return LongPollResponse(item=None, next_cursor=after, timed_out=True)
      - await notifier.wait_for_change(v, remaining)
      - 被唤醒但无匹配消息 → 继续循环，不重置 deadline
   e. finally → gate.release()

数据输出：
- 类型：LongPollResponse(item, next_cursor, timed_out)
- 去向：OutboxController
- 可观察结果：有消息返回一条；无消息等待或超时

接口关系：
OutboxController → OutboxLongPollService → SQLiteOutboxStore + Notifier

测试：
- 先失败：`async def test_longpoll_returns_message_on_commit()` — commit 后返回一条 (12.3.1)
- 先失败：`async def test_longpoll_timeout_empty_result()` — 超时空结果 cursor 不变 (12.3.3)
- 先失败：`async def test_longpoll_second_reader_busy()` — 第二个 409 reader_busy (12.3.4)
- 先失败：`async def test_longpoll_no_held_connection_during_wait()` — 等待期间无连接 (12.3.6)
- 先失败：`async def test_longpoll_unrelated_notification_continues_wait()` — 无关通知继续等待 (12.3.10)
- 先失败：`async def test_longpoll_notify_between_query_and_wait()` — 查询与等待间通知立即返回 (12.3.9)
- 通过判据：与架构 6.16 一致

### Task M6-4: OutboxController v2（长轮询端点）

修改位置：
- `app/adapters/http/outbox_controller.py`（现有）

数据输入：
- 来源：架构文档 6.18
- 类型：GET /v1/outbox/longpoll
- 约束：鉴权；参数校验；reader_busy → 409

处理指令：
1. 新增 GET /v1/outbox/longpoll
2. 参数: after (非负整数, 默认 0), timeout (1..30, 默认 30), reader_id (默认 config 值)
3. 调用 OutboxLongPollService.poll()
4. ReaderBusy → 409 reader_busy
5. 保留 v1 GET /v1/outbox（分页）兼容

数据输出：
- 类型：LongPollResponse JSON 或 409 错误
- 去向：前端
- 可观察结果：长轮询端点可用

接口关系：
前端 → OutboxController → OutboxLongPollService

测试：
- 先失败：`async def test_longpoll_endpoint_returns_message()` — 200 + item
- 先失败：`async def test_longpoll_endpoint_timeout()` — 200 + item=null + timed_out=true
- 先失败：`async def test_longpoll_endpoint_reader_busy()` — 409
- 通过判据：与架构 6.18 一致

### Task M6-5: TurnRunner 主动回合集成 Notifier

修改位置：
- `app/application/turn_runner.py`（M0 已改）

数据输入：
- 来源：架构文档 7.2, 7.4
- 类型：TurnRunner + Notifier
- 约束：commit 后才 notify；写入失败不 notify

处理指令：
1. TurnRunner 接受 outbox_notifier
2. 主动回合:
   a. enqueue_once → 成功 → notifier.notify_committed(cursor)
   b. enqueue_once → 失败 → 不 notify → ActiveTurnResult(outcome="failed")
3. <NO_MESSAGE> → 不写 Outbox，不 notify

数据输出：
- 类型：ActiveTurnResult
- 去向：WakeController
- 可观察结果：commit 后 notify；失败不 notify

接口关系：
TurnRunner → OutboxStore.enqueue_once → Notifier.notify_committed

测试：
- 先失败：`async def test_active_turn_notifies_on_commit()` — commit 后 notifier 被调用 (12.3.1)
- 先失败：`async def test_active_turn_no_notify_on_failure()` — 写入失败不 notify (12.3.2)
- 通过判据：与架构 7.2 一致

### M6 全量回归

运行：`uv run pytest -v`
反馈点 M6：Outbox commit 后唤醒长轮询 — `async test_commit_wakes_longpoll()`

---

## M7: 并发、全量回归、真实 API 与重启冒烟

**依赖:** M0—M6
**验收:** 12.4—12.5

### Task M7-1: 并发隔离测试

修改位置：
- `tests/integration/test_concurrency_isolation.py`（新增）

数据输入：
- 来源：架构文档 12.4
- 类型：并发集成测试
- 约束：用户+主动并行；两个并行回合不串线

处理指令：
1. test_user_turn_waiting_llm_outbox_responds: 用户请求等待 LLM 时 Outbox 请求可响应 (12.4.1)
2. test_user_and_active_parallel_llm: 两个上游请求可重叠 (12.4.2)
3. test_parallel_turns_no_crosstalk: messages、计数、tool_call_id 不串线 (12.4.3)
4. 使用 asyncio.gather 并发执行

数据输出：
- 类型：测试结果
- 去向：pytest
- 可观察结果：并发回合隔离正确

接口关系：
测试 → TurnRunner(并发) → 验证隔离

测试：
- 先失败：上述三个测试
- 通过判据：12.4.1—3

### Task M7-2: 重启恢复测试

修改位置：
- `tests/integration/test_restart_recovery.py`（新增）

数据输入：
- 来源：架构文档 12.2.6, 11—13
- 类型：重启恢复集成测试
- 约束：pending 保留；running → expired(crash_recovery)；Outbox 不重复

处理指令：
1. test_restart_preserves_pending_wakejob: 写入 pending → 模拟重启 → 任务保留 (12.2.6)
2. test_restart_running_to_expired: 模拟 running → 重启 → expired(crash_recovery) (12.2.12)
3. test_restart_missed_deadline: 超宽限 pending → 重启 → expired(missed_deadline) (12.2.11)
4. test_restart_outbox_not_duplicated: Outbox 已提交但 WakeJob 未完成崩溃 → 重启不重复写 (12.2.13)

数据输出：
- 类型：测试结果
- 去向：pytest
- 可观察结果：重启恢复正确

接口关系：
测试 → WakeJobStore.recover_after_restart → 验证恢复

测试：
- 先失败：上述四个测试
- 通过判据：12.2.6, 11—13

### Task M7-3: 真实 API 工具冒烟

修改位置：
- `tests/smoke/test_real_api_tool.py`（新增）

数据输入：
- 来源：架构文档 12.5.3, 提示词第六节凭据
- 类型：真实 LLM 工具调用冒烟
- 约束：使用 .env 中的真实凭据；不输出 Key

处理指令：
1. 读取 .env 中的 UPSTREAM_BASE_URL, UPSTREAM_API_KEY, UPSTREAM_MODEL
2. 创建真实 AsyncModelClient + ToolRegistry（注册 get_server_time）
3. 发送请求引导模型调用 get_server_time
4. 验证模型成功调用工具并给出最终文本
5. 无凭据时标记 BLOCKED_EXTERNAL_CREDENTIAL 并 skip
6. 不在日志或输出中包含 API Key

数据输出：
- 类型：冒烟测试结果
- 去向：pytest（默认 skip，有凭据时运行）
- 可观察结果：get_server_time 工具闭环

接口关系：
测试 → AsyncModelClient → 真实 LLM → get_server_time → 最终文本

测试：
- 标记 @pytest.mark.skipif(无凭据)
- 通过判据：模型调用 get_server_time 并返回最终文本

### Task M7-4: 操作性冒烟（启动/关闭/重启）

修改位置：
- `tests/smoke/test_operational_smoke.py`（新增）

数据输入：
- 来源：架构文档 12.5.4
- 类型：端到端操作性测试
- 约束：从零启动、健康检查、重启恢复

处理指令：
1. test_service_start_health_check: uvicorn 启动 → HTTP 200 健康检查
2. test_service_shutdown_clean: shutdown 后 Scheduler 和 AsyncClient 已关闭
3. test_service_restart_recovery: 写入数据 → 重启 → 数据恢复

数据输出：
- 类型：测试结果
- 去向：pytest
- 可观察结果：服务可启动、可关闭、可恢复

接口关系：
测试 → uvicorn → FastAPI lifespan → 验证

测试：
- 先失败：上述三个测试
- 通过判据：12.5.4

### Task M7-5: 凭据清理检查

修改位置：
- 无（验证步骤）

数据输入：
- 来源：架构文档 12.5.5, 提示词第六节
- 类型：安全检查
- 约束：代码与日志无真实 Key

处理指令：
1. 搜索代码中硬编码的 API Key
2. 搜索测试输出和日志中的 Key
3. 验证 .env 在 .gitignore 中
4. 验证 .env.example 只含空占位符

数据输出：
- 类型：检查结果
- 去向：验证报告
- 可观察结果：无真实 Key 泄漏

接口关系：
N/A

测试：
- 通过判据：无真实 Key 出现在代码/日志/git

### M7 全量回归

运行：`uv run pytest -v`
反馈点 M7：全量回归 + 真实工具冒烟 + 重启恢复

---

## 验收追踪矩阵

### 12.1 工具循环验收

| # | 场景 | 测试 | 命令 | 预期 | 里程碑 |
|---|---|---|---|---|---|
| 1 | 三轮工具调用 | test_loop_three_rounds | `uv run pytest tests/unit/test_model_tool_loop.py::test_loop_three_rounds` | 顺序和 ID 配对正确 | M3 |
| 2 | 多工具同响应 | test_loop_multiple_tools_same_response | 同上文件 | 按返回顺序执行 | M3 |
| 3 | 工具不存在 | test_dispatcher_tool_not_found | `uv run pytest tests/unit/test_tool_dispatcher.py::test_dispatcher_tool_not_found` | tool_not_found 回灌 | M2 |
| 4 | 参数无效 | test_dispatcher_invalid_arguments | 同上 | invalid_arguments 回灌 | M2 |
| 5 | 工具超时 | test_dispatcher_timeout | 同上 | 15秒内终止 | M2 |
| 6 | 固定失败 | test_fail_test_raises | `uv run pytest tests/unit/test_test_tools.py::test_fail_test_raises` | fail_test 错误被模型收到 | M2 |
| 7 | 超五轮 | test_loop_exceeds_five_rounds | M3 测试文件 | 不再调用模型 | M3 |
| 8 | 超十次 | test_loop_exceeds_ten_calls | M3 测试文件 | 不执行第 11 次 | M3 |
| 9 | 9次后请求3个 | test_loop_batch_exceeds_at_nine | M3 测试文件 | 整批不执行 | M3 |
| 10 | 首次11个 | test_loop_first_response_eleven_tools | M3 测试文件 | 整批不执行 | M3 |
| 11 | 9次后请求1个 | test_loop_nine_then_one | M3 测试文件 | 执行第10个后终止 | M3 |
| 12 | 生产配置 | test_registry_schemas_excludes_test_tools | M2 测试文件 | echo/delay/fail 不出现 | M2 |
| 13 | 前端上传工具 | test_chat_request_with_tools_rejected | M2 测试文件 | 400 client_tools_not_allowed | M2 |

### 12.2 唤醒验收

| # | 场景 | 测试 | 里程碑 |
|---|---|---|---|
| 1 | 两个同时到期 | test_two_due_jobs_one_running_one_expired | M5 |
| 2 | 超过下次启动时间 | test_missed_deadline_expired | M5 |
| 3 | 凌晨任务到期 | test_start_policy_outside_window | M4 |
| 4 | 第11次到期 | test_start_policy_daily_limit | M4 |
| 5 | 间隔不足 | test_admission_min_interval_rejected | M4 |
| 6 | 三个月后重启 | test_restart_preserves_pending_wakejob | M7 |
| 7 | 重复 wake_id | test_wake_store_schedule_once_idempotent | M4 |
| 8 | 随机重复扫描 | test_random_planner_persists_once | M5 |
| 9 | 用户等待时主动启动 | test_user_and_active_parallel_llm | M7 |
| 10 | 主动回合已运行 | test_active_turn_running_new_job_expired | M5 |
| 11 | 跨 grace 停机 | test_restart_missed_deadline | M7 |
| 12 | 重启旧 running | test_restart_running_to_expired | M7 |
| 13 | Outbox 已提交崩溃 | test_restart_outbox_not_duplicated | M7 |
| 14 | 固定规划器重算 | test_fixed_planner_id_deterministic | M5 |

### 12.3 Outbox 长轮询验收

| # | 场景 | 测试 | 里程碑 |
|---|---|---|---|
| 1 | 写入时正在长轮询 | test_longpoll_returns_message_on_commit | M6 |
| 2 | 写入失败不 notify | test_active_turn_no_notify_on_failure | M6 |
| 3 | 长轮询超时 | test_longpoll_timeout_empty_result | M6 |
| 4 | 两个前端同时 | test_longpoll_second_reader_busy | M6 |
| 5 | 重复 trigger_id | test_claim_one_idempotent_trigger | M6 |
| 6 | 等待期间无连接 | test_longpoll_no_held_connection_during_wait | M6 |
| 7 | 多条 pending | test_claim_one_returns_oldest_pending | M6 |
| 8 | 领取后不重复 | test_claim_one_changes_status_to_claimed | M6 |
| 9 | notify 竞态 | test_longpoll_notify_between_query_and_wait | M6 |
| 10 | 无关通知 | test_longpoll_unrelated_notification_continues_wait | M6 |

### 12.4 并发与隔离验收

| # | 场景 | 测试 | 里程碑 |
|---|---|---|---|
| 1 | 用户等待时 Outbox 可响应 | test_user_turn_waiting_llm_outbox_responds | M7 |
| 2 | 用户+主动同时调模型 | test_user_and_active_parallel_llm | M7 |
| 3 | 并行回合不串线 | test_parallel_turns_no_crosstalk | M7 |
| 4 | 未 startup 不启动 | test_scheduler_not_started_in_constructor | M5 |
| 5 | 单次 startup | test_scheduler_starts_on_lifespan | M5 |
| 6 | shutdown 关闭 | test_scheduler_stops_on_shutdown | M5 |

### 12.5 回归与真实集成验收

| # | 验收项 | 测试 | 里程碑 |
|---|---|---|---|
| 1 | v1 全量回归 | `uv run pytest` | M0-M7 每里程碑 |
| 2 | 工具 Mock 测试 | M2-M3 测试 | M3 |
| 3 | 真实模型工具冒烟 | test_real_api_tool | M7 |
| 4 | 操作性冒烟 | test_operational_smoke | M7 |
| 5 | 凭据清理 | M7-5 检查 | M7 |

---

## 尚未实现的第 14 节扩展能力

- 14.1 状态自动演化（StateWorker + SamplePublisher）
- 14.2 可靠 ACK 与重新投递
- 14.3 多设备优先级
- 14.4 前端工具
- 14.5 多实例部署

## 已知风险和下一步建议

1. **asyncio 测试插件**: 需确认 pytest-asyncio 或 anyio 已安装；若未安装需在 dev 依赖中添加
2. **SQLite 短连接性能**: 频繁开关连接可能有性能影响，但 v2 优先正确性
3. **时区处理**: Asia/Shanghai 需要确保系统已安装 tzdata
4. **httpx.AsyncClient 共享**: lifespan 创建的共享 client 需在 shutdown 时正确关闭
