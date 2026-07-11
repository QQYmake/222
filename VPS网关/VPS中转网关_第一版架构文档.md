# 沉的 VPS 中转网关——第一版架构文档

> 文档状态：待用户审阅  
> 运行阶段：先在本地运行，验收通过后原样部署到 VPS  
> 首版协议：OpenAI Chat Completions 兼容协议  
> 核心目标：VPS 作为“沉”的持续运行主体；确定性注入状态，转发对话请求，定时产生主动消息，并通过 Outbox API 暴露待发送内容。

---

## 1. 首版边界

### 1.1 本版必须实现

1. 提供 OpenAI Chat 兼容入口 `POST /v1/chat/completions`。
2. 从四份只读 Sample 中读取“沉”的状态：
   - `identity`
   - `preferences`
   - `memories`
   - `working_state`
3. 按固定顺序把四份 Sample 拼接成一个 system message。
4. 将注入后的请求转发给一个固定的 OpenAI Chat 兼容模型服务。
5. 将上游响应按 OpenAI Chat 格式返回前端。
6. 定时触发一次主动回合，并把主动消息写入 Outbox。
7. 提供 Outbox 数据查询 API。
8. 本地重启后，Outbox 中尚未处理的消息不能丢失。

### 1.2 本版只保留接口，不实现逻辑

1. 长驻状态计算区：未来计算记忆、情绪等长期变化。
2. Sample 自动发布：未来由长驻状态计算区更新 Sample。
3. Operit 插件。
4. Operit 对 Outbox 的 HTTP 长轮询。
5. 状态自动回写、记忆整理和情绪演化算法。

### 1.3 本版明确不做

1. 多模型协议与 canonical 中间协议。
2. Anthropic Messages、Responses API 等协议适配。
3. 伪造 `tool_call` / `tool_result` 历史。
4. 多轮工具调用。
5. WebSocket、移动端推送和设备 ACK。
6. CQRS、Event Sourcing、Redis 和分布式缓存。
7. 多实例部署和分布式锁。
8. 模型自主修改核心身份。

---

## 2. 核心术语

### 2.1 Sample 缓存区

Sample 是某类状态对其他模块发布的**最新稳定快照**。

- Context Builder 只能读取 Sample。
- Sample 在一次读取过程中必须保持完整，不能读到半份数据。
- 首版使用本地 JSON 文件充当 Sample。
- 首版由人工修改 Sample；后续由长驻状态计算区原子发布。

### 2.2 长驻状态计算区

长驻状态计算区是未来持续运行的内部状态演化逻辑，不是 HTTP 长轮询。

```text
内部输入/历史状态
       │
       ▼
长驻状态计算区
  - 计算变化
  - 整合数据
  - 过滤噪声
       │
       ▼
publish_sample(type, snapshot)
       │
       ▼
只读 Sample
```

首版不实现计算规则，只预留 `SamplePublisher` 接口。

### 2.3 Outbox

Outbox 是服务器已经生成、等待前端读取的主动消息集合。首版只提供普通查询 API；以后可在相同游标模型上扩展 HTTP 长轮询。

### 2.4 被动回合与主动回合

- 被动回合：由用户发来的 OpenAI Chat 请求触发，结果立即返回调用方。
- 主动回合：由本地定时器触发，结果写入 Outbox，不要求前端在线。

二者共用同一个 `TurnRunner`，只在触发输入和结果出口上不同。

---

## 3. 总体架构

```text
                                      ┌──────────────────────────┐
                                      │ 四类只读 Sample           │
                                      │ identity                 │
                                      │ preferences              │
                                      │ memories                 │
                                      │ working_state            │
                                      └────────────┬─────────────┘
                                                   │ SampleReader
                                                   ▼
┌──────────────┐   OpenAI Chat Request   ┌──────────────────────┐
│ Operit/测试端 │ ──────────────────────→ │ Chat API Controller  │
└──────────────┘                          └──────────┬───────────┘
                                                  │ UserTrigger
┌──────────────┐                          ┌──────────▼───────────┐
│ Scheduler    │ ───── TimerTrigger ────→ │ TurnRunner           │
└──────────────┘                          │ - 读取状态             │
                                          │ - 构造上下文           │
                                          │ - 调用模型             │
                                          └──────┬────────┬──────┘
                                                 │        │
                                      被动结果返回│        │主动结果
                                                 │        ▼
                                                 │  ┌─────────────┐
                                                 │  │ OutboxStore │
                                                 │  └──────┬──────┘
                                                 │         │
                                                 │  GET /v1/outbox
                                                 │         ▼
                                                 │   Operit/测试端
                                                 ▼
                                          OpenAI Chat Response

TurnRunner ──→ ContextBuilder ──→ OpenAIUpstreamClient ──→ 上游模型
```

### 3.1 依赖方向

依赖只能沿以下方向流动：

