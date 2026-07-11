# Health-Bridge 拉取客户端架构计划

> 基于 `health-bridge-design.md` §4.2 拉取客户端规格，对接已实现的 VPS API 服务端（6 端点）。
> 编写规则：伪代码，只写输入数据 + 输出数据 + 对数据的操作，不展开函数签名。

---

## 1. 数据流总览图

```
┌─────────────────────────────────────────────────────────────┐
│                        health_pull.py (CLI)                  │
│                                                              │
│  ┌──────────┐   ┌──────────────┐   ┌─────────────────────┐  │
│  │ pull_     │   │ pull_        │   │ pull_watch          │  │
│  │ config.py │   │ transport.py │   │ (watch loop)        │  │
│  │           │──▶│              │   │                     │  │
│  │ 配置加载  │   │ HTTP 请求    │   │ 轮询 + 原子写入     │  │
│  │ 环境变量  │   │ Bearer 认证  │   │ 变更通知            │  │
│  └──────────┘   └──────┬───────┘   └────────┬────────────┘  │
│                        │                     │               │
│  ┌─────────────┐       │                     │               │
│  │ pull_       │       │                     │               │
│  │ commands.py │◀──────┘                     │               │
│  │             │                             │               │
│  │ latest      │                             │               │
│  │ range       │◀────────────────────────────┘               │
│  │ week        │                                             │
│  │ weeks       │                                             │
│  └──────┬──────┘                                             │
│         │                                                    │
│         │  JSON / Markdown                                   │
│         ▼                                                    │
│  ┌──────────────┐                                            │
│  │ pull_output  │                                            │
│  │ .py          │                                            │
│  │              │                                            │
│  │ stdout       │                                            │
│  │ file write   │                                            │
│  │ atomic write │                                            │
│  └──────────────┘                                            │
└─────────────────────────────────────────────────────────────┘
         │
         │  HTTPS GET  (Bearer token)
         ▼
┌─────────────────────────────────────────────────────────────┐
│                   VPS API 服务端 (已实现)                    │
│                                                              │
│  GET /health/api/v1/health          → {status, version}     │
│  GET /health/api/v1/latest?type=    → {type: {obs}}         │
│  GET /health/api/v1/data?type=&     → {observations, cursor}│
│      from=&to=&limit=&cursor=                                │
│  GET /health/api/v1/weeks           → {weeks: [...]}        │
│  GET /health/api/v1/archive/{week}  → text/markdown         │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 模块伪代码

### 2.1 pull_config.py — 配置加载

```
INPUT:  环境变量 + 可选 JSON 配置文件路径

OPERATIONS:
  - 读取环境变量 HEALTH_PULL_BASE_URL（默认 "https://oh-my-frontweb.duckdns.org"）
  - 读取环境变量 HEALTH_READ_TOKEN（必需，Bearer 令牌）
  - 读取环境变量 HEALTH_PULL_TIMEOUT（默认 30 秒）
  - 读取环境变量 HEALTH_PULL_TIMEZONE（默认 "Asia/Shanghai"）
  - 如果提供了 --config 文件路径，加载 JSON 并覆盖环境变量
  - 构建 base_url = 去除尾部斜杠
  - 构建 api_base = base_url + "/health/api/v1"
  - 验证 base_url 以 https:// 开头（生产环境强制 TLS）
  - 验证 read_token 非空（除非 dry-run 模式）

OUTPUT: PullConfig dataclass
  - base_url: str
  - api_base: str
  - read_token: str
  - timeout_seconds: int
  - timezone: str
  - output_dir: Path | None  (--output-dir 参数)
  - dry_run: bool
```

### 2.2 pull_transport.py — HTTP 传输层

```
INPUT:  PullConfig + HTTP 方法 + API 路径 + 查询参数 + 请求头

OPERATIONS:
  - 构建完整 URL = config.api_base + path + "?" + urlencode(params)
  - 添加 Authorization: Bearer {read_token} 头部
  - 添加 User-Agent: health-bridge-pull/1.0
  - 创建 HTTPS 连接（或 connection_factory 注入，用于测试）
  - 发送 GET 请求
  - 读取响应体（有界读取，防止内存溢出）
  - 解析响应：
      HTTP 200 → 返回响应体 bytes
      HTTP 401 → raise AuthError("认证失败")
      HTTP 403 → raise AuthError("权限不足")
      HTTP 404 → raise NotFoundError("资源不存在")
      HTTP 429 → raise TransientError("限流")
      HTTP 5xx → raise TransientError("服务器错误")
      其他 → raise TransportError("意外状态码")

OUTPUT: 响应体 bytes（由调用方解析为 JSON 或文本）
```

### 2.3 pull_commands.py — 命令处理器

#### 2.3.1 cmd_latest

```
INPUT:  PullConfig + type: str | None

OPERATIONS:
  - 构建 API 路径 = "/latest"
  - 如果 type 非空，添加查询参数 ?type={type}
  - 调用 transport 发送 GET 请求
  - 解析响应 JSON
  - 对每条观测值，格式化为：
      {type, timestamp_utc, timestamp_local, value, source_table}
  - 如果指定了单一 type，提取该 type 的值

