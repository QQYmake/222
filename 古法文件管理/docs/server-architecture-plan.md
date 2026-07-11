# Health-Bridge 服务端核心架构计划

> 依据 `health-bridge-design.md` 编写。本文件只描述模块边界、输入/输出数据流和操作语义，
> 不展开具体函数实现。用于人工审核后再进入实现阶段。

---

## 1. 系统数据流总览

```
                          HTTPS
    Push Client ──────────────────► Nginx :443
    (health_push.py)    upload      │
                                     │ proxy_pass
                                     ▼
                              FastAPI :8765
                              (127.0.0.1 only)
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
              Auth Middleware   Ingest Pipeline   Read Endpoints
              (token verify)    (POST /upload)    (GET latest/
                    │                │              data/weeks/
                    │                ▼              archive)
                    │         Schema Inspector      │
                    │                │              │
                    │                ▼              │
                    │         Device Resolver       │
                    │                │              │
                    │                ▼              │
                    │         Type Adapters          │
                    │         (heart_rate/sleep/     │
                    │          steps)                │
                    │                │              │
                    │                ▼              │
                    │         Normalizer             │
                    │                │              │
                    │                ▼              │
                    │    ┌── Normalized SQLite ──┐  │
                    │    │   (health.sqlite3)    │  │
                    │    └───────────┬───────────┘  │
                    │                │              │
                    │          ┌─────┴──────┐       │
                    │          ▼            ▼       │
                    │   Archive Gen   Latest JSON   │
                    │   (.md files)   (.json files) │
                    │          │            │       │
                    │          ▼            ▼       │
                    └──────────┴────────────┴───────┘
                          /srv/health-bridge/
                          archives/  latest/
```

### 外部接口

| 接口 | 方向 | 协议 | 认证 |
|------|------|------|------|
| Push Client → Nginx | 上传 gzip SQLite 快照 | HTTPS multipart POST | `X-Upload-Token` |
| Pull Client → Nginx | 查询最新值/历史范围/周归档 | HTTPS GET | `Authorization: Bearer` |
| Nginx → FastAPI | 反向代理 `/health/` 路径 | HTTP (localhost) | 透传原始头部 |
| FastAPI → SQLite | 读写归一化数据 | 本地文件 I/O | 无（进程内） |
| FastAPI → 文件系统 | 写归档/latest JSON | 本地文件 I/O | 无（进程内） |

---

## 2. 模块清单与接口定义

### 2.1 Nginx 反向代理

**职责：** TLS 终止 + 路径路由 + 请求体大小限制

**输入数据：**
- 来自公网的 HTTPS 请求（`/health/` 路径）
- 请求头：`X-Upload-Token` 或 `Authorization`，`Content-Type: multipart/form-data`
- 请求体：gzip 压缩的 SQLite 快照（最大 100MB）

**输出数据：**
- 转发到 `127.0.0.1:8765` 的 HTTP 请求（保留原始路径和头部）
- 返回给客户端的 HTTPS 响应

**操作：**
- 匹配 `location ^~ /health/`，proxy_pass 到 `127.0.0.1:8765`
- 设置 `client_max_body_size 100m`
- 透传 `Host`、`X-Real-IP`、`X-Forwarded-For`、`X-Forwarded-Proto`
- 不修改 `/chat/` 等其他 location 的行为
- `nginx -t` 验证后 reload；reload 后验证 `/chat/` 不受影响

**不涉及：** 令牌校验、数据解析、业务逻辑

---

### 2.2 FastAPI 应用入口 (`main.py`)

**职责：** 路由分发 + 请求/响应序列化

**输入数据：**
- HTTP 请求（经 Nginx 转发）：路径、方法、头部、查询参数、请求体

**输出数据：**
- HTTP 响应：JSON 或 `text/markdown`
- 响应头：`Content-Type`、状态码

