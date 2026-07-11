# M1: 四份 Sample + FileSampleRepository 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现从本地 JSON 文件读取四份只读 Sample，完成校验、降级和固定拼接，为后续 ContextBuilder 提供稳定数据源。

**Architecture:** 六边形架构——领域模型和端口接口位于内层，FileSampleRepository 作为适配器实现端口，配置和日志位于基础设施层。依赖方向严格从外向内。

**Tech Stack:** Python 3.12+, uv, pytest

## Global Constraints

- 首版使用本地 JSON 文件充当 Sample
- Sample 在一次读取过程中必须保持完整，不能读到半份数据
- identity 缺失/无效时拒绝本轮请求，不得降级
- preferences/memories/working_state 缺失或损坏时降级为空值，记录警告
- 降级生成的空 Envelope 使用 version=0、source=fallback_empty
- API Key 不写入 Sample 或日志
- 业务代码不包含硬编码本地绝对路径
- Sample 目录路径来自环境变量 SAMPLE_DIRECTORY

---

## M1 数据流总览

```text
samples/*.sample.json (4 个本地文件)
        │
        ▼
FileSampleRepository.read(sample_type)
        │
        ├─ read_entire_file(path)
        ├─ parse_json(raw)
        ├─ validate_sample(parsed, expected_type)
        │
        ▼
SampleEnvelope<T>  (不可变)
        │
        ▼
FileSampleRepository.read_all()
        │
        ├─ identity  → read()       → 失败抛 SampleReadError
        ├─ preferences → read_or_empty() → 失败返回 fallback_empty
        ├─ memories   → read_or_empty() → 失败返回 fallback_empty
        ├─ working_state → read_or_empty() → 失败返回 fallback_empty
        │
        ▼
AllSamples (包含 4 个 SampleEnvelope)
```

## M1 模块依赖图

```text
config.py ──────────────────────────────┐
                                        ▼
sample.py (SampleEnvelope/SampleType) ← 被以下依赖:
    ├── identity.py                     │
    ├── preferences.py                  │   全部 domain model
    ├── memories.py                     │   依赖 sample.py
    └── working_state.py                │
                                        │
sample_reader.py (端口接口)             │
    └── 依赖 sample.py + 四个 data model │
                                        │
file_sample_repository.py (适配器)       │
    ├── 实现 sample_reader.py            │
    ├── 依赖 config.py (路径)            │
    └── 依赖 sample.py (错误类型)        │
```

---

## Task 1: 项目骨架与配置

**Files:**
- Create: `vps-gateway/pyproject.toml`
- Create: `vps-gateway/.env.example`
- Create: `vps-gateway/app/__init__.py`
- Create: `vps-gateway/app/infrastructure/__init__.py`
- Create: `vps-gateway/app/infrastructure/config.py`
- Create: `vps-gateway/app/infrastructure/logging.py`

**Interfaces:**
- Produces: `Config` 对象，供 FileSampleRepository 读取路径
- Produces: `get_logger(name)` 函数，供全项目使用

### 数据输入

```text
环境变量:
  SAMPLE_DIRECTORY       → Sample 文件目录路径
  MEMORY_CHAR_BUDGET     → 记忆字符预算 (M1 读取时暂不使用，ContextBuilder 用)
  GATEWAY_HOST           → 监听地址
  GATEWAY_PORT           → 监听端口
  GATEWAY_API_KEY        → 网关鉴权密钥
  UPSTREAM_BASE_URL      → 上游模型地址 (M2 用)
  UPSTREAM_API_KEY       → 上游模型密钥 (M2 用)
  UPSTREAM_MODEL         → 上游模型名 (M2 用)
  UPSTREAM_TIMEOUT_SECONDS → 上游超时 (M2 用)
  UPSTREAM_TOKEN_LIMIT_FIELD → max_completion_tokens | max_tokens (M2 用)
  OUTBOX_DATABASE_PATH   → SQLite 路径 (M4 用)
  ACTIVE_TURN_ENABLED    → 主动回合开关 (M5 用)
  ACTIVE_TURN_INTERVAL_MINUTES → 主动回合间隔 (M5 用)
  ACTIVE_TURN_INSTRUCTION → 主动回合指令 (M5 用)
  DEFAULT_TEMPERATURE    → 默认温度 (M2 用)
  DEFAULT_MAX_OUTPUT_TOKENS → 默认最大输出 token (M2 用)
```