```text
HTTP/Scheduler 适配器
        ↓
应用层 TurnRunner
        ↓
领域接口（SampleReader / ModelClient / OutboxWriter）
        ↓
本地文件、SQLite、HTTP 上游等基础设施实现
```

禁止：

- Context Builder 直接读取文件路径。
- TurnRunner 直接执行 SQL。
- Sample Repository 调用模型。
- Outbox Controller 反向触发 TurnRunner。
- 上游模型客户端修改 Sample。

---

## 4. 建议目录结构

目录只是编码导航，不要求为每个文件建立独立进程。

```text
app/
├── domain/
│   ├── models/
│   │   ├── sample.py
│   │   ├── trigger.py
│   │   ├── turn.py
│   │   └── outbox.py
│   └── ports/
│       ├── sample_reader.py
│       ├── sample_publisher.py       # 预留
│       ├── model_client.py
│       └── outbox_store.py
├── application/
│   ├── context_builder.py
│   ├── turn_runner.py
│   └── active_turn_service.py
├── adapters/
│   ├── http/
│   │   ├── chat_controller.py
│   │   └── outbox_controller.py
│   ├── samples/
│   │   └── file_sample_repository.py
│   ├── models/
│   │   └── openai_chat_client.py
│   ├── outbox/
│   │   └── sqlite_outbox_store.py
│   └── scheduler/
│       └── local_scheduler.py
├── infrastructure/
│   ├── config.py
│   ├── logging.py
│   └── bootstrap.py
├── samples/
│   ├── identity.sample.json
│   ├── preferences.sample.json
│   ├── memories.sample.json
│   └── working_state.sample.json
└── tests/
    ├── unit/
    └── integration/
```

---

## 5. 数据合同

## 5.1 SampleEnvelope

四份 Sample 使用统一外壳，`data` 内容各自不同。

```typescript
type SampleType =
  | "identity"
  | "preferences"
  | "memories"
  | "working_state"

interface SampleEnvelope<T> {
  sample_type: SampleType
  version: number
  updated_at: string       // ISO 8601
  source: "sample" | "state_worker" | "fallback_empty"
  data: T
}
```

通用校验指令：

```text
validate_sample(sample):
    要求 sample_type 与目标文件一致
    要求正式 Sample 的 version 是大于等于 1 的整数
    fallback_empty 由 Repository 内部构造，固定使用 version=0
    要求 updated_at 是合法时间
    要求 data 存在且类型正确
    不满足时返回 SampleValidationError
```

## 5.2 IdentitySample

数据输入：人工维护的核心身份、价值边界和关系定义。

针对输入执行的指令：

1. 校验必填字段。
2. 保持原始文字，不做模型润色。
3. 作为最高优先级状态拼入 system message。

数据输出：稳定的身份快照。

```typescript
interface IdentityData {
  name: string
  self_description: string
  values: string[]
  boundaries: string[]
  relationship_definition: string
}
```

## 5.3 PreferencesSample

数据输入：用户偏好和长期交互习惯。

针对输入执行的指令：

1. 按原始条目读取。
2. 不自动推断新偏好。
3. 拼接时放在 identity 之后。

数据输出：用户偏好快照。

```typescript
interface PreferencesData {
  communication_preferences: string[]
  stable_likes: string[]
  stable_dislikes: string[]
  interaction_rules: string[]
}
```

## 5.4 MemoriesSample

数据输入：首版人工准备的一组记忆条目。

针对输入执行的指令：

1. 校验每条记忆具有唯一 `id`。
2. 按 `priority` 降序、`created_at` 升序稳定排列。
3. 首版不做向量检索；读取全部样例记忆。
4. 超过配置的字符上限时截取高优先级条目，并记录警告。

数据输出：本轮可注入的记忆列表。

```typescript
interface MemoryItem {
  id: string
  content: string
  category: string
  priority: number
  created_at: string
}

interface MemoriesData {
  items: MemoryItem[]
}
```

## 5.5 WorkingStateSample

数据输入：当前关注、情绪、未完成事项和建议唤醒时间。

针对输入执行的指令：

1. 校验情绪和关注内容为可读文本。
2. 校验 `next_wake_at`；无值时由固定 heartbeat 兜底。
3. 首版只读，不根据模型输出自动修改。

数据输出：当前工作状态快照。

```typescript
interface WorkingStateData {
  current_focus: string[]
  emotion_summary: string
  pending_items: string[]
  next_wake_at: string | null
}
```

## 5.6 OpenAI Chat 输入

网关对前端接受 OpenAI Chat Completions 的最小子集。

```typescript
interface ChatCompletionRequest {
  model: string
  messages: Array<{
    role: "system" | "user" | "assistant"
    content: string
  }>
  temperature?: number
  max_completion_tokens?: number
  max_tokens?: number          // 仅接收旧客户端；与上项不可同时出现
  stream?: false             // 首版只支持 false 或不传
}
```