**操作：**
- 定义 6 个端点路由（见下表）
- 每个端点先经过 Auth Middleware 校验令牌
- 将请求分发给对应处理模块
- 将处理模块的返回值序列化为 HTTP 响应
- 全局异常处理：捕获已知错误类型，返回统一错误 JSON

**端点路由表：**

| 端点 | 方法 | 认证 | 处理模块 |
|------|------|------|----------|
| `/health/api/v1/health` | GET | 无 | 直接返回版本+存活状态 |
| `/health/api/v1/upload` | POST | `X-Upload-Token` | Ingest Pipeline |
| `/health/api/v1/latest` | GET | `Bearer` | Normalized Storage 查询 |
| `/health/api/v1/data` | GET | `Bearer` | Normalized Storage 查询 |
| `/health/api/v1/weeks` | GET | `Bearer` | Archive 目录扫描 |
| `/health/api/v1/archive/{week}/{type}` | GET | `Bearer` | Archive 文件读取 |

---

### 2.3 Auth Middleware (`auth.py`)

**职责：** 令牌校验（上传令牌 vs 读取令牌分离）

**输入数据：**
- HTTP 请求头部：`X-Upload-Token` 和/或 `Authorization: Bearer <token>`
- 环境变量/配置文件中的预期令牌值

**输出数据：**
- 校验通过：无返回（请求继续）
- 校验失败：HTTP 401/403 + 错误 JSON

**操作：**
- 按端点确定需要哪种令牌（upload 端点需要 upload token，read 端点需要 read token）
- 使用 `hmac.compare_digest` 做常量时间比较
- 令牌值永不出现在日志、异常消息、响应体中
- `/health` 存活检查端点不需要令牌

---

### 2.4 Ingest Pipeline (`ingest.py`)

**职责：** 接收上传 → 校验 → 存储 → 导入 → 触发归档生成

**输入数据：**
- HTTP multipart 请求体（gzip 压缩的 SQLite 快照）
- 请求头：`X-Snapshot-SHA256`（客户端计算的哈希）
- 配置：最大解压大小、incoming/raw 目录路径

**输出数据：**
- HTTP 响应 JSON：`{ snapshot_hash, is_new, status, imported_counts }`
- `status` 取值：`uploaded` | `duplicate` | `unsupported_schema`
- 副作用：写入 raw 快照、写入 SQLite 元数据、写入归一化观测、触发归档/latest 生成

**操作（按顺序）：**

```
INPUT: multipart body (gzip), X-Snapshot-SHA256 header, config

1. 接收并保存到 incoming/ 临时文件
   OUTPUT: temp file path

2. 计算 gzip 解压后大小，拒绝超限（防 gzip bomb）
   OUTPUT: accept / reject(422)

3. 解压 gzip → 验证 SQLite magic bytes → PRAGMA quick_check
   OUTPUT: validated SQLite file / reject(422)

4. 计算 SHA-256，与 SQLite 元数据表中的最近接受哈希比较
   OUTPUT: duplicate(跳过) / new(继续)

5. 原子移动到 raw/ 目录（服务端生成文件名，不信任客户端文件名）
   OUTPUT: raw snapshot path

6. 调用 Schema Inspector → 获取 schema 指纹
   → 调用 Device Resolver → 获取稳定设备/用户 ID
   → 调用 Type Adapters → 获取原始观测列表
   → 调用 Normalizer → 获取归一化观测 + 去重键
   → 写入 Normalized SQLite（按去重键 UPSERT）

7. 写入摄入元数据（哈希、时间、schema 指纹、导入计数、状态）

8. 触发 Archive Generator（重新生成受影响周）
   触发 Latest JSON Generator（更新 latest 文件）

9. 加锁防止并发摄入重叠快照

OUTPUT: { snapshot_hash, is_new, status, imported_counts }
```

**异常分支：**
- schema 未知 → 保留 raw 快照，记录 `unsupported_schema`，返回 HTTP 202
- 校验失败（非 SQLite/损坏/超限）→ 返回 HTTP 422，不保留快照
- 令牌无效 → 返回 HTTP 401/403（由 Auth Middleware 处理）