### 针对输入数据做的指令

```text
class Config:
    function load_from_env():
        从 os.environ 读取上述变量
        SAMPLE_DIRECTORY 有默认值 "./samples"
        启动时校验:
          - UPSTREAM_TOKEN_LIMIT_FIELD 必须是 "max_completion_tokens" 或 "max_tokens"
          - 不满足时拒绝启动 (ValueError)
        返回不可变 Config 对象
        API Key 只存在内存中，不写入日志
```

```text
function get_logger(name):
    返回标准 logging.Logger
    格式: [时间] [级别] [模块名] 消息
    级别由环境变量 LOG_LEVEL 控制，默认 INFO
```

### 数据输出

```text
Config 对象:
  sample_directory: str
  memory_char_budget: int
  gateway_host: str
  gateway_port: int
  upstream_base_url: str | None
  upstream_api_key: str | None
  upstream_model: str | None
  upstream_timeout_seconds: int
  upstream_token_limit_field: str
  outbox_database_path: str
  active_turn_enabled: bool
  active_turn_interval_minutes: int
  active_turn_instruction: str
  default_temperature: float
  default_max_output_tokens: int
```

### 验收

- [ ] `uv run python -c "from app.infrastructure.config import Config; c = Config.load_from_env(); print(c.sample_directory)"` 正常输出
- [ ] 缺少必填配置时启动报错且有明确错误信息

---

## Task 2: 领域模型 — SampleEnvelope 与错误类型

**Files:**
- Create: `vps-gateway/app/domain/__init__.py`
- Create: `vps-gateway/app/domain/models/__init__.py`
- Create: `vps-gateway/app/domain/models/sample.py`

**Interfaces:**
- Consumes: 无
- Produces: `SampleType`, `SampleEnvelope`, `SampleValidationError`, `SampleReadError`

### 数据输入

```text
无外部输入。定义类型和异常。
```

### 针对输入数据做的指令

```text
# --- 类型定义 ---

type SampleType = "identity" | "preferences" | "memories" | "working_state"

interface SampleEnvelope<T>:
    sample_type: SampleType
    version: int                 # 正式 Sample >= 1; fallback_empty 固定 0
    updated_at: string           # ISO 8601
    source: "sample" | "state_worker" | "fallback_empty"
    data: T                      # 具体类型由子模型定义

# --- 校验函数 ---

function validate_sample(sample, expected_type):
    # 指令:
    #   1. 要求 sample_type 与 expected_type 一致
    #   2. 要求正式 Sample 的 version >= 1 (整数)
    #   3. fallback_empty 的 version 固定为 0 (由 Repository 内部构造)
    #   4. 要求 updated_at 是合法 ISO 8601 时间
    #   5. 要求 data 存在且类型正确 (类型校验由各子模型负责)
    #   6. 不满足时抛出 SampleValidationError

    if sample.sample_type != expected_type:
        raise SampleValidationError(f"sample_type mismatch: expected {expected_type}")

    if sample.source != "fallback_empty":
        if not isinstance(sample.version, int) or sample.version < 1:
            raise SampleValidationError("version must be >= 1 for formal samples")

    if not is_valid_iso8601(sample.updated_at):
        raise SampleValidationError("updated_at is not valid ISO 8601")

    if sample.data is None:
        raise SampleValidationError("data is missing")

    return sample  # 通过校验

# --- 异常类型 ---

class SampleValidationError(Exception):
    field: str          # 出错字段
    reason: str         # 原因描述

class SampleReadError(Exception):
    sample_type: SampleType
    reason: str         # "missing" | "io_error" | "invalid_json" | "invalid_structure"
    cause: Exception     # 原始异常
    property public_message:
        return f"Sample '{sample_type}' is unavailable: {reason}"
```