输入处理指令：

1. 校验 `messages` 非空。
2. 首版拒绝 `stream=true`。
3. 提取前端传入的 system message，将其内容放进服务器 system message 内的 `<frontend_instructions>` 低优先级区；不把它作为并列 system message继续传递。
4. `model` 是否透传由配置决定；默认覆盖为服务器固定模型。
5. `max_completion_tokens` 与旧字段 `max_tokens` 不得同时出现；收到旧字段时先转换为内部 `max_output_tokens`。
6. 为请求生成 `request_id`。

## 5.7 OpenAI Chat 输出

被动回合直接返回上游的 OpenAI Chat 响应结构，同时保留上游 `id`、`usage` 和 `finish_reason`。

```typescript
interface ChatCompletionResponse {
  id: string
  object: "chat.completion"
  created: number
  model: string
  choices: Array<{
    index: number
    message: {
      role: "assistant"
      content: string
    }
    finish_reason: string
  }>
  usage?: {
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
  }
}
```

## 5.8 Trigger

```typescript
type TurnTrigger = UserTrigger | TimerTrigger

interface UserTrigger {
  type: "user"
  request_id: string
  chat_request: ChatCompletionRequest
}

interface TimerTrigger {
  type: "timer"
  trigger_id: string          // 幂等键
  fired_at: string
  instruction: string        // 本次醒来的明确指令
}
```

## 5.9 OutboxMessage

```typescript
interface OutboxMessage {
  cursor: number              // SQLite 自增游标
  event_id: string            // 全局唯一
  trigger_id: string          // 防止同一次定时触发重复写入
  created_at: string
  content: string
  metadata: {
    model: string
    sample_versions: Record<SampleType, number>
    upstream_response_id: string
  }
}
```

---

## 6. 模块、输入输出、指令与接口

## 6.1 FileSampleRepository

职责：从本地 JSON 文件读取四份稳定 Sample；不计算状态、不调用模型、不修改文件。

数据输入：

- `SampleType`
- 对应 JSON 文件

针对输入执行的指令：

1. 根据固定映射选择文件，禁止由外部请求直接传入任意路径。
2. 读取完整文件。
3. 解析 JSON。
4. 执行类型和字段校验。
5. 返回不可变对象。

数据输出：`SampleEnvelope<T>`。

接口：

```typescript
interface SampleReader {
  read<T>(sampleType: SampleType): SampleEnvelope<T>
  read_all(): AllSamples
}

interface AllSamples {
  identity: SampleEnvelope<IdentityData>
  preferences: SampleEnvelope<PreferencesData>
  memories: SampleEnvelope<MemoriesData>
  working_state: SampleEnvelope<WorkingStateData>
}
```

伪代码：

```text
class FileSampleRepository implements SampleReader:
    paths = {
        identity: "samples/identity.sample.json",
        preferences: "samples/preferences.sample.json",
        memories: "samples/memories.sample.json",
        working_state: "samples/working_state.sample.json"
    }

    function read(sample_type):
        path = paths[sample_type]
        try:
            raw = read_entire_file(path)
            parsed = parse_json(raw)
            validate_sample(parsed, expected_type=sample_type)
            return deep_freeze(parsed)
        catch file_missing as error:
            throw SampleReadError(sample_type, reason="missing", cause=error)
        catch file_read_failed as error:
            throw SampleReadError(sample_type, reason="io_error", cause=error)
        catch json_parse_failed as error:
            throw SampleReadError(sample_type, reason="invalid_json", cause=error)
        catch SampleValidationError as error:
            throw SampleReadError(sample_type, reason="invalid_structure", cause=error)

    function read_all():
        return {
            identity: read("identity"),
            preferences: read_or_empty(
                "preferences",
                empty={ communication_preferences: [], stable_likes: [],
                        stable_dislikes: [], interaction_rules: [] }
            ),
            memories: read_or_empty(
                "memories",
                empty={ items: [] }
            ),
            working_state: read_or_empty(
                "working_state",
                empty={ current_focus: [], emotion_summary: "",
                        pending_items: [], next_wake_at: null }
            )
        }

    function read_or_empty(sample_type, empty):
        try:
            return read(sample_type)
        catch SampleReadError as error:
            log_warning("optional_sample_degraded", sample_type, error.reason)
            return {
                sample_type: sample_type,
                version: 0,
                updated_at: now_iso(),
                source: "fallback_empty",
                data: empty
            }
```

失败规则：

- `read()` 将文件缺失、读取失败、JSON 解析失败和结构校验失败统一包装为 `SampleReadError`。
- `identity` 发生 `SampleReadError`：拒绝本轮请求。
- `preferences` 缺失或内容损坏：使用空列表，记录警告。
- `memories` 缺失或内容损坏：使用空列表，记录警告。
- `working_state` 缺失或内容损坏：使用空状态，记录警告。
- 降级生成的空 Envelope 使用 `version=0`、`source=fallback_empty`，不得与正式 Sample 混淆。

