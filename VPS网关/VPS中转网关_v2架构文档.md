# 沉的 VPS 中转网关 v2——工具、唤醒与长轮询架构文档

> 文档状态：v2 编码基线（用户已冻结业务规则；运行参数可由 `.env` 覆盖）  
> 运行阶段：基于 coding v1 在本地增量开发，验收后部署 VPS  
> 核心目标：在保持 v1 状态注入与 OpenAI Chat 转发闭环不变的前提下，增加 VPS 工具循环、可持久恢复的唤醒计划和单读取者 Outbox 长轮询，并保证用户回合与主动回合互不阻塞、主动回合永不并发。

---

## 1. 首版边界

### 1.1 本版必须实现

1. 保留 v1 的四份只读 Sample、ContextBuilder、OpenAI Chat 入口与普通 Outbox 存储。
2. 修正 HTTP/LLM 执行边界：用户请求等待 LLM 时，其他 HTTP 请求仍可响应。
3. 用户回合与一个主动回合允许并行调用上游 LLM。
4. 两个主动回合之间永远串行；到期时已有主动回合运行的新任务直接 `expired`。
5. 增加 VPS 内部 ToolRegistry，前端不能上传、覆盖或执行工具。
6. 实现最多 5 轮、单轮最多 10 次工具调用的模型—工具循环。
7. 第一批实现 `get_server_time`、`echo_test`、`delay_test`、`fail_test`。
8. 增加持久化 `wake_jobs`，支持固定、随机和模型工具提交的未来唤醒。
9. 增加 `schedule_wakeup`、`list_wakeups`、`cancel_wakeup` 三个 VPS 工具。
10. 唤醒时间限定在 Asia/Shanghai 的 08:00—24:00；最短间隔 20 分钟；每天最多实际启动 10 次。
11. 合法未来时间不限制最远期限；服务重启后计划时间保持不变。
12. 增加单一逻辑读取者的 Outbox HTTP 长轮询，每次最多原子领取一条消息。
13. SQLite 每次读写使用短连接；等待期间不持有数据库连接或事务。
14. 按第 12 节完成工具、唤醒、长轮询和并发冲突验收。

### 1.2 本版只保留接口，不实现逻辑

1. `SamplePublisher`：继续只保留端口，不启用状态自动发布。
2. 状态演化组件：记忆整理、情绪演化、偏好变化仍不实现。
3. Outbox 可靠 ACK：保留未来扩展位置，不实现重新投递。
4. 多设备优先级：不实现平板、电脑、手机竞争与排序。
5. 前端工具：不实现 `tool_origin=frontend`。

### 1.3 本版明确不做

1. 前端上传、替换或覆盖工具 Schema。
2. 工具并行执行。
3. 工具自动重试。
4. 已过期唤醒补跑。
5. 主动回合排队等待前一个主动回合结束。
6. 多个活动 Outbox 长轮询读取请求。
7. Outbox 领取后的可靠 ACK、租约和重新投递。
8. 多进程/多实例部署。
9. PostgreSQL、Redis、消息队列等分布式基础设施。
10. Sample 自动写回和角色状态组件实现。

---

## 2. 核心术语

### 2.1 用户回合

由 `POST /v1/chat/completions` 触发。用户回合不占用主动回合锁，因此可以与一个主动回合同时调用 LLM。每个用户回合拥有独立 `TurnContext`，不得共享 messages、tool history 或调用计数。

### 2.2 主动回合

由 `wake_jobs` 中到期任务触发。任何时刻最多一个主动回合处于 `running`。主动回合到期时若另一个主动回合仍在运行，当前任务直接变为 `expired`，不等待、不补跑。

### 2.3 TurnContext

单个回合的临时上下文，包含：

- `turn_id`
- 触发类型与触发 ID
- 注入后的 messages
- 本轮工具调用历史
- 工具轮次与总调用次数
- Sample 版本

生命周期只覆盖当前回合。回合结束后销毁，不写入 Sample。

### 2.4 ToolRegistry

VPS 内部工具注册表。它是工具名称到 Schema、参数校验器和执行器的唯一映射。

- 只有服务器启动代码可以注册工具。
- 前端请求中的 `tools`、`tool_choice` 不作为可信工具来源。
- 模型只能看到 Registry 当前允许暴露的工具。

### 2.5 工具轮次

一次“LLM 返回一组 `tool_calls` → VPS 顺序执行 → 结果回灌 LLM”称为一轮。最多 5 轮；所有轮次累计最多执行 10 个工具。

### 2.6 WakeJob

一次未来主动唤醒计划。可由固定规则、随机规划器或 `schedule_wakeup` 工具创建。计划可以有多个，但执行器始终单线运行。

### 2.7 Outbox 领取

长轮询读取者从 `pending` 消息中按 cursor 顺序原子领取一条，将其改为 `claimed` 后返回。v2 不实现 ACK；网络在领取后中断时，消息可能无法展示且不会自动重投。

### 2.8 SingleReaderGate

保证同时只有一个活动 Outbox 长轮询请求。第二个请求立即得到 `reader_busy`，不进入等待。

---

## 3. 总体架构