### 数据输出

```text
SampleEnvelope[T] — 不可变数据结构
SampleValidationError — 结构校验失败
SampleReadError — 读取失败统一包装 (含 sample_type, reason, cause, public_message)
```

### 验收

- [ ] `validate_sample` 对合法 Envelope 返回原对象
- [ ] `validate_sample` 对 sample_type 不匹配抛 SampleValidationError
- [ ] `validate_sample` 对 version < 1 的正式 Sample 抛 SampleValidationError
- [ ] `validate_sample` 对 version=0 的 fallback_empty 不抛错
- [ ] `validate_sample` 对非法 updated_at 抛 SampleValidationError
- [ ] `SampleReadError.public_message` 返回可读字符串

---

## Task 3: 领域模型 — 四类 Sample Data

**Files:**
- Create: `vps-gateway/app/domain/models/identity.py`
- Create: `vps-gateway/app/domain/models/preferences.py`
- Create: `vps-gateway/app/domain/models/memories.py`
- Create: `vps-gateway/app/domain/models/working_state.py`

**Interfaces:**
- Consumes: `sample.py` 的 `SampleEnvelope`
- Produces: `IdentityData`, `PreferencesData`, `MemoryItem`, `MemoriesData`, `WorkingStateData`

### 3.1 IdentityData

**数据输入:** 人工维护的核心身份 JSON

```text
interface IdentityData:
    name: string
    self_description: string
    values: string[]
    boundaries: string[]
    relationship_definition: string
```

**针对输入数据做的指令:**

```text
function validate_identity(data):
    # 指令:
    #   1. 校验 name 非空字符串
    #   2. 校验 self_description 非空字符串
    #   3. 校验 values 是字符串列表 (允许空列表)
    #   4. 校验 boundaries 是字符串列表 (允许空列表)
    #   5. 校验 relationship_definition 非空字符串
    #   6. 保持原始文字，不做模型润色
    #   7. 不满足时抛 SampleValidationError

    required = ["name", "self_description", "relationship_definition"]
    for field in required:
        if not data.get(field):
            raise SampleValidationError(field, "must be non-empty string")

    for field in ["values", "boundaries"]:
        if not isinstance(data.get(field), list):
            raise SampleValidationError(field, "must be a list")
        for item in data[field]:
            if not isinstance(item, str):
                raise SampleValidationError(field, "all items must be strings")

    return IdentityData(**data)
```

**数据输出:** `IdentityData` 不可变对象

### 3.2 PreferencesData

**数据输入:** 用户偏好和长期交互习惯 JSON

```text
interface PreferencesData:
    communication_preferences: string[]
    stable_likes: string[]
    stable_dislikes: string[]
    interaction_rules: string[]
```

**针对输入数据做的指令:**

```text
function validate_preferences(data):
    # 指令:
    #   1. 按原始条目读取，不自动推断新偏好
    #   2. 四个字段均为字符串列表，允许空列表
    #   3. 不满足时抛 SampleValidationError

    for field in ["communication_preferences", "stable_likes",
                  "stable_dislikes", "interaction_rules"]:
        if not isinstance(data.get(field), list):
            raise SampleValidationError(field, "must be a list")
        for item in data[field]:
            if not isinstance(item, str):
                raise SampleValidationError(field, "all items must be strings")

    return PreferencesData(**data)
```

**数据输出:** `PreferencesData` 不可变对象

### 3.3 MemoriesData

**数据输入:** 人工准备的一组记忆条目 JSON

```text
interface MemoryItem:
    id: string               # 唯一标识
    content: string          # 记忆正文
    category: string         # 分类
    priority: number         # 优先级 (数值越大越优先)
    created_at: string       # ISO 8601 创建时间

interface MemoriesData:
    items: MemoryItem[]
```

**针对输入数据做的指令:**

