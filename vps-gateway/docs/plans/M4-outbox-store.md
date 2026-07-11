# M4: SQLiteOutboxStore + GET /v1/outbox 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 持久保存主动回合消息，提供游标查询接口。打通 TurnRunner 主动回合分支，完成 enqueue_once 幂等写入和 list_after 游标查询。

**Architecture:** SQLiteOutboxStore 是适配器（SQLite IO）；OutboxStore 端口接口隔离；Outbox API Controller 提供 HTTP 查询边界。

**Tech Stack:** Python 3.12+, uv, pytest, sqlite3 (标准库)

## Global Constraints

- `trigger_id` 唯一约束：同一定时触发至多一条消息
- 消息写入与事务提交成功后才返回
- 查询按 cursor 升序，不自动删除
- `enqueue_once` 幂等：重复调用返回已有消息，不产生新行
- limit 被 clamp 到 1..100
- 空页保持调用方传入的 cursor
- SQLite 写失败时不返回 `message_enqueued`
- 不声称消息已保存除非确实写入

---

## M4 数据流总览

```text
TimerTrigger (来自 TurnRunner)
        │
        ▼
TurnRunner.run(timer_trigger)
        │
        ├─ read_all_samples → AllSamples
        ├─ context_builder.build → PreparedTurn
        ├─ model_client.complete → ChatCompletionResponse
        │
        ├─ content == "<NO_MESSAGE>" → log outcome="no_message" → finish
        │
        ├─ content (正常文本)
        │     │
        │     ▼
        │  OutboxStore.enqueue_once(NewOutboxMessage)
        │     │
        │     ├─ BEGIN TRANSACTION
        │     ├─ INSERT ... ON CONFLICT(trigger_id) DO NOTHING
        │     ├─ SELECT WHERE trigger_id = ?
        │     ├─ COMMIT
        │     │
        │     ▼
        │  OutboxMessage (含 cursor)
        │
        ▼
ActiveTurnResult { trigger_id, outcome="message_enqueued", event_id }

GET /v1/outbox?after=0&limit=20
        │
        ▼
Outbox API Controller
        │
        ├─ authenticate_gateway_request
        ├─ after = parse_non_negative_integer(query.after, default=0)
        ├─ limit = clamp(parse_integer(query.limit, 20), 1, 100)
        ├─ page = outbox_store.list_after(after, limit)
        │
        ▼
HTTP 200 { items: [...], next_cursor: N }
```

## M4 模块依赖图

```text
config.py ─────────────────────────────────┐
                                           ▼
                                    ports/
                               ┌──────────────────┐
                               │  outbox_store.py  │ (端口: OutboxStore)
                               └───────┬──────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                                      │
                    ▼                                      ▼
          ┌──────────────────┐              ┌──────────────────────┐
          │ TurnRunner       │              │ SQLiteOutboxStore     │
          │ (已有, 接入)      │              │ (适配器)              │
          │                  │              │                      │
          │ enqueue_once()   │              │ enqueue_once()       │
          └──────────────────┘              │ list_after()         │
                                            └──────────┬───────────┘
                                                       │
                                                       ▼
                                            ┌──────────────────────┐
                                            │ Outbox API Controller │
                                            │ GET /v1/outbox        │
                                            └──────────────────────┘
```

---

## Task 1: OutboxMessage 数据模型 + OutboxStore 端口接口

**Files:**
- Create: `app/domain/models/outbox.py`
- Create: `app/domain/ports/outbox_store.py`
- Create: `tests/unit/test_outbox_model.py`

### 伪代码

```text
# outbox.py

dataclass NewOutboxMessage:
    """待写入的消息 (不含 cursor)"""
    event_id: str               # 全局唯一
    trigger_id: str             # 幂等键
    created_at: str             # ISO 8601
    content: str
    metadata: dict              # { model, sample_versions, upstream_response_id }

dataclass OutboxMessage:
    """已保存的消息 (含 cursor)"""
    cursor: int                 # SQLite 自增游标
    event_id: str
    trigger_id: str
    created_at: str
    content: str
    metadata: dict

dataclass OutboxPage:
    """查询结果页"""
    items: list[OutboxMessage]
    next_cursor: int

# 数据输入: TurnRunner 构造 NewOutboxMessage
# 数据输出: SQLiteOutboxStore 返回 OutboxMessage (含 cursor)
# 指令:
#   1. NewOutboxMessage 不含 cursor (写入前未知)
#   2. OutboxMessage 含 cursor (写入后由 SQLite 分配)
#   3. metadata 是 dict, 序列化为 JSON 存储
```