---

### 2.5 Schema Inspector (`schema_inspector.py`)

**职责：** 探测 Gadgetbridge DB 结构，生成 schema 指纹

**输入数据：**
- 已校验的 SQLite 文件路径（只读打开）

**输出数据：**
- schema 指纹（字符串，用于判断是否已知 schema）
- 表名列表 + 每张表的列名/类型列表
- 是否为已知支持的 schema（布尔值）

**操作：**
```
INPUT: validated SQLite file path

1. 以只读 URI 打开 SQLite
2. 查询 sqlite_master 获取所有表名
3. 对每张表执行 PRAGMA table_info 获取列定义
4. 对表名+列名集合做排序+拼接 → 计算 hash 作为 schema 指纹
5. 将指纹与已知支持列表比对

OUTPUT: { fingerprint, tables: [{name, columns}], is_supported }
```

**已知支持 schema：** Gadgetbridge 0.92.1 + Xiaomi Mi Band 8（含 `XIAOMI_ACTIVITY_SAMPLE`、`XIAOMI_DAILY_SUMMARY_SAMPLE`、`XIAOMI_SLEEP_TIME_SAMPLE`、`XIAOMI_SLEEP_STAGE_SAMPLE`）

---

### 2.6 Device Resolver (`device_resolver.py`)

**职责：** 将 Gadgetbridge 的设备/用户 ID 映射为服务端稳定内部 ID

**输入数据：**
- Gadgetbridge DB 中的 `DEVICE` 表行：`(_id, NAME, IDENTIFIER, TYPE_NAME, MODEL)`
- Gadgetbridge DB 中的 `USER` 表行：`(_id, NAME)`
- Normalized SQLite 中的 `devices` 和 `users` 表（已有映射）

**输出数据：**
- 稳定内部设备 ID（整数）
- 稳定内部用户 ID（整数）

**操作：**
```
INPUT: DEVICE rows, USER rows from source DB; existing mappings in server DB

1. 查询服务端 DB 是否已有匹配（按 IDENTIFIER 匹配设备，按 NAME 匹配用户）
2. 若已有 → 返回已分配的内部 ID
3. 若没有 → 插入新记录，分配新的内部 ID

OUTPUT: { internal_device_id, internal_user_id }
```

**注意：** 内部 ID 不通过未认证端点暴露

---

### 2.7 Type Adapters (`adapters/`)

**职责：** 从 Gadgetbridge DB 源表中提取特定类型的数据行，转为统一中间格式

#### 2.7.1 心率适配器 (`heart_rate.py`)

**输入数据：**
- `XIAOMI_ACTIVITY_SAMPLE` 表中 `HEART_RATE > 0` 的行
- 每行字段：`TIMESTAMP`(秒), `DEVICE_ID`, `USER_ID`, `HEART_RATE`(BPM)

**输出数据：**
- 原始观测列表，每条：`{ source_timestamp_sec, device_id, user_id, type="heart_rate", value={bpm: int} }`

**操作：**
```
INPUT: SQLite connection, internal_device_id, internal_user_id

1. SELECT TIMESTAMP, HEART_RATE FROM XIAOMI_ACTIVITY_SAMPLE
   WHERE DEVICE_ID=? AND USER_ID=? AND HEART_RATE > 0
2. 每行 → { source_timestamp_sec: TIMESTAMP, type: "heart_rate",
            value: { bpm: HEART_RATE } }

OUTPUT: list[RawObservation]
```

#### 2.7.2 步数适配器 (`steps.py`)

**输入数据：**
- `XIAOMI_ACTIVITY_SAMPLE` 表中 `STEPS > 0` 的行（逐采样点步数）
- `XIAOMI_DAILY_SUMMARY_SAMPLE` 表全量行（每日汇总步数）
- 注意：`DAILY_SUMMARY` 的 `TIMESTAMP` 单位是**毫秒**，`ACTIVITY_SAMPLE` 是**秒**