```text
                             四份只读 Sample
                                    │
                                    ▼
                         ┌─────────────────────┐
用户 HTTP ──────────────→│                     │
                         │     TurnRunner      │──→ OutboxWriter ──→ SQLite
WakeController ─────────→│                     │
                         └──────────┬──────────┘
                                    │
                                    ▼
                           ModelToolLoop
                      ┌─────────────┴─────────────┐
                      │                           │
                 Async ModelClient           ToolRegistry
                      │                           │
                      ▼                           ▼
                  上游 LLM                 VPS ToolExecutor

用户回合：不经过 ActiveTurnGate，可与一个主动回合并行
主动回合：WakeController → ActiveTurnGate → TurnRunner，永远串行

Outbox 写入：commit → OutboxNotifier.notify()

单一前端：
GET /v1/outbox/longpoll
       │
       ▼
SingleReaderGate → claim_one()
       │ 无消息
       ▼
释放 SQLite → 等待 Notifier → 再次 claim_one() → 返回一条/空页
```

### 3.1 依赖方向

```text
HTTP / Wake 适配器 ──→ 应用层 ──→ 领域模型与端口
SQLite / OpenAI / Tool 适配器 ──→ 领域端口

Bootstrap / AppFactory：只负责把所有实现注入端口
```

禁止：

- HTTP Controller 自己执行工具或拼接工具历史。
- ToolExecutor 直接调用 TurnRunner。
- WakeController 直接调用 OpenAI 客户端。
- OutboxStore 管理长轮询等待。
- OutboxNotifier 持久化消息。
- 用户回合获取 ActiveTurnGate。
- 任意回合共享可变 messages 或工具计数器。
- 前端请求改变服务器工具注册表。

### 3.2 并发边界

```text
用户回合 A ──────────────→ LLM request A
                              │ 可并行
主动回合 B → ActiveTurnGate → LLM request B

主动回合 C 到期
  ├─ Gate 空闲 → running
  └─ Gate 已锁 → expired
```

允许同时存在：

- 一个或多个 HTTP 请求处理任务；
- 最多一个主动回合；
- 一个活动 Outbox 长轮询等待。

不允许同时存在：

- 两个主动回合；
- 两个活动长轮询读取者；
- 同一回合内两个并行工具执行器。

---

## 4. 建议目录结构

```text
app/
├── domain/
│   ├── models/
│   │   ├── chat_completion.py    # 扩展 tool_calls/tool message
│   │   ├── tool.py
│   │   ├── turn.py               # TurnContext/工具计数
│   │   ├── wake_job.py
│   │   └── outbox.py             # pending/claimed
│   └── ports/
│       ├── model_client.py        # async complete
│       ├── tool_executor.py
│       ├── wake_job_store.py
│       ├── outbox_store.py
│       └── outbox_notifier.py
├── application/
│   ├── turn_runner.py
│   ├── model_tool_loop.py
│   ├── tool_dispatcher.py
│   ├── wake_controller.py
│   ├── schedule_admission_policy.py
│   ├── wake_start_policy.py
│   └── outbox_longpoll_service.py
├── adapters/
│   ├── http/
│   │   ├── chat_controller.py
│   │   └── outbox_controller.py
│   ├── models/
│   │   └── openai_upstream_client.py
│   ├── tools/
│   │   ├── registry.py
│   │   ├── get_server_time.py
│   │   ├── test_tools.py
│   │   └── wake_tools.py
│   ├── wakeups/
│   │   └── sqlite_wake_job_store.py
│   ├── outbox/
│   │   ├── sqlite_outbox_store.py
│   │   └── in_process_notifier.py
│   └── scheduler/
│       └── wake_scheduler.py
├── infrastructure/
│   ├── config.py
│   ├── logging.py
│   └── lifecycle.py
└── tests/
    ├── unit/
    ├── integration/
    └── smoke/
```

---

## 5. 数据合同

### 5.1 ChatMessage

```text
ChatMessage
  role: system | user | assistant | tool
  content: string | null
  tool_calls: ToolCall[] | null
  tool_call_id: string | null
```

校验规则：

- `assistant` 有 `tool_calls` 时允许 `content=null`。
- `tool` 必须有 `tool_call_id` 和字符串 content。
- `system/user` 不允许携带 `tool_calls`。
- 每条 tool message 必须能匹配当前回合已出现的 tool call ID。

### 5.2 ToolDefinition

```text
ToolDefinition
  name: 唯一名称
  description: 给模型的用途说明
  parameters: JSON Schema
  enabled_in_production: bool
  timeout_seconds: int
  max_result_chars: int
```

约束：

- 工具名只允许服务器注册。
- 参数 Schema 在执行前校验。
- 测试工具在生产配置下不得暴露给模型。

### 5.3 ToolCall 与 ToolResult

```text
ToolCall
  id: 上游 LLM 返回的 tool_call_id
  name: 工具名
  arguments_json: 原始 JSON 字符串

ToolResult
  tool_call_id: 与 ToolCall.id 相同
  ok: bool
  content: 返回模型的字符串
  error_code: 可选
  truncated: bool
  duration_ms: int
```

错误也转换成 ToolResult，并以 tool message 回灌模型；只有达到回合上限时不再调用模型。

### 5.4 TurnContext

```text
TurnContext
  turn_id: UUID
  trigger_type: user | wake
  trigger_id: request_id | wake_id
  messages: 当前回合独立消息数组
  sample_versions: 四类 Sample 版本
  tool_round: 0..5
  total_tool_calls: 0..10
  started_at: ISO8601
```

### 5.5 WakeJob

```text
WakeJob
  wake_id: 唯一幂等 ID
  source: fixed | random | tool
  requested_at: 模型/规则原始时间
  scheduled_at: Asia/Shanghai 规范化后的执行时间
  reason: 唤醒理由
  status: pending | running | completed | expired | failed | cancelled
  created_at: 创建时间
  started_at: 实际启动时间，可空
  finished_at: 结束时间，可空
  expire_reason: active_turn_running | outside_window | daily_limit | min_interval | missed_deadline | crash_recovery | null
```