```text
# outbox_store.py (端口接口)

class OutboxStore(ABC):
    @abstractmethod
    function enqueue_once(message: NewOutboxMessage) -> OutboxMessage:
        """
        数据输入: NewOutboxMessage
        数据输出: OutboxMessage (含 cursor)
        指令:
          1. INSERT ... ON CONFLICT(trigger_id) DO NOTHING
          2. SELECT WHERE trigger_id = ? → 返回已存在或刚插入的行
          3. 事务提交成功后才返回
          4. 幂等: 同一 trigger_id 重复调用返回相同行
        """

    @abstractmethod
    function list_after(after_cursor: int, limit: int) -> OutboxPage:
        """
        数据输入: after_cursor (游标), limit (数量)
        数据输出: OutboxPage { items, next_cursor }
        指令:
          1. safe_limit = clamp(limit, 1, 100)
          2. SELECT * WHERE cursor > after_cursor ORDER BY cursor ASC LIMIT safe_limit
          3. next_cursor = items.last.cursor if items else after_cursor
          4. 不删除消息
        """
```

- [ ] Step 1: 写 test_outbox_model.py — NewOutboxMessage/OutboxMessage/OutboxPage 构造与不可变性
- [ ] Step 2: 实现 outbox.py + outbox_store.py
- [ ] Step 3: 运行测试通过

---

## Task 2: SQLiteOutboxStore 适配器实现

**Files:**
- Create: `app/adapters/outbox/sqlite_outbox_store.py`
- Create: `tests/integration/test_sqlite_outbox_store.py`

### 伪代码

```text
# sqlite_outbox_store.py

class SQLiteOutboxStore(OutboxStore):
    def __init__(self, database_path: str):
        """
        数据输入: SQLite 文件路径 (来自 config.outbox_database_path)
        数据输出: 无
        指令:
          1. 连接 SQLite (check_same_thread=False)
          2. 执行 DDL 建表 (IF NOT EXISTS)
          3. 启用 WAL 模式 (并发友好)
        """

    function _ensure_table():
        """
        CREATE TABLE IF NOT EXISTS outbox_messages (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            trigger_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """

    function enqueue_once(message: NewOutboxMessage) -> OutboxMessage:
        """
        数据输入: NewOutboxMessage
        数据输出: OutboxMessage (含 cursor)
        指令:
          1. BEGIN TRANSACTION
          2. INSERT INTO outbox_messages (event_id, trigger_id, created_at, content, metadata_json)
             VALUES (?, ?, ?, ?, ?)
             ON CONFLICT(trigger_id) DO NOTHING
          3. SELECT * WHERE trigger_id = ?
          4. COMMIT
          5. 返回 OutboxMessage (含 cursor)
          6. 事务失败时 raise, 不返回假成功
        """

    function list_after(after_cursor: int, limit: int) -> OutboxPage:
        """
        数据输入: after_cursor, limit
        数据输出: OutboxPage
        指令:
          1. safe_limit = max(1, min(limit, 100))
          2. SELECT * WHERE cursor > ? ORDER BY cursor ASC LIMIT ?
          3. next_cursor = items[-1].cursor if items else after_cursor
          4. 不删除消息
        """

    function close():
        """关闭连接"""
```

- [ ] Step 1: 写 test_sqlite_outbox_store.py — 建表、enqueue_once 正常/幂等/冲突、list_after 游标/limit/空页、重启后查询
- [ ] Step 2: 实现 sqlite_outbox_store.py
- [ ] Step 3: 运行测试通过

---

## Task 3: TurnRunner 主动回合分支接入

**Files:**
- Edit: `app/application/turn_runner.py`
- Create: `tests/unit/test_turn_runner_active.py`

### 伪代码

```text
# TurnRunner.run(trigger) — 补充主动回合分支

function run(trigger: TurnTrigger):
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

    # --- 主动回合分支 (M4 新增) ---
    content = first_assistant_text(response).strip()

    if content == "<NO_MESSAGE>":
        log_active_turn(trigger, outcome="no_message")
        return ActiveTurnResult(
            trigger_id=trigger.trigger_id,
            outcome="no_message"
        )

    message = NewOutboxMessage(
        event_id=new_uuid(),
        trigger_id=trigger.trigger_id,
        created_at=now_iso(),
        content=content,
        metadata={
            "model": response.model,
            "sample_versions": prepared.sample_versions,
            "upstream_response_id": response.id
        }
    )

    saved = outbox_store.enqueue_once(message)
    log_active_turn(trigger, outcome="message_enqueued", event_id=saved.event_id)

    return ActiveTurnResult(
        trigger_id=trigger.trigger_id,
        outcome="message_enqueued",
        event_id=saved.event_id
    )

# 数据输入: TimerTrigger
# 数据输出: ActiveTurnResult
# 指令:
#   1. <NO_MESSAGE> → outcome="no_message", 不写 Outbox
#   2. 正常文本 → enqueue_once → outcome="message_enqueued"
#   3. 上游失败 → 异常传播, outcome="failed" (由调用方处理)
#   4. outbox_store 为 None 时主动回合抛 RuntimeError (防止 M3 阶段误调用)
```