```text
function validate_memories(data):
    # 指令:
    #   1. 校验 items 是列表 (允许空列表)
    #   2. 校验每条记忆具有唯一 id
    #   3. 校验 content, category 为非空字符串
    #   4. 校验 priority 为数字
    #   5. 校验 created_at 为合法 ISO 8601
    #   6. 不满足时抛 SampleValidationError

    items = data.get("items", [])
    if not isinstance(items, list):
        raise SampleValidationError("items", "must be a list")

    seen_ids = set()
    for i, item in enumerate(items):
        for field in ["id", "content", "category"]:
            if not item.get(field):
                raise SampleValidationError(f"items[{i}].{field}", "must be non-empty string")

        if item["id"] in seen_ids:
            raise SampleValidationError(f"items[{i}].id", f"duplicate id: {item['id']}")
        seen_ids.add(item["id"])

        if not isinstance(item.get("priority"), (int, float)):
            raise SampleValidationError(f"items[{i}].priority", "must be a number")

        if not is_valid_iso8601(item.get("created_at", "")):
            raise SampleValidationError(f"items[{i}].created_at", "must be valid ISO 8601")

    return MemoriesData(items=parsed_items)

# 注意: 排序和字符预算由 ContextBuilder 在 M2 执行，M1 只负责读取和校验
```

**数据输出:** `MemoriesData` 不可变对象（items 顺序与文件一致，未排序）

### 3.4 WorkingStateData

**数据输入:** 当前关注、情绪、未完成事项 JSON

```text
interface WorkingStateData:
    current_focus: string[]
    emotion_summary: string
    pending_items: string[]
    next_wake_at: string | null    # ISO 8601 或 null
```

**针对输入数据做的指令:**

```text
function validate_working_state(data):
    # 指令:
    #   1. 校验 current_focus 为字符串列表
    #   2. 校验 emotion_summary 为字符串 (允许空字符串)
    #   3. 校验 pending_items 为字符串列表
    #   4. 校验 next_wake_at: null 合法；非 null 时必须为合法 ISO 8601
    #   5. 不满足时抛 SampleValidationError

    for field in ["current_focus", "pending_items"]:
        if not isinstance(data.get(field), list):
            raise SampleValidationError(field, "must be a list")

    if not isinstance(data.get("emotion_summary", ""), str):
        raise SampleValidationError("emotion_summary", "must be a string")

    next_wake = data.get("next_wake_at")
    if next_wake is not None and not is_valid_iso8601(next_wake):
        raise SampleValidationError("next_wake_at", "must be valid ISO 8601 or null")

    return WorkingStateData(**data)
```

**数据输出:** `WorkingStateData` 不可变对象

### 验收

- [ ] 四类 Data 模型对合法 JSON 正常构造
- [ ] IdentityData 缺少 name 时抛 SampleValidationError
- [ ] MemoriesData 有重复 id 时抛 SampleValidationError
- [ ] WorkingStateData 的 next_wake_at=null 合法
- [ ] WorkingStateData 的 next_wake_at 非法字符串时抛 SampleValidationError
- [ ] 所有模型构造后属性不可变

---

## Task 4: 端口接口 — SampleReader 与 SamplePublisher

**Files:**
- Create: `vps-gateway/app/domain/ports/__init__.py`
- Create: `vps-gateway/app/domain/ports/sample_reader.py`
- Create: `vps-gateway/app/domain/ports/sample_publisher.py`

**Interfaces:**
- Consumes: `sample.py` (SampleEnvelope, SampleType), 四个 data model
- Produces: `SampleReader` (抽象接口), `AllSamples`, `SamplePublisher` (预留)

### 数据输入

```text
无外部输入。定义抽象接口。
```

### 针对输入数据做的指令

