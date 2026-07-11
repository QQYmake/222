# M3: POST /v1/chat/completions + TurnRunner 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 提供 OpenAI Chat 兼容 HTTP 边界，完整打通 被动回合 数据流：HTTP 请求 → 鉴权 → TurnRunner → Sample 读取 → ContextBuilder → 上游模型 → HTTP 响应。

**Architecture:** FastAPI 提供 HTTP 边界；TurnRunner 是应用层编排器；Chat API Controller 是适配器。

**Tech Stack:** Python 3.12+, uv, pytest, FastAPI, httpx

## Global Constraints

- 只做鉴权、协议校验、调用 TurnRunner、错误映射——Controller 不含业务逻辑
- `stream=true` 返回 400
- `max_completion_tokens` 与 `max_tokens` 同时出现返回 400
- 客户端 system 内容只出现在 `<frontend_instructions>` 中，不作为并列 system message
- model 字段默认覆盖为服务器固定模型
- API Key 不写入日志
- TurnRunner 不知道 Sample 存在文件中，不知道 Outbox 存在 SQLite 中
- 主动回合分支在 M3 中预留但不接入（M4/M5 实现）

---

## M3 数据流总览

```text
HTTP POST /v1/chat/completions
        │
        ▼
Chat API Controller
        │
        ├─ authenticate_gateway_request(headers)  ← Bearer {GATEWAY_API_KEY}
        ├─ parse_chat_request(body)
        ├─ reject if stream=true
        ├─ reject if max_completion_tokens && max_tokens
        ├─ construct UserTrigger { request_id, chat_request }
        │
        ▼
TurnRunner.run(trigger)
        │
        ├─ sample_reader.read_all()  → AllSamples
        ├─ context_builder.build(samples, trigger)  → PreparedTurn
        ├─ model_client.complete({ messages, temperature, max_output_tokens })  → Response
        ├─ log_turn(trigger, sample_versions, response, started_at)
        │
        ▼
ChatCompletionResponse → HTTP 200 JSON

错误映射:
  SampleReadError     → 503 { error: { type: "state_unavailable", message } }
  UpstreamTimeout     → 504 { error: { type: "upstream_timeout", message } }
  UpstreamError       → 502 { error: { type: "upstream_error", message } }
  stream=true         → 400 { error: { type: "unsupported_stream", message } }
  双 token 字段       → 400 { error: { type: "conflicting_token_fields", message } }
  鉴权失败            → 401 { error: { type: "invalid_api_key", message } }
```

## M3 模块依赖图

```text
config.py ─────────────────────────────────────────┐
                                                    ▼
sample_reader.py ───┐                    ports/
                    │               ┌──────────────┐
context_builder.py──┤               │ model_client  │
                    │               └──────┬───────┘
                    ▼                      │
           ┌───────────────┐               │
           │ TurnRunner    │───────────────┘
           │ (应用层)       │
           └───────┬───────┘
                   │
                   ▼
           ┌───────────────────┐
           │ Chat API Controller│ ← FastAPI router
           │ (适配器)            │
           └───────────────────┘
```

---

## Task 1: TurnRunner 实现

**Files:**
- Create: `app/application/turn_runner.py`
- Create: `tests/unit/test_turn_runner.py`

### 伪代码