约束：

- `scheduled_at` 必须落在 08:00—24:00。
- 不限制距离现在的最大未来天数。
- 同一 `wake_id` 重复提交不产生第二条记录。
- `running` 状态全局最多一条。
- 只有 `scheduled_at <= now <= scheduled_at + WAKE_START_GRACE_SECONDS` 的任务可以开始。
- 超过宽限窗口仍为 pending 的任务必须转为 `expired(missed_deadline)`，不得补跑。

### 5.6 OutboxMessage v2

```text
OutboxMessage
  cursor: 单调递增读取顺序
  event_id: 唯一 ID
  trigger_id: 主动回合 ID，唯一
  content: 消息正文
  status: pending | claimed
  created_at: 创建时间
  claimed_at: 领取时间，可空
  claimed_by: 逻辑读取者 ID，可空
  metadata: 模型、Sample 版本、回合 ID
```

### 5.7 LongPollRequest / Response

```text
LongPollRequest
  after: 非负 cursor，默认 0
  timeout_seconds: 1..30
  reader_id: 固定逻辑读取者

LongPollResponse
  item: OutboxMessage | null
  next_cursor: 有消息时等于 item.cursor；空结果时保持 after
  timed_out: bool
```

### 5.8 ActiveTurnResult

```text
ActiveTurnResult
  wake_id
  outcome: message_enqueued | no_message | failed | expired
  event_id: 可空
  error_code: 可空
```

---

## 6. 模块、输入输出、指令与接口

### 6.1 AsyncModelClient

```text
输入: ModelCompletionInput(messages, tools, tool_choice, 参数)
指令:
  1. 使用服务器固定上游与 API Key
  2. 异步发送 OpenAI Chat 请求
  3. 等待期间让出 HTTP 执行权
  4. 保留 assistant content、tool_calls、finish_reason
  5. 超时/协议错误转换成领域错误
输出: ChatCompletionResponse
```

接口：

```python
class ModelClient(ABC):
    async def complete(self, request: ModelCompletionInput) -> ChatCompletionResponse: ...
```

### 6.2 ToolRegistry

```text
输入: 服务器启动时提供的 ToolDefinition + ToolExecutor
指令:
  1. 校验工具名唯一
  2. 根据运行环境过滤测试工具
  3. 向模型输出允许工具的 Schema
  4. 按名称解析执行器
输出: tools[] 或 ToolExecutor
```

接口：

```python
class ToolRegistry:
    def schemas(self) -> list[ToolDefinition]: ...
    def resolve(self, name: str) -> ToolExecutor | None: ...
```

### 6.3 ToolDispatcher

```text
输入: ToolCall + 当前 TurnExecutionContext
指令:
  1. 查找工具；不存在则返回 tool_not_found
  2. 解析 arguments JSON
  3. 按 Schema 校验；失败返回 invalid_arguments
  4. 按 tool_calls 返回顺序逐个执行
  5. 单个工具最多执行 15 秒
  6. 失败不重试，转换为错误 ToolResult
  7. 结果超过上限时截断并标记 truncated
输出: ToolResult
```

接口：

```python
class ToolDispatcher:
    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult: ...
```

ToolDispatcher 是解析、校验、超时、截断和错误归一化的唯一入口。

### 6.4 ToolExecutor

```text
输入: 已完成 Schema 校验的 arguments + ToolExecutionContext
指令:
  1. 只执行一个具体工具的领域动作
  2. 不查 Registry、不解析原始 JSON、不自行重试
  3. 返回具体结果或抛出内部工具异常
输出: 工具原始结果
```

接口：

```python
class ToolExecutor(ABC):
    async def execute(self, arguments: dict, context: ToolExecutionContext) -> object: ...
```

### 6.5 ModelToolLoop

```text
输入: PreparedTurn + ToolRegistry
指令:
  1. 初始化 tool_round=0、total_tool_calls=0
  2. 调用 LLM
  3. 无 tool_calls：返回最终响应
  4. 有 tool_calls：在执行整批之前检查轮次与总次数
  5. 若 total_tool_calls + batch_size > 10：整批不执行，抛 ToolLoopLimitError，不再调用 LLM
  6. 保存 assistant(tool_calls) 到本轮 messages
  7. 按返回顺序交给 ToolDispatcher 执行每个工具
  8. 将每个 ToolResult 追加为 tool message
  9. tool_round += 1，total_tool_calls 增加本批数量
 10. 若 total_tool_calls == 10：返回明确上限错误，不再调用 LLM
 11. 未达到上限：重新调用 LLM
输出: 最终 ChatCompletionResponse
```

约束：

- 最多 5 轮。
- 总工具调用最多 10 次。
- 同一批工具不并行。
- 调用额度不足以容纳整批时，整批拒绝；禁止只执行一部分。
- 工具失败结果交还模型；不自动重试。
- 上限错误不再回灌模型，直接结束当前回合。

### 6.6 TurnRunner v2

```text
输入: UserTrigger | WakeTrigger
指令:
  1. 创建独立 TurnContext
  2. 读取四份 Sample
  3. ContextBuilder 生成初始 messages
  4. 调用 ModelToolLoop
  5. 用户回合：返回最终 OpenAI Chat 响应
  6. 主动回合：<NO_MESSAGE> 则不写 Outbox
  7. 主动普通文本：enqueue_once，提交成功后通知长轮询
  8. 销毁 TurnContext
输出: ChatCompletionResponse | ActiveTurnResult
```