## 6.2 SamplePublisher（预留接口）

职责：未来供长驻状态计算区原子发布新 Sample。首版不提供实现、不提供 HTTP 入口，也不由任何首版模块调用。

数据输入：完整的新 Sample。

预留合同：未来实现必须校验版本并以原子方式发布；任何失败都必须保留上一份有效 Sample。具体写入、刷新和替换算法进入第二阶段设计。

数据输出：发布后的版本号。

预留接口：

```typescript
interface SamplePublisher {
  publish<T>(sample: SampleEnvelope<T>): number
}
```

## 6.3 ContextBuilder

职责：把四份 Sample 和触发输入转换为 OpenAI Chat `messages`。不读取文件、不访问网络、不写状态。

数据输入：

- `AllSamples`
- `TurnTrigger`
- 固定基础系统提示词

针对输入执行的指令：

1. 固定顺序：基础提示词 → identity → preferences → memories → working_state → 前端附加指令区。
2. 对 XML 特殊字符转义，避免 Sample 内容破坏结构边界。
3. memories 按优先级排序并执行字符预算。
4. 合并为一个服务器 system message。
5. 被动回合：提取前端 system 内容并放入服务器 system message 的 `<frontend_instructions priority="supplemental">` 区；只把其余 user/assistant 历史追加到后面。
6. 主动回合：追加一条内部 user message，明确本次定时唤醒原因。
7. 返回每类 Sample 的版本号，供日志和 Outbox 追踪。

数据输出：`PreparedTurn`。

```typescript
interface PreparedTurn {
  messages: ChatCompletionRequest["messages"]
  sample_versions: Record<SampleType, number>
}
```

接口与伪代码：

```typescript
interface ContextBuilder {
  build(samples: AllSamples, trigger: TurnTrigger): PreparedTurn
}
```

```text
function build(samples, trigger):
    memory_items = stable_sort(
        samples.memories.data.items,
        by = priority descending, created_at ascending
    )
    memory_items = fit_within_budget(memory_items, MEMORY_CHAR_BUDGET)

    state_block = render_xml({
        identity: samples.identity.data,
        user_preferences: samples.preferences.data,
        memories: memory_items,
        working_state: samples.working_state.data
    })

    frontend_instructions = ""
    conversation_messages = []

    if trigger.type == "user":
        frontend_instructions = join_contents(
            filter(trigger.chat_request.messages, role == "system")
        )
        conversation_messages = filter(
            trigger.chat_request.messages,
            role != "system"
        )

    supplemental_block = render_xml({
        frontend_instructions: frontend_instructions
    }, attributes={ priority: "supplemental" })

    server_system_message = {
        role: "system",
        content: BASE_SYSTEM_PROMPT + "\n\n" + state_block
                 + "\n\n" + supplemental_block
    }

    if trigger.type == "user":
        messages = [server_system_message] + conversation_messages

    if trigger.type == "timer":
        messages = [
            server_system_message,
            {
                role: "user",
                content: render_timer_instruction(trigger)
            }
        ]

    return {
        messages: messages,
        sample_versions: collect_versions(samples)
    }
```

定时指令模板：

```text
这是一次服务器内部的定时唤醒，不是用户刚刚发送的新消息。
唤醒时间：{fired_at}
本次任务：{instruction}

请根据已注入的身份、偏好、记忆和工作状态进行一次有界思考。
如果确实有值得告诉用户的内容，直接输出消息正文；
如果没有，输出严格标记：<NO_MESSAGE>。
不要虚构用户刚刚说过话。
```

## 6.4 OpenAIUpstreamClient

职责：将准备好的请求发送到一个固定的 OpenAI Chat 兼容上游。

数据输入：

- `PreparedTurn.messages`
- 固定模型配置
- 可选的 `temperature`、内部统一字段 `max_output_tokens`

针对输入执行的指令：

1. 构造 `POST {UPSTREAM_BASE_URL}/v1/chat/completions`。
2. 使用服务器端 API Key，绝不透传前端凭据给上游。
3. 固定 `stream=false`。
4. 设置请求超时。
5. 校验响应至少包含一个 assistant 文本结果。
6. 根据固定上游模型配置，把 `max_output_tokens` 映射为该上游支持的 `max_completion_tokens` 或旧字段 `max_tokens`。
7. 返回标准化但仍兼容 OpenAI Chat 的响应。

数据输出：`ChatCompletionResponse`。

接口与伪代码：

```typescript
interface ModelClient {
  complete(input: ModelCompletionInput): ChatCompletionResponse
}

interface ModelCompletionInput {
  messages: ChatCompletionRequest["messages"]
  temperature?: number
  max_output_tokens?: number
}
```