```text
class TurnRunner:
    def __init__(self, sample_reader, context_builder, model_client, outbox_store=None):
        """
        数据输入: 四个依赖注入
          - sample_reader: SampleReader
          - context_builder: ContextBuilder
          - model_client: ModelClient
          - outbox_store: OutboxStore | None (M3 传 None, M4 接入)
        数据输出: 无 (实例方法)
        指令:
          1. TurnRunner 只编排顺序，不包含 IO 细节
          2. 不知道 Sample 存在文件中
          3. 不知道 Outbox 存在 SQLite 中
        """

    function run(trigger: TurnTrigger) -> ChatCompletionResponse | ActiveTurnResult:
        """
        数据输入: TurnTrigger (UserTrigger | TimerTrigger)
        数据输出:
          - UserTrigger → ChatCompletionResponse
          - TimerTrigger → ActiveTurnResult (M3 中仅预留, M4/M5 实现)
        指令:
          1. 记录 started_at
          2. samples = sample_reader.read_all()
          3. prepared = context_builder.build(samples, trigger)
          4. response = model_client.complete({
               messages: prepared.messages,
               temperature: choose_temperature(trigger),
               max_output_tokens: choose_max_output_tokens(trigger)
             })
          5. if trigger.type == "user":
               log_turn(trigger, prepared.sample_versions, response, started_at)
               return response
          6. if trigger.type == "timer":
               (M3 暂不实现完整主动回合, 仅记录 NotImplemented)
        """

    function choose_temperature(trigger: TurnTrigger) -> float | None:
        """
        数据输入: TurnTrigger
        数据输出: temperature 值
        指令:
          1. 被动回合: 如果前端传了 temperature 则使用, 否则 None (上游适配器用默认)
          2. 主动回合: None
        """

    function choose_max_output_tokens(trigger: TurnTrigger) -> int | None:
        """
        数据输入: TurnTrigger
        数据输出: max_output_tokens 值
        指令:
          1. 被动回合: 如果前端传了则使用 (已转换为内部字段), 否则 None
          2. 主动回合: None
        """
```

- [ ] Step 1: 写 test_turn_runner.py — mock 四个依赖, 验证调用顺序、日志记录、错误传播
- [ ] Step 2: 实现 turn_runner.py
- [ ] Step 3: 运行测试通过

---

## Task 2: OpenAI Chat 请求解析与校验

**Files:**
- Edit: `app/domain/models/chat_completion.py` (M2 已创建, 补充 parse + validate 逻辑)
- Create: `tests/unit/test_chat_request_parsing.py`

### 伪代码

```text
function parse_chat_request(raw: dict) -> ChatCompletionRequest:
    """
    数据输入: HTTP 请求体 dict
    数据输出: ChatCompletionRequest
    指令:
      1. 校验 messages 存在且非空
      2. 校验每条 message: role in (system/user/assistant), content 非空字符串
      3. stream 只允许 false 或不传; true → raise UnsupportedStreamError
      4. max_completion_tokens 和 max_tokens 不可同时出现 → raise ConflictingTokenFieldsError
      5. 提取 max_output_tokens (优先 max_completion_tokens, 回退 max_tokens)
      6. model 字段允许任意字符串 (不校验具体值, 由 TurnRunner 覆盖)
    """

function to_internal_max_output_tokens(request: ChatCompletionRequest) -> int | None:
    """
    数据输入: ChatCompletionRequest
    数据输出: 统一的 max_output_tokens
    指令:
      1. max_completion_tokens 优先
      2. 回退到 max_tokens
      3. 都没有则 None
    """
```

- [ ] Step 1: 写 test_chat_request_parsing.py — 合法请求、空 messages、非法 role、stream=true、双字段冲突、旧字段转换
- [ ] Step 2: 实现解析逻辑
- [ ] Step 3: 运行测试通过

---

## Task 3: Chat API Controller (FastAPI Router)

**Files:**
- Create: `app/adapters/http/chat_controller.py`
- Create: `app/adapters/http/errors.py` (OpenAI 兼容错误格式)
- Create: `app/adapters/http/auth.py`
- Create: `tests/unit/test_chat_controller.py`

### 伪代码

```text
# auth.py

function authenticate_gateway_request(headers: dict, api_key: str) -> None:
    """
    数据输入: HTTP headers, 服务器配置的 GATEWAY_API_KEY
    数据输出: None (成功) 或 raise AuthenticationError
    指令:
      1. 提取 Authorization: Bearer {token}
      2. 与 GATEWAY_API_KEY 比较
      3. 不匹配 → AuthenticationError
      4. API Key 为空配置时也拒绝 (防止误开放)
    """

# errors.py

function openai_error(status: int, error_type: str, message: str) -> JSONResponse:
    """
    数据输入: HTTP 状态码, 错误类型, 消息
    数据输出: OpenAI 兼容错误 JSON
    指令:
      格式: { "error": { "message": "...", "type": "...", "code": ... } }
    """
```