**输出数据：**
- 逐采样点观测列表：`{ source_timestamp_sec, type="steps", value={steps: int, source: "activity_sample"} }`
- 每日汇总观测列表：`{ source_timestamp_sec, type="steps_daily", value={steps: int, source: "daily_summary"} }`

**操作：**
```
INPUT: SQLite connection, internal_device_id, internal_user_id

1. SELECT TIMESTAMP, STEPS FROM XIAOMI_ACTIVITY_SAMPLE
   WHERE DEVICE_ID=? AND USER_ID=? AND STEPS > 0
   → 每行 timestamp 单位为秒

2. SELECT TIMESTAMP, STEPS FROM XIAOMI_DAILY_SUMMARY_SAMPLE
   WHERE DEVICE_ID=? AND USER_ID=?
   → 每行 timestamp 单位为毫秒，需 / 1000 转为秒

OUTPUT: list[RawObservation] (合并两类，标记 source)
```

#### 2.7.3 睡眠适配器 (`sleep.py`)

**输入数据：**
- `XIAOMI_SLEEP_TIME_SAMPLE` 表全量行（睡眠会话）
  - 字段：`TIMESTAMP`(秒), `WAKEUP_TIME`(秒), `IS_AWAKE`, `TOTAL_DURATION`, `DEEP_SLEEP_DURATION`, `LIGHT_SLEEP_DURATION`, `REM_SLEEP_DURATION`, `AWAKE_DURATION`
- `XIAOMI_SLEEP_STAGE_SAMPLE` 表全量行（睡眠分期）
  - 字段：`TIMESTAMP`(秒), `STAGE`(整数代码)

**输出数据：**
- 会话观测列表：`{ source_timestamp_sec, type="sleep_session", value={ wakeup_time, duration, deep, light, rem, awake } }`
- 分期观测列表：`{ source_timestamp_sec, type="sleep_stage", value={ stage_code: int } }`

**操作：**
```
INPUT: SQLite connection, internal_device_id, internal_user_id

1. SELECT * FROM XIAOMI_SLEEP_TIME_SAMPLE WHERE DEVICE_ID=? AND USER_ID=?
   → 每行 → { type: "sleep_session", value: { wakeup_time, total_duration,
            deep_sleep, light_sleep, rem_sleep, awake_duration } }

2. SELECT TIMESTAMP, STAGE FROM XIAOMI_SLEEP_STAGE_SAMPLE WHERE DEVICE_ID=? AND USER_ID=?
   → 每行 → { type: "sleep_stage", value: { stage_code: STAGE } }

OUTPUT: list[RawObservation]
```

**注意：** 当前 DB 中睡眠表为 0 行，但 schema 已就绪。适配器必须完整实现，用合成 fixture 测试。

---

### 2.8 Normalizer (`normalizer.py`)

**职责：** 将原始观测转为归一化观测，生成确定性去重键

**输入数据：**
- 原始观测列表（来自 Type Adapters）：`{ source_timestamp_sec, device_id, user_id, type, value }`
- 稳定内部设备 ID、用户 ID

**输出数据：**
- 归一化观测列表，每条：
  ```
  {
    dedup_key: str,          # 确定性唯一键
    type: str,               # heart_rate / steps / steps_daily / sleep_session / sleep_stage
    timestamp_utc: ISO-8601, # 从 source_timestamp_sec 转换
    timestamp_local: ISO-8601, # Asia/Shanghai 时区
    value: JSON,             # 归一化后的值
    raw_source: JSON,        # 保留原始字段供未来重新解释
    source_table: str,       # 源表名
    source_identity: str,    # 源行标识
    internal_device_id: int,
    internal_user_id: int
  }
  ```