```text
function complete(input):
    request_body = {
        model: CONFIG.UPSTREAM_MODEL,
        messages: input.messages,
        temperature: input.temperature ?? CONFIG.DEFAULT_TEMPERATURE,
        stream: false
    }

    token_field = CONFIG.UPSTREAM_TOKEN_LIMIT_FIELD
    require token_field in ["max_completion_tokens", "max_tokens"]
    request_body[token_field] = input.max_output_tokens
                                ?? CONFIG.DEFAULT_MAX_OUTPUT_TOKENS

    response = http_post(
        url = CONFIG.UPSTREAM_BASE_URL + "/v1/chat/completions",
        headers = { Authorization: "Bearer " + CONFIG.UPSTREAM_API_KEY },
        json = request_body,
        timeout = CONFIG.UPSTREAM_TIMEOUT_SECONDS
    )

    require response.status is 2xx
    parsed = parse_json(response.body)
    validate_chat_completion_response(parsed)
    return parsed
```

## 6.5 TurnRunner

职责：唯一回合编排器。它只安排顺序，不知道 Sample 存在文件中，也不知道 Outbox 存在 SQLite 中。

数据输入：`TurnTrigger`。

针对输入执行的指令：

1. 读取四份 Sample。
2. 构造上下文。
3. 调用上游模型。
4. 被动回合返回模型响应。
5. 主动回合把有效消息写入 Outbox。
6. 记录 request/trigger、Sample 版本、延迟和结果。

数据输出：

- 被动回合：`ChatCompletionResponse`
- 主动回合：`ActiveTurnResult`

```typescript
interface ActiveTurnResult {
  trigger_id: string
  outcome: "message_enqueued" | "no_message" | "failed"
  event_id?: string
}
```

伪代码：

```text
class TurnRunner:
    constructor(sample_reader, context_builder, model_client, outbox_store)

    function run(trigger):
        started_at = now()
        samples = sample_reader.read_all()
        prepared = context_builder.build(samples, trigger)
        response = model_client.complete({
            messages: prepared.messages,
            temperature: choose_temperature(trigger),
            max_output_tokens: choose_max_output_tokens(trigger)
        })

        if trigger.type == "user":
            log_turn(trigger, prepared.sample_versions, response, started_at)
            return response

        content = first_assistant_text(response).trim()

        if content == "<NO_MESSAGE>":
            log_active_turn(trigger, outcome="no_message")
            return {
                trigger_id: trigger.trigger_id,
                outcome: "no_message"
            }

        message = {
            event_id: new_uuid(),
            trigger_id: trigger.trigger_id,
            created_at: now_iso(),
            content: content,
            metadata: {
                model: response.model,
                sample_versions: prepared.sample_versions,
                upstream_response_id: response.id
            }
        }

        saved = outbox_store.enqueue_once(message)
        log_active_turn(trigger, outcome="message_enqueued", event_id=saved.event_id)

        return {
            trigger_id: trigger.trigger_id,
            outcome: "message_enqueued",
            event_id: saved.event_id
        }
```

## 6.6 Chat API Controller

职责：提供 OpenAI Chat 兼容 HTTP 边界。只做鉴权、协议校验、调用 TurnRunner 和错误映射。

数据输入：HTTP `POST /v1/chat/completions`。

针对输入执行的指令：

1. 校验网关 API Key。
2. 解析最小 OpenAI Chat 请求。
3. 拒绝 `stream=true` 和首版不支持的消息类型。
4. 生成 `UserTrigger`。
5. 调用 `TurnRunner.run()`。
6. 返回兼容响应。

数据输出：HTTP OpenAI Chat Response。

伪代码：

```text
POST /v1/chat/completions:
    authenticate_gateway_request(headers)
    request = parse_and_validate_chat_request(body)

    if request.stream == true:
        return error(400, "stream=true is not supported in v1")

    trigger = {
        type: "user",
        request_id: new_request_id(),
        chat_request: request
    }

    try:
        response = turn_runner.run(trigger)
        return http_json(200, response)
    catch SampleReadError as error:
        return openai_error(503, "state_unavailable", error.public_message)
    catch UpstreamTimeout as error:
        return openai_error(504, "upstream_timeout", "Model request timed out")
    catch UpstreamError as error:
        return openai_error(502, "upstream_error", "Model provider failed")
```

## 6.7 SQLiteOutboxStore

职责：持久保存主动消息，并提供游标查询。首版单机 SQLite 足够。

数据输入：

- 新的 `OutboxMessage`
- 查询游标和数量上限

针对输入执行的指令：

1. 使用 `trigger_id` 唯一约束防止同一定时触发重复写入。
2. 消息写入与事务提交成功后才返回。
3. 查询按 `cursor` 升序。
4. 首版查询不自动删除消息。
5. 首版保证同一个 `trigger_id` 在 Outbox 中至多一条消息；进程崩溃后的模型调用本身不承诺严格只执行一次。

数据输出：已保存消息或消息列表。