并发规则：

- TurnRunner 本身不持有全局可变 messages。
- 用户回合不获取 ActiveTurnGate。
- WakeController 在调用 TurnRunner 前负责 ActiveTurnGate。

### 6.7 ScheduleAdmissionPolicy

```text
输入: requested_at、当前时间、该日期已知的实际启动记录
指令:
  1. 转换为 Asia/Shanghai
  2. 检查 requested_at 位于未来
  3. 检查 08:00 <= time < 24:00
  4. 对已发生的同日记录检查当前已知的 20 分钟/10 次限制
  5. 未来状态不可预测时允许写入，由 StartPolicy 在执行时最终裁决
  6. 不检查最大未来期限
输出: accepted + normalized_time，或拒绝码 + 最早允许时间
```

拒绝码：

- `outside_active_window`
- `min_interval_not_met`
- `daily_limit_reached`
- `invalid_datetime`
- `not_in_future`

### 6.8 WakeStartPolicy

```text
输入: WakeJob、now、最近一次实际主动启动时间、scheduled_at 所属自然日的实际启动次数
指令:
  1. 检查 scheduled_at <= now <= scheduled_at + START_GRACE
  2. 超过宽限：拒绝 missed_deadline
  3. 检查执行时仍在 08:00—24:00
  4. 检查距离最近一次实际启动 >= 20 分钟
  5. 检查 scheduled_at 所属自然日实际启动次数 < 10
输出: can_start，或 expire_reason
```

### 6.9 WakeJobStore

```text
输入: WakeJob 或查询/状态转换指令
指令:
  1. 每次操作使用独立 SQLite 短连接
  2. 相同 wake_id 幂等写入
  3. 按 scheduled_at、created_at 查询到期 pending 任务
  4. 状态转换使用事务条件，防止同一任务重复启动
  5. 不在等待期间持有连接
输出: WakeJob / WakeJob[] / 状态转换结果
```

接口：

```python
class WakeJobStore(ABC):
    async def schedule_once(self, job: WakeJob) -> WakeJob: ...
    async def due_jobs(self, now, grace_seconds) -> list[WakeJob]: ...
    async def transition(self, wake_id, expected, target, reason=None) -> bool: ...
    async def list_jobs(self, filters) -> list[WakeJob]: ...
    async def cancel(self, wake_id) -> WakeJob: ...
    async def recover_after_restart(self, now, grace_seconds) -> RecoveryResult: ...
```

### 6.10 ActiveTurnGate

```text
输入: 非阻塞获取请求
指令:
  1. Gate 空闲：立即占用并返回 true
  2. Gate 已占用：立即返回 false
  3. 禁止等待 Gate
输出: bool
```

接口：

```python
class ActiveTurnGate(ABC):
    async def try_acquire(self) -> bool: ...
    def release(self) -> None: ...
```

### 6.11 WakeController

```text
输入: 按 scheduled_at、created_at 排序的到期 WakeJob 批次
指令:
  1. 对超出 START_GRACE 的任务标记 expired(missed_deadline)
  2. 用 WakeStartPolicy 过滤其他不可启动任务
  3. 从剩余任务中选择最早一条作为唯一候选
  4. 对候选调用 ActiveTurnGate.try_acquire()，绝不等待
  5. 获取失败：候选及本批其他到期任务全部 expired(active_turn_running)
  6. 获取成功：条件更新候选 pending → running
  7. 更新失败：释放 Gate；不得等待或重复启动
  8. 候选成功 running 后，本批其余到期任务立即 expired(active_turn_running)
  9. 创建独立后台任务调用 TurnRunner(WakeTrigger)，扫描循环立即返回
 10. 后台任务完成后写 completed/failed 并释放 Gate
输出: 本批状态转换结果；实际 ActiveTurnResult 由后台任务记录
```

这样即使两个任务同时到期，也只有一条能进入 TurnRunner，另一条不会等待前一条结束。

### 6.12 WakePlanner

```text
输入: 固定配置、随机配置、已有 pending WakeJob
指令:
  1. 固定规划器最多维护下一条 pending fixed 任务
  2. 固定槽位以每天 WAKE_ACTIVE_START（默认 08:00）为锚点
  3. 当日槽位 = active_start + n × fixed_interval，且必须早于 active_end
  4. 无 pending fixed 时，选择严格晚于 now 的最早槽位；当日无剩余槽位则选择次日 08:00
  5. 固定 ID = "fixed:" + scheduled_at；同一 now 在重启前后计算结果一致
  6. 随机规划器最多维护下一条 pending random 任务
  7. 无 pending random 时，以 now 为基准在 [min,max] 范围抽取一次
  8. 候选区间若落在凌晨，则改用下一个活动窗口内的合法区间
  9. 随机 ID = "random:" + scheduled_at + 生成 UUID
 10. 调用 ScheduleAdmissionPolicy 后持久化
 11. 已持久化随机时间不因扫描频率重新抽取
 12. tool 来源的远期任务不计入 fixed/random 各自的一条维护额度
输出: pending WakeJob
```

### 6.13 唤醒工具

#### schedule_wakeup

```text
输入: wake_id、requested_at、reason
指令:
  1. ScheduleAdmissionPolicy 校验
  2. 失败：返回错误码与最早允许时间，不写数据库
  3. 成功：WakeJobStore.schedule_once()
  4. 重复 wake_id：返回原记录
输出: 已接受 WakeJob 或拒绝结果
```