**操作：**
```
INPUT: list[RawObservation], internal_device_id, internal_user_id

1. 对每条原始观测：
   a. 将 source_timestamp_sec → UTC ISO-8601
   b. 将 source_timestamp_sec → Asia/Shanghai ISO-8601（含 +08:00 偏移）
   c. 生成 dedup_key = sha256(internal_device_id + type + source_timestamp_sec + source_identity + stable_value_fields)
   d. 保留原始字段到 raw_source JSON

OUTPUT: list[NormalizedObservation]
```

**去重键语义：** 相同 device + type + timestamp + source_identity + value 的观测被视为重复。重复上传相同或重叠的 Gadgetbridge DB 不会产生重复观测。

---

### 2.9 Normalized Storage (`database.py`)

**职责：** SQLite 数据库初始化、迁移、读写管理

**输入数据：**
- 归一化观测列表（写入时）
- 查询参数：type, from, to, limit, cursor（读取时）

**输出数据：**
- 写入：影响的行数、去重计数
- 读取：观测列表 + 分页游标

**操作：**
```
初始化:
  - 创建表：snapshots, devices, users, observations, archive_state
  - 若已有数据库 → 备份后执行迁移

写入 (Ingest Pipeline 调用):
INPUT: list[NormalizedObservation], snapshot_metadata
  1. 按 dedup_key 做 UPSERT（存在则跳过，不存在则插入）
  2. 记录摄入计数（new_count, duplicate_count）
  3. 写入 snapshot 元数据
  OUTPUT: { new_count, duplicate_count }

读取 — latest 查询:
INPUT: type
  1. SELECT * FROM observations WHERE type=? ORDER BY timestamp_utc DESC LIMIT 1
  OUTPUT: NormalizedObservation | null

读取 — range 查询:
INPUT: type, from_ts, to_ts, limit, cursor
  1. SELECT * FROM observations
     WHERE type=? AND timestamp_utc BETWEEN ? AND ?
     ORDER BY timestamp_utc ASC
     LIMIT ? + 1  -- 多取 1 行判断是否有下一页
  2. 生成 cursor（最后一条的 timestamp_utc + source_identity）
  OUTPUT: { observations: list, next_cursor: str | null }

读取 — 周归档数据:
INPUT: week_id (如 "2026-W28"), type
  1. 计算 week 的 Monday 00:00 ~ Sunday 23:59:59 (Asia/Shanghai)
  2. 转为 UTC 范围
  3. SELECT * FROM observations WHERE type=? AND timestamp_utc IN range
  OUTPUT: list[NormalizedObservation]
```

**表结构概要：**

| 表 | 关键字段 | 用途 |
|----|---------|------|
| `snapshots` | hash, received_at, schema_fingerprint, status, import_counts | 摄入元数据 |
| `devices` | internal_id, name, identifier, type_name, model | 稳定设备映射 |
| `users` | internal_id, name | 稳定用户映射 |
| `observations` | dedup_key(UNIQUE), type, timestamp_utc, timestamp_local, value(JSON), raw_source(JSON), source_table, source_identity, internal_device_id, internal_user_id | 归一化观测 |
| `archive_state` | week_id, type, last_generated_at | 归档生成状态 |

---

### 2.10 Archive Generator (`archive.py`)

**职责：** 按 ISO 周生成 Markdown 归档文件

**输入数据：**
- 受影响的周 ID 列表（从刚导入的观测时间戳推算）
- 每个周的归一化观测数据（从 Normalized Storage 查询）

**输出数据：**
- 文件系统中的 Markdown 文件：
  ```
  /srv/health-bridge/archives/2026-W28/
    summary.md
    heart_rate.md
    sleep.md
    steps.md
  ```