```text
# --- SampleReader ---

interface SampleReader:
    function read<T>(sample_type: SampleType) -> SampleEnvelope<T>:
        # 指令:
        #   1. 根据固定映射选择文件，禁止外部传入任意路径
        #   2. 读取完整文件
        #   3. 解析 JSON
        #   4. 执行类型和字段校验
        #   5. 返回不可变对象
        #   6. 失败时抛 SampleReadError

    function read_all() -> AllSamples:
        # 指令:
        #   1. 调用 read("identity")，失败则抛 SampleReadError (不降级)
        #   2. 调用 read_or_empty("preferences", 空偏好)
        #   3. 调用 read_or_empty("memories", 空记忆)
        #   4. 调用 read_or_empty("working_state", 空状态)
        #   5. 返回 AllSamples

# --- AllSamples ---

interface AllSamples:
    identity: SampleEnvelope<IdentityData>
    preferences: SampleEnvelope<PreferencesData>
    memories: SampleEnvelope<MemoriesData>
    working_state: SampleEnvelope<WorkingStateData>

# --- 各类型的空值降级数据 ---

EMPTY_PREFERENCES = {
    communication_preferences: [],
    stable_likes: [],
    stable_dislikes: [],
    interaction_rules: []
}

EMPTY_MEMORIES = {
    items: []
}

EMPTY_WORKING_STATE = {
    current_focus: [],
    emotion_summary: "",
    pending_items: [],
    next_wake_at: null
}

# --- SamplePublisher (预留接口) ---

interface SamplePublisher:
    # 首版不提供实现、不提供 HTTP 入口、不由任何首版模块调用。
    # 未来实现必须校验版本并以原子方式发布。
    # 任何失败都必须保留上一份有效 Sample。

    function publish<T>(sample: SampleEnvelope<T>) -> int:
        # 预留合同:
        #   1. 校验 sample 结构
        #   2. 校验 version > 当前版本
        #   3. 原子写入文件
        #   4. 返回新版本号
        #   5. 失败时保留旧文件
        raise NotImplementedError("SamplePublisher is reserved for phase 2")
```

### 数据输出

```text
SampleReader — 抽象接口 (FileSampleRepository 在 Task 5 实现)
AllSamples — 四份 Sample 的聚合容器
SamplePublisher — 预留接口 (NotImplementedError)
```

### 验收

- [ ] `SampleReader` 是抽象类，不能直接实例化
- [ ] `SamplePublisher.publish()` 调用时抛 NotImplementedError
- [ ] `AllSamples` 包含四个正确类型的字段

---

## Task 5: FileSampleRepository 实现

**Files:**
- Create: `vps-gateway/app/adapters/__init__.py`
- Create: `vps-gateway/app/adapters/samples/__init__.py`
- Create: `vps-gateway/app/adapters/samples/file_sample_repository.py`

**Interfaces:**
- Consumes: `SampleReader` (端口), `Config.sample_directory`, `validate_sample`, 四个 data model 的校验函数, `get_logger`
- Produces: `FileSampleRepository` (SampleReader 的具体实现)

### 数据输入

```text
构造输入:
  sample_directory: str     — 来自 Config.sample_directory
  logger: Logger            — 来自 get_logger("file_sample_repository")

读取输入:
  sample_type: SampleType   — "identity" | "preferences" | "memories" | "working_state"
```

### 固定路径映射

```text
paths = {
    "identity":     "{sample_directory}/identity.sample.json",
    "preferences":   "{sample_directory}/preferences.sample.json",
    "memories":      "{sample_directory}/memories.sample.json",
    "working_state": "{sample_directory}/working_state.sample.json"
}
# 禁止由外部请求直接传入任意路径
```

### 针对输入数据做的指令