#### list_wakeups

```text
输入: status 可选、时间范围可选
指令: 查询并按 scheduled_at 升序返回
输出: WakeJob[]
```

#### cancel_wakeup

```text
输入: wake_id
指令:
  1. 只允许 pending → cancelled
  2. running/completed/expired 不可取消
输出: 取消结果
```

### 6.14 SQLiteOutboxStore v2

```text
输入: 新消息或领取条件
指令:
  1. 每次操作打开独立短连接
  2. enqueue_once：trigger_id 幂等写入 pending 消息
  3. claim_one：事务中选择 cursor 最小的 pending 消息
  4. 原子更新为 claimed 并返回同一行
  5. 操作结束立即提交并关闭连接
输出: OutboxMessage | null
```

接口：

```python
class OutboxStore(ABC):
    async def enqueue_once(self, message) -> OutboxMessage: ...
    async def claim_one(self, after_cursor, reader_id) -> OutboxMessage | None: ...
```

### 6.15 OutboxNotifier

```text
输入: 已成功提交的 cursor
指令:
  1. 只保存进程内通知版本，不保存消息正文
  2. commit 成功后才 notify
  3. 唤醒当前长轮询等待者
输出: 通知信号
```

接口：

```python
class OutboxNotifier(ABC):
    def current_version(self) -> int: ...
    async def wait_for_change(self, after_version: int, timeout_seconds: int) -> bool: ...
    def notify_committed(self, cursor: int) -> None: ...
```

服务重启时通知丢失不影响数据；长轮询首先查询 SQLite，可以发现重启前的 pending 消息。

### 6.16 OutboxLongPollService

```text
输入: after、timeout_seconds、reader_id
指令:
  1. 非阻塞获取 SingleReaderGate；失败返回 reader_busy
  2. 计算绝对 deadline = monotonic_now + timeout
  3. 循环：先读取 notification_version = notifier.current_version()
  4. 再执行 claim_one；有消息立即返回
  5. 无消息：计算 remaining = deadline - monotonic_now
  6. remaining <= 0：返回超时空结果
  7. remaining > 0：wait_for_change(notification_version, remaining)
  8. 若消息恰在查询与等待之间提交，版本已变化，wait 必须立即返回
  9. 被通知但没有符合 after 的 pending 消息：更新循环，不重置 deadline
 10. 等待期间不持有 SQLite 连接或事务
 11. 直到领取一条或真正到达 deadline
 12. 释放 SingleReaderGate
输出: LongPollResponse
```

### 6.17 AppLifecycle

```text
FastAPI startup:
  1. 创建共享 Async HTTP Client
  2. 初始化 OutboxNotifier、SingleReaderGate、ActiveTurnGate
  3. WakeJobStore.recover_after_restart():
     - 遗留 running → expired(crash_recovery)
     - pending 且已超过 START_GRACE → expired(missed_deadline)
     - 宽限内 pending 保留供正常扫描
  4. 启动 WakePlanner 和非阻塞扫描循环

FastAPI shutdown:
  1. 停止接收新后台任务
  2. 取消并等待 Scheduler 任务退出
  3. 等待当前主动后台任务按关闭策略结束或取消
  4. 关闭 Async HTTP Client
```

构造对象时不得自动启动 Scheduler。

### 6.18 HTTP Controllers v2

```text
ChatController 输入: OpenAI Chat HTTP 请求
指令:
  1. 沿用 v1 鉴权和基础字段校验
  2. 请求含客户端 tools 或 tool_choice：返回 400 client_tools_not_allowed
  3. 调用 async TurnRunner
  4. ToolLoopLimitError → 422 tool_loop_limit_exceeded
输出: OpenAI Chat 响应或统一错误

OutboxController 输入: after、timeout、reader_id
指令:
  1. 鉴权和范围校验
  2. 调用 OutboxLongPollService
  3. SingleReaderGate 获取失败 → 409 reader_busy
输出: LongPollResponse
```

---

## 7. 端到端数据流

### 7.1 用户回合 + 工具循环

```text
前端 POST /v1/chat/completions
  → ChatController 校验
  → TurnRunner 创建独立 TurnContext
  → 读取 Sample + 构造上下文
  → ModelToolLoop 调用 LLM
      ├─ 普通回复 → 返回前端
      └─ tool_calls
           → ToolRegistry 查找
           → 参数校验
           → VPS 顺序执行
           → tool messages 回灌
           → 再次调用 LLM
  → 达到最终回复或上限错误
```

### 7.2 主动唤醒回合

```text
WakeScheduler 扫描到期任务
  → WakeController 对批次执行 WakeStartPolicy
  → ActiveTurnGate.try_acquire()
      ├─ 失败 → 本批到期任务 expired，不等待
      └─ 成功
           → 最早候选 pending → running
           → 本批其余任务立即 expired
           → 创建独立主动后台任务，扫描循环继续
           → TurnRunner + 独立工具循环
           → <NO_MESSAGE>：completed，不写 Outbox
           → 普通消息：Outbox commit → notify → completed
           → 异常：failed
           → release Gate
```

用户回合不经过 ActiveTurnGate，所以可以与该主动回合并行。

### 7.3 schedule_wakeup

```text
LLM tool_call(schedule_wakeup)
  → ToolRegistry
  → 参数校验
  → ScheduleAdmissionPolicy
      ├─ 拒绝 → 错误 ToolResult 回灌模型
      └─ 接受 → WakeJobStore 幂等写入
                  → 成功 ToolResult 回灌模型
```