**操作：**
```
INPUT: list[week_id], SQLite connection

对每个 week_id:
  1. 查询该周所有类型的归一化观测
  2. 生成 summary.md:
     - 列出可用类型
     - 每类型的记录数
     - 覆盖日期范围
     - 指向各类型文件的链接
  3. 对每个类型生成 {type}.md:
     - 心率: 按日期分组 → 按时间排序 → 列出 timestamp + BPM
     - 步数: 按日期分组 → 按时间排序 → 列出 timestamp + steps
     - 睡眠: 列出检测到的会话和分期（stage code 不作为医学权威解释）
  4. 原子写入（临时文件 → os.replace）
  5. 更新 archive_state 表

OUTPUT: 无返回值（副作用：文件写入 + DB 状态更新）
```

**规则：**
- ISO 周从周一到周日，时区 Asia/Shanghai
- 输出确定性：相同输入永远产生相同文件
- 每个类型文件内按本地日期分组，再按本地时间排序
- 原子替换：读者永远不会看到部分写入的文件

---

### 2.11 Latest JSON Generator (`latest.py`)

**职责：** 为每种类型生成最新的 JSON 快照文件

**输入数据：**
- Normalized Storage 中每种类型的最新一条观测

**输出数据：**
- 文件系统中的 JSON 文件：
  ```
  /srv/health-bridge/latest/heart_rate.json
  /srv/health-bridge/latest/sleep.json
  /srv/health-bridge/latest/steps.json
  ```

**操作：**
```
INPUT: SQLite connection

对每种类型 (heart_rate, sleep, steps):
  1. 查询最新一条观测
  2. 序列化为 JSON
  3. 原子写入（临时文件 → os.replace）

OUTPUT: 无返回值（副作用：文件写入）
```

**用途：** 供巡检、备份、简单集成使用。API 也可直接查询 SQLite，但 latest 文件提供轻量级访问。

---

## 3. 模块间数据流详图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         POST /upload                                 │
│                                                                      │
│  multipart body ──► Ingest Pipeline                                  │
│                         │                                            │
│                    ┌────┴────┐                                       │
│                    │ gzip    │ ── 拒绝超限(422)                      │
│                    │ 解压    │                                       │
│                    │ +校验   │                                       │
│                    └────┬────┘                                       │
│                         │ validated SQLite file                      │
│                         ▼                                            │
│                    ┌────┴────┐                                       │
│                    │SHA-256  │ ── 重复 → 返回 duplicate               │
│                    │去重     │                                       │
│                    └────┬────┘                                       │
│                         │ new snapshot                               │
│                         ▼                                            │
│              ┌──────────┴──────────┐                                 │
│              │  Schema Inspector   │                                 │
│              │  → fingerprint      │                                 │
│              │  → is_supported?    │── 否 → 保留 raw, 返回 202       │
│              └──────────┬──────────┘                                 │
│                         │ supported schema                           │
│                         ▼                                            │
│              ┌──────────┴──────────┐                                 │
│              │  Device Resolver    │                                 │
│              │  → internal_dev_id  │                                 │
│              │  → internal_user_id │                                 │
│              └──────────┬──────────┘                                 │
│                         │                                            │
│              ┌──────────┼──────────┐                                 │
│              ▼          ▼          ▼                                 │
│          ┌──────┐  ┌──────┐  ┌──────┐                               │
│          │ HR   │  │Steps │  │Sleep │  Type Adapters                 │
│          │adapter│ │adapter│ │adapter│                               │
│          └──┬───┘  └──┬───┘  └──┬───┘                               │
│             │         │         │                                    │
│             └────┬────┴─────────┘                                    │
│                  ▼                                                   │
│           ┌────────────┐                                             │
│           │ Normalizer │                                             │
│           │ → dedup_key│                                             │
│           │ → UTC+local│                                             │
│           └──────┬─────┘                                             │
│                  │                                                   │
│                  ▼                                                   │
│     ┌────────────────────────┐                                      │
│     │  Normalized SQLite     │                                      │
│     │  UPSERT by dedup_key   │                                      │
│     │  + snapshot metadata   │                                      │
│     └───────────┬────────────┘                                      │
│                 │                                                    │
│          ┌──────┴──────┐                                             │
│          ▼             ▼                                             │
│   ┌───────────┐ ┌───────────┐                                       │
│   │ Archive   │ │ Latest    │                                       │
│   │ Generator │ │ JSON Gen  │                                       │
│   │ → .md     │ │ → .json   │                                       │
│   └───────────┘ └───────────┘                                       │
│                                                                      │
│  ◄── { hash, is_new, status, imported_counts } ──── HTTP Response   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                       GET /latest?type=                              │
│                                                                      │
│  query param ──► Normalized SQLite SELECT                            │
│                       │                                              │
│  ◄── { timestamp_utc, timestamp_local, type, value } ─── JSON       │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    GET /data?type=&from=&to=&limit=                  │
│                                                                      │
│  query params ──► Normalized SQLite SELECT + pagination              │
│                       │                                              │
│  ◄── { observations: [...], next_cursor: "..." } ─── JSON           │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    GET /archive/{week}/{type}                        │
│                                                                      │
│  path params ──► 读取 /archives/{week}/{type}.md 文件                │
│                       │                                              │
│  ◄── Markdown 文件内容 ─── text/markdown                             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. 服务端文件系统布局