- [ ] Step 1: 写 test_turn_runner_active.py — NO_MESSAGE 不写 Outbox、正常文本写入、trigger_id 幂等、outbox_store=None 时拒绝
- [ ] Step 2: 编辑 turn_runner.py 接入主动回合分支
- [ ] Step 3: 运行测试通过

---

## Task 4: Outbox API Controller

**Files:**
- Create: `app/adapters/http/outbox_controller.py`
- Create: `tests/unit/test_outbox_controller.py`

### 伪代码

```text
GET /v1/outbox:
    authenticate_gateway_request(headers, config.gateway_api_key)
    after = parse_non_negative_integer(query_params.get("after"), default=0)
    limit = clamp(parse_integer(query_params.get("limit"), default=20), 1, 100)

    page = outbox_store.list_after(after, limit)
    return http_json(200, {
        "items": [msg.to_dict() for msg in page.items],
        "next_cursor": page.next_cursor
    })

# 数据输入: HTTP GET /v1/outbox?after=N&limit=M
# 数据输出: { items: [...], next_cursor: N }
# 指令:
#   1. 鉴权 (同 Chat Controller)
#   2. after 默认 0, 非负整数
#   3. limit 默认 20, clamp 到 1..100
#   4. 查询不删除消息
#   5. 空页时 next_cursor == 传入的 after
```

- [ ] Step 1: 写 test_outbox_controller.py — 正常查询、空页、limit clamp、auth 失败、after 默认值
- [ ] Step 2: 实现 outbox_controller.py
- [ ] Step 3: 运行测试通过

---

## Task 5: Outbox 路由注册到 FastAPI 应用

**Files:**
- Edit: `app/application/dependencies.py` (注入 OutboxStore)
- Edit: `app/application/app.py` (注册 Outbox 路由)
- Create: `tests/integration/test_outbox_api_e2e.py`

### 伪代码

```text
# dependencies.py 修改

def create_app(config: Config) -> FastAPI:
    ...
    outbox_store = SQLiteOutboxStore(config.outbox_database_path)
    turn_runner = TurnRunner(
        sample_reader, context_builder, model_client, outbox_store
    )
    # 注册 Outbox Controller
    ...
```

- [ ] Step 1: 修改 dependencies.py 接入 OutboxStore
- [ ] Step 2: 注册 GET /v1/outbox 路由
- [ ] Step 3: 写 test_outbox_api_e2e.py — 完整 HTTP 流程
- [ ] Step 4: 运行测试通过

---

## Task 6: M4 集成验证

**Files:**
- Create: `tests/integration/test_active_turn_e2e.py`
- Create: `tests/integration/test_outbox_persistence.py`

### 伪代码

```text
test_active_turn_writes_outbox:
    1. 构造 TurnRunner (真实 OutboxStore + Mock ModelClient)
    2. TimerTrigger → run()
    3. Mock 返回普通文本
    4. 断言 ActiveTurnResult.outcome == "message_enqueued"
    5. 断言 OutboxStore.list_after(0, 10) 返回 1 条
    6. 断言消息 content 匹配

test_active_turn_no_message:
    1. Mock 返回 "<NO_MESSAGE>"
    2. 断言 outcome == "no_message"
    3. 断言 Outbox 行数为 0

test_trigger_id_idempotent:
    1. 同一 trigger_id 调用两次 run()
    2. 断言 Outbox 中只有 1 条消息
    3. 断言两次返回的 event_id 相同

test_outbox_persistence_after_restart:
    1. 写入 3 条消息
    2. 关闭 SQLiteOutboxStore
    3. 重新打开 (模拟重启)
    4. 断言 list_after(0, 100) 返回 3 条
    5. 断言 cursor 连续

test_outbox_pagination_no_duplicates:
    1. 写入 5 条消息
    2. list_after(0, 2) → 2 条, next_cursor=X
    3. list_after(X, 2) → 2 条, next_cursor=Y
    4. list_after(Y, 2) → 1 条
    5. 断言无重复、无遗漏

test_outbox_limit_clamped:
    list_after(0, 999) → 返回至多 100 条

test_outbox_empty_page_keeps_cursor:
    list_after(9999, 20) → items=[], next_cursor=9999
```

- [ ] Step 1: 写集成测试
- [ ] Step 2: 运行全部测试 (M1+M2+M3+M4 回归)
- [ ] Step 3: 验收基线 12.3 + 12.4 全部通过