OUTPUT: dict[str, Observation | None]
  - 键 = 数据类型（heart_rate / steps / steps_daily / sleep_stage）
  - 值 = 最新观测值或 None
```

#### 2.3.2 cmd_range

```
INPUT:  PullConfig + type: str + from_ts: str | None + to_ts: str | None
        + limit: int (默认 100) + cursor: str | None

OPERATIONS:
  - 构建 API 路径 = "/data"
  - 查询参数: type={type}, from={from_ts}, to={to_ts}, limit={limit}
  - 如果 cursor 非空，添加 cursor={cursor}
  - 调用 transport 发送 GET 请求
  - 解析响应 JSON: {observations: [...], next_cursor: str | None}

OUTPUT: RangeResult dataclass
  - observations: list[Observation]
  - next_cursor: str | None
  - has_more: bool = next_cursor is not None
```

#### 2.3.3 cmd_weeks

```
INPUT:  PullConfig

OPERATIONS:
  - 构建 API 路径 = "/weeks"
  - 调用 transport 发送 GET 请求
  - 解析响应 JSON: {weeks: ["2026-W28", ...]}

OUTPUT: list[str]  (ISO 周标识列表，按时间排序)
```

#### 2.3.4 cmd_archive

```
INPUT:  PullConfig + week_id: str (如 "2026-W28")

OPERATIONS:
  - 构建 API 路径 = "/archive/{week_id}"
  - 调用 transport 发送 GET 请求
  - 响应体为 Markdown 文本（text/markdown）

OUTPUT: str  (Markdown 归档内容)
```

### 2.4 pull_watch.py — Watch 模式

```
INPUT:  PullConfig + types: list[str] + interval: int (秒) + output_dir: Path

OPERATIONS:
  LOOP:
    - 对每个 type in types:
        - 调用 cmd_latest(config, type) 获取最新值
        - 计算内容指纹 = sha256(json.dumps(observation))
        - 如果指纹与上次不同（或首次）:
            - 原子写入文件: output_dir / f"{type}.json"
              (写入临时文件 → fsync → rename)
            - 打印变更通知到 stdout:
              "[{timestamp}] {type}: {value_summary}"
        - 否则:
            - 跳过（无变更）
    - 等待 interval 秒
    - 检查终止信号（KeyboardInterrupt → 优雅退出）

OUTPUT:
  - 文件: {output_dir}/{type}.json  (每个 type 一个文件)
  - stdout: 变更通知行
  - 退出码: 0 (正常) / 1 (错误) / 130 (Ctrl+C)
```

### 2.5 pull_output.py — 输出格式化

```
INPUT:  命令结果 + 输出目标 (stdout | file)

OPERATIONS:
  - 如果 --output 指定文件路径:
      - 原子写入: 写临时文件 → fsync → rename
      - 格式: JSON (latest/range/weeks) 或 Markdown (archive)
  - 否则:
      - 打印到 stdout
      - JSON 命令: pretty-print JSON (indent=2)
      - Archive 命令: 原样输出 Markdown

OUTPUT: 无返回值（副作用: 写文件或打印 stdout）
```

### 2.6 health_pull.py — CLI 入口

```
INPUT:  argv (命令行参数)

OPERATIONS:
  - 解析命令行参数:
      子命令: latest | range | weeks | archive | watch
      公共参数: --config, --base-url, --token, --timeout, --output, --output-dir
      latest:  [type]
      range:   type --from --to --limit --cursor
      weeks:   (无额外参数)
      archive: week_id
      watch:   types... --interval --output-dir
  - 加载 PullConfig (合并 环境变量 > 配置文件 > 命令行参数)
  - 根据子命令分发到对应的 cmd_* 函数
  - 格式化输出到 stdout 或文件
  - 退出码: 0 (成功) / 1 (配置/认证错误) / 2 (网络错误) / 3 (未找到)

OUTPUT: 退出码 + stdout/文件内容
```

---

## 3. 详细数据流图

### 3.1 latest 命令数据流

```
用户执行:
  python health_pull.py latest heart_rate

数据流:
  argv["latest", "heart_rate"]
    │
    ▼
  health_pull.py (CLI)
    │  type="heart_rate"
    ▼
  pull_config.py
    │  PullConfig {api_base, read_token, ...}
    ▼
  pull_commands.cmd_latest(config, "heart_rate")
    │
    ▼
  pull_transport.py
    │  GET /health/api/v1/latest?type=heart_rate
    │  Authorization: Bearer {token}
    ▼
  VPS API 服务端
    │  200 OK
    │  {"heart_rate": {type, timestamp_utc, timestamp_local, value, ...}}
    ▼
  pull_output.py
    │  pretty-print JSON to stdout
    ▼
  终端输出