```text
class FileSampleRepository implements SampleReader:

    constructor(sample_directory, logger):
        self.paths = build_fixed_paths(sample_directory)
        self.logger = logger

    # --- 核心读取 ---

    function read(sample_type) -> SampleEnvelope:
        path = self.paths[sample_type]

        try:
            raw = read_entire_file(path)          # 完整读取，不流式
        catch file_missing:
            throw SampleReadError(sample_type, reason="missing", cause=error)
        catch io_error:
            throw SampleReadError(sample_type, reason="io_error", cause=error)

        try:
            parsed = parse_json(raw)
        catch json_error:
            throw SampleReadError(sample_type, reason="invalid_json", cause=error)

        try:
            validate_sample(parsed, expected_type=sample_type)
            data = validate_and_build_data(sample_type, parsed.data)
        catch SampleValidationError:
            throw SampleReadError(sample_type, reason="invalid_structure", cause=error)

        return freeze(parsed)  # 返回不可变对象

    # --- 降级读取 (仅用于非核心 Sample) ---

    function read_or_empty(sample_type, empty_data) -> SampleEnvelope:
        try:
            return self.read(sample_type)
        catch SampleReadError as error:
            self.logger.warning(
                "optional_sample_degraded",
                sample_type=sample_type,
                reason=error.reason
            )
            return SampleEnvelope(
                sample_type=sample_type,
                version=0,
                updated_at=now_iso(),
                source="fallback_empty",
                data=empty_data
            )

    # --- 读取全部 ---

    function read_all() -> AllSamples:
        # identity 不降级，失败直接抛出
        identity = self.read("identity")

        # 三类可选 Sample 降级为空
        preferences = self.read_or_empty("preferences", EMPTY_PREFERENCES)
        memories = self.read_or_empty("memories", EMPTY_MEMORIES)
        working_state = self.read_or_empty("working_state", EMPTY_WORKING_STATE)

        return AllSamples(
            identity=identity,
            preferences=preferences,
            memories=memories,
            working_state=working_state
        )

    # --- 内部: 根据 sample_type 调用对应校验 ---

    function validate_and_build_data(sample_type, raw_data):
        switch sample_type:
            case "identity":
                return validate_identity(raw_data)
            case "preferences":
                return validate_preferences(raw_data)
            case "memories":
                return validate_memories(raw_data)
            case "working_state":
                return validate_working_state(raw_data)
```

### 失败规则

```text
| Sample 类型   | read() 失败时          | read_all() 中行为        |
|---------------|------------------------|--------------------------|
| identity      | 抛 SampleReadError     | 不降级，异常向上传播     |
| preferences   | 抛 SampleReadError     | 降级为空偏好 + 警告日志   |
| memories      | 抛 SampleReadError     | 降级为空记忆 + 警告日志   |
| working_state | 抛 SampleReadError     | 降级为空状态 + 警告日志   |

降级 Envelope 规范:
  version = 0
  source = "fallback_empty"
  updated_at = 当前时间 ISO 8601
  不得与正式 Sample 混淆
```

### 数据输出

```text
read(sample_type) → SampleEnvelope<T> 或 抛 SampleReadError
read_all() → AllSamples (identity 可能抛 SampleReadError)
```

### 验收

- [ ] 四份合法 Sample 均能通过 `read()` 读取
- [ ] `read_all()` 返回包含四份 Sample 的 AllSamples
- [ ] identity 文件缺失时 `read_all()` 抛 SampleReadError
- [ ] preferences 文件缺失时 `read_all()` 返回 fallback_empty，日志含 `optional_sample_degraded`
- [ ] memories 文件 JSON 格式损坏时降级为空记忆
- [ ] working_state 文件结构校验失败时降级为空状态
- [ ] 降级 Envelope 的 version=0, source="fallback_empty"
- [ ] `read()` 不接受任意路径，只接受固定映射的 sample_type
- [ ] 返回的对象不可变

---

## Task 6: Sample JSON 文件

**Files:**
- Create: `vps-gateway/samples/identity.sample.json`
- Create: `vps-gateway/samples/preferences.sample.json`
- Create: `vps-gateway/samples/memories.sample.json`
- Create: `vps-gateway/samples/working_state.sample.json`

**Interfaces:**
- Consumes: 数据合同 5.1-5.5 定义的接口结构
- Produces: 四份合法 Sample 文件，供 FileSampleRepository 读取

### 数据输入

```text
数据来源: 人工准备的状态快照
格式: SampleEnvelope<T> 的 JSON 序列化
```

### 针对输入数据做的指令