接口：

```typescript
interface OutboxStore {
  enqueue_once(message: NewOutboxMessage): OutboxMessage
  list_after(cursor: number, limit: number): OutboxPage
}

interface OutboxPage {
  items: OutboxMessage[]
  next_cursor: number
}
```

SQLite 表：

```sql
CREATE TABLE outbox_messages (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    trigger_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);
```

伪代码：

```text
function enqueue_once(message):
    begin transaction
    insert message
        on conflict(trigger_id) do nothing
    saved = select where trigger_id = message.trigger_id
    commit
    return saved

function list_after(after_cursor, limit):
    safe_limit = clamp(limit, 1, 100)
    items = select *
            where cursor > :after_cursor
            order by cursor ascending
            limit :safe_limit

    next_cursor = items.last.cursor if items not empty else after_cursor
    return { items, next_cursor }
```

## 6.8 Outbox API Controller

### 查询接口

```http
GET /v1/outbox?after=120&limit=20
Authorization: Bearer <gateway-api-key>
```

数据输入：`after` 游标和 `limit`。

针对输入执行的指令：

1. 鉴权。
2. 将 `after` 默认设为 `0`。
3. 将 `limit` 限制在 `1..100`。
4. 查询 `cursor > after` 的消息。
5. 不删除、不自动 ACK。

数据输出：

```json
{
  "items": [
    {
      "cursor": 121,
      "event_id": "evt_xxx",
      "created_at": "2026-07-12T09:00:00+08:00",
      "content": "沉产生的主动消息",
      "metadata": {}
    }
  ],
  "next_cursor": 121
}
```

伪代码：

```text
GET /v1/outbox:
    authenticate_gateway_request(headers)
    after = parse_non_negative_integer(query.after, default=0)
    limit = clamp(parse_integer(query.limit, default=20), 1, 100)
    page = outbox_store.list_after(after, limit)
    return http_json(200, page)
```

## 6.9 LocalScheduler

职责：在本地按固定间隔产生主动回合触发。首版使用单进程定时器。

数据输入：

- `ACTIVE_TURN_ENABLED`
- `ACTIVE_TURN_INTERVAL_MINUTES`
- 固定主动回合指令

针对输入执行的指令：

1. 服务启动后等待一个完整间隔，不立即调用模型。
2. 每个时间槽生成稳定 `trigger_id`。
3. 同一时刻只允许一个主动回合运行。
4. 上一次未结束时跳过本次，不堆积。
5. 失败后记录日志，等待下一正常周期，不立即循环重试。

数据输出：`TimerTrigger` 和执行日志。

伪代码：

```text
function scheduler_loop():
    while service_is_running:
        sleep_until_next_slot(CONFIG.ACTIVE_TURN_INTERVAL_MINUTES)

        slot = current_time_slot(CONFIG.ACTIVE_TURN_INTERVAL_MINUTES)
        trigger_id = "timer:" + slot.start_time_iso

        if active_turn_lock.is_locked():
            log("skip_timer_slot", trigger_id, reason="previous_turn_running")
            continue

        with active_turn_lock:
            trigger = {
                type: "timer",
                trigger_id: trigger_id,
                fired_at: now_iso(),
                instruction: CONFIG.ACTIVE_TURN_INSTRUCTION
            }

            try:
                turn_runner.run(trigger)
            catch error:
                log("active_turn_failed", trigger_id, error)
                // 不立即重试，等待下一个周期
```

---

## 7. 端到端数据流

## 7.1 被动对话流程

```text
1. 前端发送 OpenAI Chat 请求
2. Chat API Controller 鉴权并校验协议
3. Controller 创建 UserTrigger
4. TurnRunner 调用 SampleReader.read_all()
5. FileSampleRepository 读取并校验四份 JSON
6. ContextBuilder 生成服务器 system message
7. ContextBuilder 在其后追加前端 messages
8. OpenAIUpstreamClient 调用固定上游模型
9. TurnRunner 获得 OpenAI Chat 响应
10. Controller 原格式返回前端
```

伪代码总览：

```text
http_request
    → validate_openai_chat_request
    → UserTrigger
    → read_all_samples
    → build_injected_messages
    → upstream_chat_completion
    → validate_upstream_response
    → http_response
```

输入示例：

```json
{
  "model": "ignored-by-default",
  "messages": [
    {"role": "user", "content": "早上好，沉。"}
  ],
  "stream": false
}
```

送往上游的核心变化：

```json
{
  "model": "server-fixed-model",
  "messages": [
    {
      "role": "system",
      "content": "基础提示词 + 四类 Sample 组成的 <chen_state>"
    },
    {"role": "user", "content": "早上好，沉。"}
  ],
  "stream": false
}
```

## 7.2 主动回合流程