```
/srv/health-bridge/
  app/
    main.py              # FastAPI 应用入口
    config.py            # 令牌路径、大小限制、目录路径
    auth.py              # 令牌校验
    ingest.py            # 上传处理管线
    schema_inspector.py  # Gadgetbridge schema 探测
    device_resolver.py   # 设备/用户 ID 映射
    normalizer.py        # 归一化 + 去重键
    database.py          # SQLite 初始化/迁移/连接
    archive.py           # ISO 周 Markdown 归档
    latest.py            # latest JSON 生成
    adapters/
      __init__.py
      base.py            # 适配器注册机制
      heart_rate.py
      steps.py
      sleep.py
  data/
    incoming/            # 上传临时文件
    raw/                 # 已接受的原始快照
    health.sqlite3       # 归一化数据库
  archives/              # Markdown 周归档
  latest/                # latest JSON 文件
  logs/
```

---

## 5. 安全约束摘要

| 约束 | 实现位置 |
|------|---------|
| HTTPS 强制 | Nginx TLS + transport 层校验 upload_url scheme |
| 上传/读取令牌分离 | Auth Middleware |
| 常量时间令牌比较 | Auth Middleware (`hmac.compare_digest`) |
| 令牌不出现在日志/响应/异常 | 所有模块 |
| gzip bomb 防护 | Ingest Pipeline（解压大小限制） |
| SQLite magic + quick_check | Ingest Pipeline |
| 服务端生成文件名 | Ingest Pipeline（不信任客户端文件名） |
| 原子写入 | Archive Generator, Latest JSON, State |
| 并发摄入锁 | Ingest Pipeline |
| 8765 端口不公开 | systemd 绑定 127.0.0.1 + 无防火墙规则 |
| raw/token/db 不被 Nginx 用户读取 | 文件系统权限 |

---

## 6. 与推送客户端的接口契约

推送客户端（已完成）通过以下接口与服务端交互：

| 项目 | 值 |
|------|-----|
| URL | `{base_url}/health/api/v1/upload` |
| 方法 | POST |
| 认证 | `X-Upload-Token: <upload_token>` |
| 请求体 | `multipart/form-data`，字段名 `file`，文件名 `snapshot.db.gz` |
| 额外头部 | `X-Snapshot-SHA256: <hash>`, `Content-Encoding: gzip` |
| 成功响应 | HTTP 200/201: `{ "status": "ok"/"uploaded", ... }` |
| Schema 未知 | HTTP 202: `{ "status": "unsupported_schema" }` |
| 校验失败 | HTTP 422 |
| 认证失败 | HTTP 401/403 |
| 瞬时错误 | HTTP 429/5xx（客户端重试） |