```text
每份文件必须:
  1. 包含 sample_type 字段，值与文件对应
  2. version >= 1
  3. updated_at 为合法 ISO 8601
  4. source = "sample"
  5. data 字段符合对应 Data 接口
  6. JSON 格式合法，UTF-8 编码
```

### identity.sample.json 结构

```json
{
  "sample_type": "identity",
  "version": 1,
  "updated_at": "2025-01-01T00:00:00+08:00",
  "source": "sample",
  "data": {
    "name": "沉",
    "self_description": "一段自我描述",
    "values": ["价值1", "价值2"],
    "boundaries": ["边界1"],
    "relationship_definition": "与用户的关系定义"
  }
}
```

### preferences.sample.json 结构

```json
{
  "sample_type": "preferences",
  "version": 1,
  "updated_at": "2025-01-01T00:00:00+08:00",
  "source": "sample",
  "data": {
    "communication_preferences": ["偏好1"],
    "stable_likes": ["喜欢1"],
    "stable_dislikes": ["不喜欢1"],
    "interaction_rules": ["规则1"]
  }
}
```

### memories.sample.json 结构

```json
{
  "sample_type": "memories",
  "version": 1,
  "updated_at": "2025-01-01T00:00:00+08:00",
  "source": "sample",
  "data": {
    "items": [
      {
        "id": "mem_001",
        "content": "记忆内容",
        "category": "category_a",
        "priority": 10,
        "created_at": "2025-01-01T00:00:00+08:00"
      }
    ]
  }
}
```

### working_state.sample.json 结构

```json
{
  "sample_type": "working_state",
  "version": 1,
  "updated_at": "2025-01-01T00:00:00+08:00",
  "source": "sample",
  "data": {
    "current_focus": ["当前关注1"],
    "emotion_summary": "情绪概述",
    "pending_items": ["待办1"],
    "next_wake_at": null
  }
}
```

### 验收

- [ ] 四份 JSON 文件均能被 `FileSampleRepository.read()` 正常读取
- [ ] 每份文件的 `sample_type` 与文件名对应
- [ ] 每份文件的 `version >= 1`
- [ ] 每份文件的 `data` 通过对应校验函数

---

## Task 7: 集成验证

**Files:**
- Create: `vps-gateway/tests/__init__.py`
- Create: `vps-gateway/tests/unit/__init__.py`
- Create: `vps-gateway/tests/integration/__init__.py`
- Create: `vps-gateway/tests/unit/test_validate_sample.py`
- Create: `vps-gateway/tests/unit/test_identity_model.py`
- Create: `vps-gateway/tests/unit/test_preferences_model.py`
- Create: `vps-gateway/tests/unit/test_memories_model.py`
- Create: `vps-gateway/tests/unit/test_working_state_model.py`
- Create: `vps-gateway/tests/integration/test_file_sample_repository.py`

**Interfaces:**
- Consumes: M1 全部模块
- Produces: 验收通过的测试套件

### 数据输入

```text
测试用例需要的输入:
  1. 合法的四份 Sample JSON (临时目录)
  2. 缺失 identity 的场景
  3. 损坏 preferences 的场景
  4. 损坏 memories 的场景
  5. 损坏 working_state 的场景
  6. JSON 格式错误的场景
  7. 结构校验失败的场景
```

### 针对输入数据做的指令