```

### 3.2 range 命令数据流

```
用户执行:
  python health_pull.py range heart_rate --from 2026-07-01T00:00:00+08:00 --to 2026-07-08T00:00:00+08:00 --limit 50

数据流:
  argv["range", "heart_rate", "--from", ..., "--to", ..., "--limit", "50"]
    │
    ▼
  health_pull.py (CLI)
    │  type="heart_rate", from_ts=..., to_ts=..., limit=50
    ▼
  pull_commands.cmd_range(config, type, from_ts, to_ts, limit)
    │
    ▼
  pull_transport.py
    │  GET /health/api/v1/data?type=heart_rate&from=...&to=...&limit=50
    │  Authorization: Bearer {token}
    ▼
  VPS API 服务端
    │  200 OK
    │  {"observations": [...], "next_cursor": "eyJ..." | null}
    ▼
  pull_output.py
    │  pretty-print JSON to stdout (or file)
    ▼
  终端输出
```

### 3.3 archive 命令数据流

```
用户执行:
  python health_pull.py archive 2026-W28

数据流:
  argv["archive", "2026-W28"]
    │
    ▼
  health_pull.py (CLI)
    │  week_id="2026-W28"
    ▼
  pull_commands.cmd_archive(config, "2026-W28")
    │
    ▼
  pull_transport.py
    │  GET /health/api/v1/archive/2026-W28
    │  Authorization: Bearer {token}
    ▼
  VPS API 服务端
    │  200 OK
    │  Content-Type: text/markdown
    │  "# Health Archive — 2026-W28\n..."
    ▼
  pull_output.py
    │  原样输出 Markdown 到 stdout (或 --output 指定文件)
    ▼
  终端/文件
```

### 3.4 watch 命令数据流

```
用户执行:
  python health_pull.py watch heart_rate steps --interval 60 --output-dir ./latest

数据流:
  argv["watch", "heart_rate", "steps", "--interval", "60", "--output-dir", "./latest"]
    │
    ▼
  health_pull.py (CLI)
    │  types=["heart_rate", "steps"], interval=60, output_dir=./latest
    ▼
  pull_watch.watch_loop(config, types, interval, output_dir)
    │
    ├──▶ LOOP (每 60 秒):
    │     │
    │     ├──▶ cmd_latest(config, "heart_rate")
    │     │      │  GET /health/api/v1/latest?type=heart_rate
    │     │      ▼
    │     │      Observation {type, value, timestamp_utc, ...}
    │     │      │
    │     │      ├── sha256 → 与上次比较
    │     │      ├── 如果变更 → 原子写入 ./latest/heart_rate.json
    │     │      └── 打印 "[2026-07-11T14:35+08:00] heart_rate: 84 bpm"
    │     │
    │     ├──▶ cmd_latest(config, "steps")
    │     │      │  GET /health/api/v1/latest?type=steps
    │     │      ▼
    │     │      Observation | None
    │     │      │
    │     │      ├── sha256 → 与上次比较
    │     │      ├── 如果变更 → 原子写入 ./latest/steps.json
    │     │      └── 打印 "[2026-07-11T14:40+08:00] steps: 0"
    │     │
    │     └── sleep(60)
    │
    └──▶ Ctrl+C → 退出码 0

OUTPUT FILES:
  ./latest/heart_rate.json  ← {type, value, timestamp_utc, timestamp_local, ...}
  ./latest/steps.json       ← {type, value, ...} 或 {"steps": null}
```

---

## 4. 文件结构

```
client/
  health_pull.py              # CLI 入口
  health_bridge/
    __init__.py
    pull_config.py            # 配置加载
    pull_transport.py         # HTTP 传输层
    pull_commands.py          # 命令处理器 (latest/range/weeks/archive)
    pull_watch.py             # watch 模式轮询
    pull_output.py            # 输出格式化
  tests/
    test_pull_config.py
    test_pull_transport.py
    test_pull_commands.py
    test_pull_watch.py
    test_health_pull_cli.py
  examples/
    pull-config.example.json
```

---

## 5. 环境变量与配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `HEALTH_PULL_BASE_URL` | `https://oh-my-frontweb.duckdns.org` | 服务端基础 URL |
| `HEALTH_READ_TOKEN` | (无，必需) | 读取令牌（Bearer） |
| `HEALTH_PULL_TIMEOUT` | `30` | HTTP 请求超时（秒） |
| `HEALTH_PULL_TIMEZONE` | `Asia/Shanghai` | 本地时区显示 |

优先级：命令行参数 > 配置文件 > 环境变量 > 默认值。

---

## 6. 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 配置错误或认证失败 |
| 2 | 网络错误（连接失败、超时） |
| 3 | 资源未找到（404） |
| 130 | Ctrl+C 中断（watch 模式） |

---

## 7. 安全约束

- 读取令牌永不写入配置文件、日志或错误消息
- 生产环境强制 HTTPS
- 连接工厂可注入，用于测试（允许 HTTP）
- 响应体有界读取，防止内存溢出
- Watch 模式文件写入使用原子操作（临时文件 → rename）