```text
# chat_controller.py

POST /v1/chat/completions:
    authenticate_gateway_request(headers, config.gateway_api_key)
    request = parse_chat_request(body)

    if request.stream == true:
        return openai_error(400, "unsupported_stream",
            "stream=true is not supported in v1")

    trigger = UserTrigger(
        request_id=new_request_id(),
        chat_request=request
    )

    try:
        response = turn_runner.run(trigger)
        return http_json(200, response.to_dict())
    catch SampleReadError as error:
        return openai_error(503, "state_unavailable", error.public_message)
    catch UpstreamTimeout:
        return openai_error(504, "upstream_timeout", "Model request timed out")
    catch UpstreamError:
        return openai_error(502, "upstream_error", "Model provider failed")
```

- [ ] Step 1: 写 test_chat_controller.py — 鉴权成功/失败、stream 拒绝、双字段冲突、正常调用、错误映射
- [ ] Step 2: 实现 auth.py + errors.py + chat_controller.py
- [ ] Step 3: 运行测试通过

---

## Task 4: FastAPI 应用入口与路由注册

**Files:**
- Create: `app/application/app.py`
- Create: `app/application/dependencies.py`
- Create: `tests/integration/test_app_endpoints.py`

### 伪代码

```text
# dependencies.py — 依赖注入容器

def create_app(config: Config) -> FastAPI:
    """
    数据输入: Config
    数据输出: FastAPI 实例
    指令:
      1. 构造 FileSampleRepository(config.sample_directory)
      2. 构造 ContextBuilder(BASE_SYSTEM_PROMPT, config.memory_char_budget)
      3. 构造 OpenAIUpstreamClient(config)
      4. 构造 TurnRunner(sample_reader, context_builder, model_client)
      5. 注册 Chat API Controller
      6. 注册异常处理器
    """

# app.py

app = create_app(Config.load_from_env())

# 路由:
POST   /v1/chat/completions  → chat_controller.handle
GET    /health               → { status: "ok" }
```

- [ ] Step 1: 写 test_app_endpoints.py — 用 TestClient 测试 /health、/v1/chat/completions 完整流程
- [ ] Step 2: 实现 dependencies.py + app.py
- [ ] Step 3: 运行测试通过

---

## Task 5: M3 集成验证

**Files:**
- Create: `tests/integration/test_passive_turn_e2e.py`

### 伪代码

```text
test_standard_openai_chat_request:
    1. 启动 FastAPI TestClient
    2. POST /v1/chat/completions with valid OpenAI Chat body
    3. Mock ModelClient 返回标准响应
    4. 断言 200, 响应可被 OpenAI Chat 客户端解析
    5. 断言上游收到的 messages[0].role == "system"
    6. 断言上游收到的 system message 包含 <chen_state>
    7. 断言用户历史顺序不打乱

test_stream_true_rejected:
    POST with stream=true → 400, error.type == "unsupported_stream"

test_dual_token_fields_rejected:
    POST with both max_completion_tokens and max_tokens → 400

test_auth_failure:
    POST without/with wrong API key → 401

test_identity_failure_returns_503:
    Mock SampleReader 抛 SampleReadError → 503

test_upstream_timeout_returns_504:
    Mock ModelClient 抛 UpstreamTimeout → 504

test_upstream_error_returns_502:
    Mock ModelClient 抛 UpstreamError → 502

test_frontend_system_only_in_supplemental:
    POST with system message → 上游请求中只有 1 条 system message (服务器拼接的)
    断言前端 system 内容出现在 <frontend_instructions> 中

test_old_max_tokens_field_converted:
    POST with max_tokens=500 → 上游请求使用 max_completion_tokens (或 max_tokens 取决于配置)
```

- [ ] Step 1: 写集成测试
- [ ] Step 2: 运行全部测试 (含 M1+M2 回归)
- [ ] Step 3: 验收基线 12.2 全部通过