写入计划不会立刻唤醒模型。

### 7.4 随机唤醒

```text
WakePlanner 发现需要安排随机任务
  → 在配置范围内抽取 requested_at 一次
  → ScheduleAdmissionPolicy
  → 写入 pending WakeJob
  → 后续扫描只读取该固定 scheduled_at
```

扫描频率不会改变已经抽取的随机时间。

### 7.5 Outbox 长轮询

```text
前端 GET /v1/outbox/longpoll
  → SingleReaderGate
      ├─ 已占用 → 409 reader_busy
      └─ 获取成功
          → 读取 notifier version
          → 短连接 claim_one
              ├─ 有消息 → claimed → 返回一条
              └─ 无消息 → 关闭连接 → wait_for_change(旧 version)
                                      → 被唤醒 → 再次 claim_one
                                      → 返回一条或超时空结果
```

### 7.6 并行隔离

```text
User TurnContext A   → messages A → tool history A → response A
Active TurnContext B → messages B → tool history B → outbox B
```

两条链只共享只读 Sample、只读 ToolRegistry 和基础设施端口，不共享可变回合数据。

---

## 8. 错误与降级

| 错误场景 | 处理策略 | 外部可见行为 |
|---|---|---|
| 工具不存在 | 不执行，生成错误 ToolResult | 模型收到 `tool_not_found` |
| 工具参数无效 | 不执行，生成错误 ToolResult | 模型收到 `invalid_arguments` |
| 工具超时 | 取消该工具，不重试 | 模型收到 `tool_timeout` |
| 工具内部失败 | 捕获并转成错误结果 | 模型收到 `tool_failed` |
| 超过 5 轮 | 停止，不再调用模型 | 用户回合明确错误；主动回合 failed |
| 超过 10 次工具调用 | 同上 | `tool_call_limit_exceeded` |
| 主动任务到期时已有主动回合 | 不等待 | WakeJob → expired |
| pending 超过启动宽限 | 不补跑 | expired(missed_deadline) |
| 重启发现遗留 running | 不恢复执行 | expired(crash_recovery) |
| 到期时处于凌晨 | 不调用 LLM | WakeJob → expired |
| 当天已启动 10 次 | 不调用 LLM | WakeJob → expired |
| 最短间隔不足 | 不调用 LLM | WakeJob → expired |
| 唤醒写入重复 ID | 返回已有记录 | 不产生重复任务 |
| Outbox 写入失败 | 不发送 notify | 主动回合 failed |
| 长轮询超时 | 返回空结果 | `item=null`，cursor 不变 |
| 第二个读取者到达 | 不进入等待 | 409 `reader_busy` |
| 领取后网络断开 | v2 不重新投递 | 消息保持 claimed |
| Sample identity 失败 | 沿用 v1：拒绝回合 | 503 |
| 其他 Sample 失败 | 沿用 v1：空值降级 | 正常回合 + 告警 |

---

## 9. 配置接口

| 变量名 | 说明 | 默认值 | 必填 |
|---|---|---|---|
| `TOOLS_ENABLED` | 启用 VPS 工具 | `true` | 否 |
| `TEST_TOOLS_ENABLED` | 暴露 echo/delay/fail | `false` | 否 |
| `TOOL_MAX_ROUNDS` | 工具循环轮数 | `5` | 否 |
| `TOOL_MAX_CALLS_PER_TURN` | 每回合工具总次数 | `10` | 否 |
| `TOOL_TIMEOUT_SECONDS` | 单工具超时 | `15` | 否 |
| `TOOL_MAX_RESULT_CHARS` | 工具结果字符上限 | `12000` | 否 |
| `WAKE_ENABLED` | 启用主动唤醒 | `true` | 否 |
| `WAKE_TIMEZONE` | 唤醒时区 | `Asia/Shanghai` | 否 |
| `WAKE_ACTIVE_START` | 允许开始时间 | `08:00` | 否 |
| `WAKE_ACTIVE_END` | 允许结束边界 | `24:00` | 否 |
| `WAKE_MIN_INTERVAL_MINUTES` | 实际启动最短间隔 | `20` | 否 |
| `WAKE_DAILY_LIMIT` | 每日实际启动次数 | `10` | 否 |
| `WAKE_SCAN_INTERVAL_SECONDS` | 到期扫描间隔 | `5` | 否 |
| `WAKE_START_GRACE_SECONDS` | 正常扫描延迟宽限 | `10` | 否 |
| `WAKE_FIXED_ENABLED` | 固定唤醒规划 | `true` | 否 |
| `WAKE_FIXED_INTERVAL_MINUTES` | 固定间隔 | `60` | 否 |
| `WAKE_RANDOM_ENABLED` | 随机唤醒规划 | `false` | 否 |
| `WAKE_RANDOM_MIN_MINUTES` | 随机最短时间 | `20` | 否 |
| `WAKE_RANDOM_MAX_MINUTES` | 随机最长时间 | `180` | 否 |
| `WAKE_DATABASE_PATH` | WakeJob SQLite | `./data/wake_jobs.sqlite3` | 否 |
| `OUTBOX_LONGPOLL_TIMEOUT_SECONDS` | 最长等待 | `30` | 否 |
| `OUTBOX_READER_ID` | 唯一逻辑读取者 | `chen-primary` | 否 |

配置校验规则：