```text
1. LocalScheduler 到达时间槽
2. Scheduler 生成带稳定 trigger_id 的 TimerTrigger
3. TurnRunner 读取四份 Sample
4. ContextBuilder 生成 system message + 定时唤醒 user message
5. OpenAIUpstreamClient 调用固定上游模型
6. 输出为 <NO_MESSAGE>：只记录结果
7. 输出为普通文本：OutboxStore.enqueue_once()
8. 前端稍后通过 GET /v1/outbox 拉取
```

```text
TimerTrigger
    → read_all_samples
    → build_timer_context
    → upstream_chat_completion
    → [<NO_MESSAGE>] → finish
    → [message] → enqueue_once → Outbox API
```

## 7.3 未来状态演化流程（本版不实现）

```text
对话结果/时间/外部信号
        ↓
长驻状态计算区
        ↓
计算记忆、情绪等变化
        ↓
生成完整新 Sample
        ↓
SamplePublisher 原子发布
        ↓
下一回合由 ContextBuilder 读取
```

这个未来回路不得绕过 `SamplePublisher` 直接修改 Sample 文件。

---

## 8. 错误与降级

首版只实现能直接保护闭环的错误处理。

| 故障 | 本轮动作 | 是否影响其他回路 |
|---|---|---|
| identity Sample 缺失/无效 | 拒绝模型调用，返回 503 | 是；身份注入不可盲降级 |
| preferences 缺失 | 使用空偏好并告警 | 否 |
| memories 缺失 | 使用空记忆并告警 | 否 |
| working_state 缺失 | 使用空工作状态并告警 | 否 |
| 上游超时 | 被动回合返回 504；主动回合记录失败 | 不立即重试 |
| 上游 4xx/5xx | 返回 502 或记录主动回合失败 | 不修改 Sample |
| 上游响应格式错误 | 返回 502 | 不写 Outbox |
| SQLite 写失败 | 主动回合标记失败并告警 | 不声称消息已保存 |
| 定时任务重入 | 跳过新时间槽 | 不排队放大负载 |
| 同一 trigger 重复执行 | `trigger_id` 唯一约束返回已有消息 | 不产生重复消息 |

控制论上的稳定性边界：

- 不做立即无限重试。
- 不因单个 Sample 可选部分损坏而停止所有请求。
- 不在模型失败时写入假消息。
- 不允许主动回合互相唤醒。
- 不允许主动回合堆积成无限队列。

---

## 9. 配置接口

本地与 VPS 使用同一套代码，只替换环境配置。

```text
GATEWAY_HOST=127.0.0.1
GATEWAY_PORT=8000
GATEWAY_API_KEY=...

UPSTREAM_BASE_URL=https://example.com
UPSTREAM_API_KEY=...
UPSTREAM_MODEL=...
UPSTREAM_TIMEOUT_SECONDS=30
UPSTREAM_TOKEN_LIMIT_FIELD=max_completion_tokens

SAMPLE_DIRECTORY=./samples
MEMORY_CHAR_BUDGET=12000

OUTBOX_DATABASE_PATH=./data/outbox.sqlite3

ACTIVE_TURN_ENABLED=true
ACTIVE_TURN_INTERVAL_MINUTES=60
ACTIVE_TURN_INSTRUCTION=检查当前状态，判断是否有值得主动告诉用户的内容。

DEFAULT_TEMPERATURE=0.7
DEFAULT_MAX_OUTPUT_TOKENS=1200
```

配置约束：

- API Key 不写入 Sample 或日志。
- 本地默认只监听 `127.0.0.1`。
- 启动时校验固定上游模型与 `UPSTREAM_TOKEN_LIMIT_FIELD` 的组合；配置不明确时拒绝启动，而不是运行中猜测字段。
- 部署 VPS 时再通过反向代理提供 TLS。
- 首版固定单进程运行，避免 Scheduler 多实例重复触发。

---

## 10. 本地启动与 VPS 部署边界

## 10.1 本地阶段

```text
测试端 ──HTTP──→ localhost 网关 ──HTTPS──→ 上游模型
                         │
                         ├── samples/*.json
                         └── data/outbox.sqlite3
```

本地阶段验证业务闭环，不引入：

- Docker 必需依赖；
- Nginx；
- HTTPS 证书；
- systemd；
- 外网鉴权攻击面；
- 多实例调度。

## 10.2 VPS 阶段

代码和内部接口不变，只增加基础设施外壳：

```text
Operit
   ↓ HTTPS
反向代理
   ↓ localhost HTTP
网关单进程
   ├── Sample 持久目录
   └── SQLite 持久目录
```

部署前必须补充：

1. TLS。
2. 防火墙。
3. 服务守护与自动重启。
4. Sample 和 SQLite 备份。
5. 日志轮转。

---

## 11. 可观测数据

首版每个回合记录一条结构化日志：