```text
# --- 单元测试: validate_sample ---

test_valid_sample_passes:
    构造合法 SampleEnvelope → validate_sample → 通过

test_type_mismatch_raises:
    sample_type="identity" 但 expected="preferences" → SampleValidationError

test_version_below_1_raises:
    正式 Sample version=0 → SampleValidationError

test_fallback_empty_version_0_allowed:
    source="fallback_empty" version=0 → 通过

test_invalid_updated_at_raises:
    updated_at="not-a-date" → SampleValidationError

test_missing_data_raises:
    data=None → SampleValidationError

# --- 单元测试: IdentityData ---

test_valid_identity:
    全字段合法 → 构造成功

test_missing_name:
    name="" → SampleValidationError("name")

test_missing_self_description:
    self_description="" → SampleValidationError

test_values_not_list:
    values="not a list" → SampleValidationError

# --- 单元测试: MemoriesData ---

test_valid_memories:
    items 合法 → 构造成功

test_duplicate_id:
    两条记忆 id 相同 → SampleValidationError

test_empty_items_allowed:
    items=[] → 构造成功

test_missing_priority:
    priority 缺失 → SampleValidationError

# --- 单元测试: WorkingStateData ---

test_valid_working_state:
    全字段合法 → 构造成功

test_next_wake_at_null:
    next_wake_at=null → 通过

test_next_wake_at_invalid:
    next_wake_at="bad" → SampleValidationError

# --- 集成测试: FileSampleRepository ---

test_read_all_normal:
    创建临时目录，放入四份合法 JSON
    repo = FileSampleRepository(tmpdir)
    result = repo.read_all()
    assert result.identity.data.name == "沉"
    assert result.preferences.source == "sample"
    assert result.memories.data.items 非空
    assert result.working_state.data.emotion_summary 非空

test_identity_missing_raises:
    只放三份 JSON (无 identity)
    with pytest.raises(SampleReadError):
        repo.read_all()

test_preferences_missing_degrades:
    只放 identity + memories + working_state
    result = repo.read_all()
    assert result.preferences.source == "fallback_empty"
    assert result.preferences.version == 0
    assert result.preferences.data.communication_preferences == []

test_preferences_corrupt_degrades:
    identity 合法，preferences 文件内容为 "not json"
    result = repo.read_all()
    assert result.preferences.source == "fallback_empty"
    assert 日志含 "optional_sample_degraded"

test_memories_invalid_structure_degrades:
    memories JSON 合法但 data.items 不是列表
    result = repo.read_all()
    assert result.memories.source == "fallback_empty"

test_working_state_missing_degrades:
    无 working_state 文件
    result = repo.read_all()
    assert result.working_state.source == "fallback_empty"
    assert result.working_state.data.next_wake_at is None

test_read_rejects_arbitrary_path:
    # 确认 read() 不接受路径参数，只接受 SampleType
    repo.read("identity")  # 正确
    repo.read("/etc/passwd")  # 类型错误，编译期或运行期拒绝

test_envelope_is_immutable:
    envelope = repo.read("identity")
    with pytest.raises((AttributeError, TypeError)):
        envelope.data.name = "modified"  # 不可变
```

### 验收 (对应架构文档 12.1)

```text
验收清单:
  [ ] 1. 四份合法 Sample 均能读取
  [ ] 2. 拼接顺序固定且可测试 (read_all 的调用顺序: identity → preferences → memories → working_state)
  [ ] 3. Sample 中的特殊字符不会破坏结构边界 (M2 ContextBuilder 验证，M1 确保 JSON 解析不报错)
  [ ] 4. identity 无效时不会调用上游模型 (M1 层面: read_all() 抛 SampleReadError，不返回数据)
  [ ] 5. 三类非核心 Sample 缺失时使用明确空值
  [ ] 6. 三类非核心 Sample 内容损坏时同样降级为空值，并记录 optional_sample_degraded
```

---

## M1 完成后的接口快照

以下接口供 M2 (ContextBuilder + OpenAIUpstreamClient) 消费:

```text
# M2 将依赖的接口

SampleReader:
    read_all() -> AllSamples

AllSamples:
    identity: SampleEnvelope<IdentityData>
    preferences: SampleEnvelope<PreferencesData>
    memories: SampleEnvelope<MemoriesData>
    working_state: SampleEnvelope<WorkingStateData>

SampleEnvelope<T>:
    sample_type: SampleType
    version: int
    updated_at: str
    source: str
    data: T

# M2 ContextBuilder 将从 AllSamples 中提取:
#   - identity.data → 最高优先级状态
#   - preferences.data → 偏好
#   - memories.data.items → 按 priority 降序、created_at 升序排序 + 字符预算截取
#   - working_state.data → 当前工作状态
#   - 各 envelope.version → 日志和 Outbox 追踪
```