- 工具轮次必须 `1..5`，工具总次数必须 `1..10`。
- 工具超时必须 `1..15` 秒。
- 随机最大间隔必须不小于随机最小间隔。
- 启用固定规划时固定间隔必须不少于 20 分钟。
- START_GRACE 必须不小于一次扫描间隔，且不超过 60 秒。
- 所有随机/固定结果最终仍受 08:00—24:00、20 分钟、每日 10 次约束。
- v2 不设置 `WAKE_MAX_FUTURE_DAYS`。

---

## 10. 本地启动与部署边界

### 10.1 本地阶段

- 在现有 v1 项目中增量修改，不重新搭建仓库。
- 启动命令沿用 v1 的 `uv run uvicorn app.main:app`。
- 使用单进程运行；禁止 `--workers > 1`。
- 开发热重载时，Scheduler 只能在 FastAPI lifespan startup 中启动。
- 测试工具只在测试配置下注册。

### 10.2 部署阶段

- 继续单进程部署。
- SQLite 与 Sample 目录必须持久化。
- FastAPI shutdown 必须等待 WakeController 停止。
- 服务重启后重新读取 pending WakeJob 和 pending Outbox。
- 本地与 VPS 只更换环境变量和持久化路径。

---

## 11. 可观测数据

| 指标/日志 | 记录时机 | 用途 |
|---|---|---|
| `turn_started/completed/failed` | 每个用户/主动回合 | 验证并发与隔离 |
| `tool_call_started/completed/failed` | 每个工具 | 配对、耗时、错误回灌 |
| `tool_loop_limit_reached` | 达到轮次/次数上限 | 验证镇定边界 |
| `wake_scheduled` | WakeJob 写入 | 验证未来时间与来源 |
| `wake_started/completed/expired/failed` | 状态转换 | 验证单线执行 |
| `wake_expire_reason` | 任务过期 | 区分冲突/凌晨/次数 |
| `outbox_committed` | SQLite commit 后 | 与 notify 顺序核对 |
| `outbox_notified` | 提交后通知 | 长轮询反馈链 |
| `outbox_claimed` | 前端领取 | 验证最多一个读取者 |
| `longpoll_started/woken/timed_out/reader_busy` | 长轮询生命周期 | 诊断投递等待 |

所有日志必须带 `turn_id` 或 `wake_id`；并行回合不得混用 correlation ID。

---

## 12. 验收基线

### 12.1 工具循环验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | LLM 连续调用工具三轮 | 调用顺序和 tool_call_id 配对正确 |
| 2 | 同一响应含多个工具 | 严格按返回顺序执行 |
| 3 | 工具名称不存在 | 不执行，错误回灌模型 |
| 4 | 工具参数无效 | 不执行，错误回灌模型 |
| 5 | 工具超时 | 15 秒内终止，不自动重试 |
| 6 | 工具固定失败 | fail_test 错误被模型收到 |
| 7 | 超过五轮 | 不再调用模型，返回明确错误 |
| 8 | 超过十次调用 | 不执行第 11 次，不再调用模型 |
| 9 | 已执行 9 次后模型一次请求 3 个工具 | 整批 3 个均不执行，明确超限错误 |
| 10 | 首次响应一次请求 11 个工具 | 整批不执行，明确超限错误 |
| 11 | 已执行 9 次后模型请求 1 个工具 | 执行第 10 个后终止，不再调用模型 |
| 12 | 生产配置 | echo/delay/fail 不出现在工具 Schema |
| 13 | 前端上传工具 | 不进入 Registry、不转发为可执行工具 |

### 12.2 唤醒验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | 两个主动任务同时到期 | 一个 running，另一个 expired |
| 2 | 当前回合超过下次启动时间 | 下次任务 expired，不补跑 |
| 3 | 凌晨任务到期 | 不调用 LLM，直接 expired |
| 4 | 当天第 11 次任务到期 | 不调用 LLM |
| 5 | 间隔不足 20 分钟 | schedule 拒绝或到期 expired |
| 6 | 三个月后的任务重启恢复 | scheduled_at 完全不变 |
| 7 | 相同 wake_id 重复提交 | 数据库只有一条 |
| 8 | 随机任务重复扫描 | scheduled_at 不重新随机 |
| 9 | 用户回合正在等待 LLM | 主动回合仍可启动并行 LLM 请求 |
| 10 | 主动回合已运行 | 第二个主动回合绝不进入 TurnRunner |
| 11 | 服务停机跨过 scheduled_at + grace | 重启后任务 expired，不调用 LLM |
| 12 | 重启发现旧 running | 转为 expired(crash_recovery)，不重新执行 |
| 13 | Outbox 已提交但 WakeJob 未完成时崩溃 | 重启不重复写 Outbox；旧 running 不重跑 |
| 14 | 固定规划器在相同 now 重启重算 | 仍得到同一个 08:00 锚定的下一槽位和 wake_id |

### 12.3 Outbox 长轮询验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | 写入时前端正在长轮询 | commit 后唤醒并返回一条 |
| 2 | 写入失败 | 不触发 notify |
| 3 | 长轮询超时 | 空结果，cursor 不变 |
| 4 | 两个前端同时轮询 | 一个等待，另一个 reader_busy |
| 5 | 同一 trigger_id 重复写入 | Outbox 仍只有一条 |
| 6 | 等待期间检查 SQLite | 无长期 connection/transaction |
| 7 | 一次存在多条 pending | 每次只领取 cursor 最小的一条 |
| 8 | 领取完成 | 同一消息不会再次被 operational longpoll 返回 |
| 9 | notify 恰发生在空查询与开始等待之间 | wait_for_change 立即返回，不睡到超时 |
| 10 | 收到与 after 无关的通知 | 继续按剩余时间等待，不提前返回、不重置 deadline |