```typescript
interface TurnLog {
  turn_id: string
  trigger_type: "user" | "timer"
  correlation_id: string       // 用户回合=request_id；主动回合=trigger_id
  started_at: string
  duration_ms: number
  sample_versions: Record<SampleType, number>
  upstream_model: string
  upstream_response_id?: string
  outcome: "success" | "no_message" | "failed"
  error_code?: string
}
```

禁止记录：

- 上游 API Key；
- 网关 API Key；
- 完整身份、记忆和用户消息正文；
- 未脱敏的上游请求体。

首版需要观察的真正被控量：

1. 四份 Sample 是否读取成功。
2. 实际转发请求是否包含状态注入。
3. 被动回合是否成功返回。
4. 主动回合是否按计划触发。
5. Outbox 是否只写入一次。

---

## 12. 验收基线

## 12.1 Sample 读取与拼接

1. 四份合法 Sample 均能读取。
2. 拼接顺序固定且可测试。
3. Sample 中的特殊字符不会破坏 `<chen_state>` 边界。
4. identity 无效时不会调用上游模型。
5. 三类非核心 Sample 缺失时使用明确空值。
6. 三类非核心 Sample 内容损坏时同样降级为空值，并记录 `optional_sample_degraded`。

## 12.2 被动回合

1. 使用标准 OpenAI Chat 请求可以调用本地网关。
2. 上游实际收到服务器 system message。
3. 用户历史顺序不被打乱。
4. 返回体可被 OpenAI Chat 客户端解析。
5. `stream=true` 得到清晰的首版不支持错误。
6. 客户端 system 内容只出现在 `<frontend_instructions>` 中，上游请求中不存在第二条并列 system message。
7. 同时传入 `max_completion_tokens` 与 `max_tokens` 时返回 400；单独传旧字段时能够正确转换。

## 12.3 主动回合

1. 定时器能产生一次 TimerTrigger。
2. 主动回合使用与被动回合相同的四份 Sample。
3. `<NO_MESSAGE>` 不写 Outbox。
4. 普通文本写入 Outbox。
5. 同一 `trigger_id` 即使重新调用模型，Outbox 中也至多产生一条消息。
6. `enqueue_once` 的并发冲突路径能够返回已经保存的消息。
7. 主动回合上游超时或响应畸形时，Outbox 行数保持不变，日志 outcome 为 `failed`。
8. SQLite 提交失败时不得返回 `message_enqueued`。
6. 服务重启后消息仍可查询。

## 12.4 Outbox API

1. `after=0` 可以读取第一批消息。
2. 使用 `next_cursor` 可以继续读取且不漏消息。
3. 重复使用旧 cursor 允许重新读到消息。
4. 查询不会删除消息。
5. 超大 `limit` 会被限制为 100。
6. 空页保持调用方传入的 cursor。
7. 使用真实 SQLite 连续翻页时不重复、不漏行。

## 12.5 本地到 VPS 可迁移性

1. 业务代码不包含硬编码本地绝对路径。
2. Sample 目录、SQLite 路径和上游配置均来自环境变量。
3. 本地与 VPS 不改变应用层接口。

---

## 13. 建议实现顺序

```text
M1  四份 Sample + FileSampleRepository
    验收：正常读取、错误降级、固定拼接

M2  ContextBuilder + OpenAIUpstreamClient
    验收：能看到实际注入后的上游请求并获得回复

M3  POST /v1/chat/completions
    验收：标准 OpenAI Chat 客户端可调用

M4  SQLiteOutboxStore + GET /v1/outbox
    验收：写入、重启、游标读取均正常

M5  LocalScheduler + 主动回合
    验收：定时产生消息且不重复

M6  部署 VPS
    验收：通用 HTTP 测试客户端可通过 HTTPS 调用网关和读取 Outbox
```

---

## 14. 第二阶段探索清单

以下内容记录下来，但不自动进入第一版：

1. 长驻状态计算区的记忆变化函数。
2. 情绪状态演化与噪声过滤。
3. 模型输出到状态修改提案的结构化协议。
4. SamplePublisher 的正式启用。
5. 相关记忆检索和上下文预算优化。
6. Operit 插件与 Outbox HTTP 长轮询。
7. 主动消息 ACK、设备寻址和推送。
8. 工具调用与状态回写。
9. 多模型协议适配。
10. 数据库从 SQLite 向其他存储迁移。

---

## 15. 第一版架构不变量

1. VPS 是“沉”的主体，前端只是输入和观察端。
2. 每次模型调用前都必须确定性读取并注入四类 Sample。
3. identity 缺失时不得盲目调用模型。
4. Context Builder 只读 Sample。
5. Sample 只有一个发布入口。
6. 被动回合和主动回合共用 TurnRunner。
7. 主动回合没有消息时不得制造空消息。
8. Outbox 写入必须幂等并可在重启后恢复。
9. 第一版只支持 OpenAI Chat、非流式、单模型、单进程。
10. 新能力默认进入探索清单，不改变第一版闭环。