### 12.4 并发与隔离验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | 用户请求等待 LLM 时查询 Outbox | Outbox 请求仍可响应 |
| 2 | 用户请求与主动回合同时调用模型 | 两个上游请求可重叠 |
| 3 | 两个并行回合均调用工具 | messages、计数、tool_call_id 不串线 |
| 4 | FastAPI 重复 create_app 但未 startup | 不启动 Scheduler |
| 5 | 单次 startup | 只产生一个 WakeController 任务 |
| 6 | shutdown | Scheduler 和 Async HTTP Client 均已关闭 |

### 12.5 回归与真实集成验收

| # | 验收项 | 可观测判据 |
|---|---|---|
| 1 | v1 全量回归 | 原 264 项基线不得下降；若仓库现状不同，以执行前实测数为基线 |
| 2 | 工具 Mock 测试 | 覆盖成功、失败、超时、上限 |
| 3 | 真实模型工具冒烟 | 模型成功调用 get_server_time 并给出最终文本 |
| 4 | 操作性冒烟 | 从零启动、健康检查、重启后 WakeJob/Outbox 恢复 |
| 5 | 凭据清理 | 代码与日志无真实 Key |

---

## 13. 建议实现顺序

| 里程碑 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| M0 | 异步 ModelClient、TurnRunner、lifespan；SQLite 短连接 | v1 | 12.4.1、12.4.4—6 |
| M1 | Chat/tool 数据合同扩展 | M0 | ChatMessage/ToolCall/ToolResult 合同单元测试 |
| M2 | ToolRegistry + ToolDispatcher + 四个第一批工具 | M1 | 12.1.3—6、12—13 |
| M3 | ModelToolLoop 顺序、上限与错误回灌 | M2 | 12.1.1—2、7—11 |
| M4 | WakeJobStore + Admission/Start Policy + 三个唤醒工具 | M3 | 12.2.3—7、11—12 |
| M5 | WakePlanner + WakeController + ActiveTurnGate | M4 | 12.2.1—2、8—10、14 |
| M6 | Outbox claim + Notifier + SingleReader 长轮询 | M0、M5 | 12.3 全部、12.2.13 |
| M7 | 并发、全量回归、真实 API 与重启冒烟 | M0—M6 | 12.4—12.5 |

每个里程碑结束后运行新增测试和全量回归；不得把状态组件提前插入 M0—M7。

---

## 14. 扩展方向（必填）

### 14.1 状态自动演化

**当前状态：** 四份 Sample 继续只读；`SamplePublisher` 只有端口。

**扩展路径：**

```text
回合结果/时间信号
  → StateWorker
  → 记忆与情绪计算
  → SamplePublisher 原子发布
  → 下一回合读取新 Sample
```

**需要实现的接口：**

- `StateWorker.process(turn_result)`
- `SamplePublisher.publish(sample)`

**涉及代码变更：**

- 新增状态计算模块和 Publisher 适配器。
- TurnRunner 只通过 post-turn 端口投递事件，不直接写 Sample。

### 14.2 可靠 ACK 与重新投递

**当前状态：** `claimed` 后不重新投递。

**扩展路径：**

```text
claimed → 客户端 ACK → delivered
claimed → lease 超时 → pending
```

**需要实现的接口：** `acknowledge`、`release_expired_claims`。

### 14.3 多设备优先级

**当前状态：** SingleReaderGate，只允许一个逻辑读取者。

**扩展路径：** DeviceRegistry + 在线状态 + 平板/电脑/手机领取优先级。不得只靠客户端自报优先值。

### 14.4 前端工具

**当前状态：** 所有工具均在 VPS 执行。

**扩展路径：** 为工具增加 `origin` 与 deferred tool result 协议；不得复用 VPS ToolExecutor 直接执行前端动作。

### 14.5 多实例部署

**当前状态：** 单进程、进程内 Gate/Notifier、SQLite。

**扩展路径：** PostgreSQL 原子领取、分布式 ActiveTurnGate、外部通知通道。扩展前不得启动多个 worker。

---

## 15. 架构不变量

1. 前端不能上传、覆盖或执行工具；所有工具来自 VPS ToolRegistry。
2. 同一组 tool_calls 必须按模型返回顺序执行。
3. 工具失败不自动重试，必须作为 ToolResult 回灌模型。
4. 工具循环最多 5 轮、每回合最多 10 次、单工具最多 15 秒。
5. 达到工具上限后不再调用模型。
6. 用户回合与一个主动回合允许并行。
7. 两个主动回合永远不能并行；冲突任务直接 expired。
8. 已过执行时间的主动任务不排队、不补跑。
9. 唤醒只允许 Asia/Shanghai 的 08:00—24:00，最短间隔 20 分钟，每天最多实际启动 10 次。
10. 未来唤醒没有最远期限，重启不得改变计划时间。
11. 每个回合必须拥有独立 TurnContext、messages 和工具计数。
12. Outbox 必须先 commit，后 notify。
13. 长轮询等待期间不得持有 SQLite connection 或 transaction。
14. 同时只能有一个活动长轮询读取者，每次最多领取一条。
15. SQLite 不得把同一个 connection 长期共享给 HTTP 与 Scheduler。
16. Scheduler 只能由 FastAPI lifespan 启动和停止，构造函数不得创建后台任务。
17. v2 不启用 Sample 自动回写和状态演化。
18. 新能力默认进入第 14 节，不改变已验收闭环。
