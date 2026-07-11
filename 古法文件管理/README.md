# Health-Bridge 使用指南

从 Gadgetbridge 导出的 SQLite 数据库，到远程服务器存储、归档、查询的完整健康数据管线。

## 系统架构

```
┌──────────────┐     gzip + HTTPS      ┌──────────────────────────┐     HTTPS GET      ┌──────────────┐
│  推送客户端   │  ──────────────────▶  │     VPS API 服务端        │  ◀──────────────  │  拉取客户端   │
│ health_push  │   X-Upload-Token      │  FastAPI (127.0.0.1:8765) │   Bearer Token    │ health_pull  │
│ (平板/Termux) │                       │  Nginx 反代 /health/      │                   │ (任意设备)    │
└──────────────┘                       │  SQLite + Markdown 归档   │                    └──────────────┘
                                       └──────────────────────────┘
```

## 三大组件

### 1. 推送客户端 (`client/health_push.py`)

运行在平板/终端侧，监视 Gadgetbridge 导出的 SQLite 数据库，gzip 压缩后通过 HTTPS 上传到服务器。

- **3 种模式：** `dry-run`（验证）、`once`（单次上传）、`watch`（长期监听）
- **安全：** SHA-256 去重、SQLite 完整性校验、令牌认证、指数退避重试
- **零依赖：** 纯 Python 标准库

[详细文档](client/README-push-zh.md)

### 2. VPS 服务端 (`server/`)

FastAPI 服务，接收上传、适配 Gadgetbridge schema、归一化存储、生成 Markdown 归档。

- **6 个 API 端点：** health / upload / latest / data / weeks / archive
- **Schema 适配器：** 心率、步数（双源）、睡眠分期
- **归档生成器：** 按 ISO 周生成 Markdown，原子写入
- **认证：** 上传/读取令牌分离，常量时间校验

[架构计划](docs/server-architecture-plan.md) | [拉取客户端架构](docs/pull-client-architecture-plan.md)

### 3. 拉取客户端 (`client/health_pull.py`)

从服务端查询健康数据，支持单次查询和长期监听模式。

- **5 个子命令：** latest / range / weeks / archive / watch
- **Watch 模式：** 定时轮询、原子文件写入、变更通知
- **零依赖：** 纯 Python 标准库

[详细文档](client/README-pull-zh.md)

## 完整使用流程

### 第一步：部署服务端

```sh
# 在 VPS 上
cd server/
pip install -r requirements.txt

# 配置环境变量
export HEALTH_BRIDGE_DATA_DIR=/srv/health-bridge/data
export HEALTH_BRIDGE_UPLOAD_TOKEN='your-upload-token'
export HEALTH_BRIDGE_READ_TOKEN='your-read-token'

# 启动
uvicorn app.main:app --host 127.0.0.1 --port 8765

# 配置 Nginx 反代（参考 nginx-health-bridge.conf）
# 验证
curl https://your-server.com/health/api/v1/health
# {"status": "ok", "version": "1.0.0"}
```

### 第二步：配置推送客户端

```sh
# 在平板/Termux 上
export HEALTH_UPLOAD_BASE_URL='https://your-server.com'
export HEALTH_UPLOAD_TOKEN='your-upload-token'

# 验证配置和源数据库
python health_push.py dry-run --source ~/storage/downloads/health/Gadgetbridge.db

# 单次上传
python health_push.py once --source ~/storage/downloads/health/Gadgetbridge.db

# 长期监听（每 15 分钟）
python health_push.py watch --source ~/storage/downloads/health/Gadgetbridge.db
```

### 第三步：拉取数据

```sh
# 在任意设备上
export HEALTH_PULL_BASE_URL='https://your-server.com'
export HEALTH_READ_TOKEN='your-read-token'

# 查看最新心率
python health_pull.py latest heart_rate

# 查询一周心率历史
python health_pull.py range heart_rate \
  --from 2026-07-01T00:00:00+08:00 \
  --to 2026-07-08T00:00:00+08:00

# 下载周归档（Markdown）
python health_pull.py archive 2026-W28

# 长期监听模式
python health_pull.py watch heart_rate steps_daily \
  --interval 60 \
  --output-dir ./latest
```

## 环境变量汇总

### 推送客户端

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_UPLOAD_BASE_URL` | `https://oh-my-frontweb.duckdns.org` | 服务器地址 |
| `HEALTH_UPLOAD_TOKEN` | (必需) | 上传令牌 |

### 服务端

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_BRIDGE_DATA_DIR` | `/srv/health-bridge/data` | 数据目录 |
| `HEALTH_BRIDGE_UPLOAD_TOKEN` | (必需) | 上传令牌 |
| `HEALTH_BRIDGE_READ_TOKEN` | (必需) | 读取令牌 |
| `HEALTH_BRIDGE_LISTEN_HOST` | `127.0.0.1` | 监听地址 |
| `HEALTH_BRIDGE_LISTEN_PORT` | `8765` | 监听端口 |

### 拉取客户端

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_PULL_BASE_URL` | `https://oh-my-frontweb.duckdns.org` | 服务器地址 |
| `HEALTH_READ_TOKEN` | (必需) | 读取令牌 |
| `HEALTH_PULL_TIMEOUT` | `30` | 请求超时（秒） |
| `HEALTH_PULL_TIMEZONE` | `Asia/Shanghai` | 时区 |

## API 端点

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/health/api/v1/health` | 无 | 存活检查 |
| POST | `/health/api/v1/upload` | Upload Token | 上传快照 |
| GET | `/health/api/v1/latest?type=` | Read Token | 最新值 |
| GET | `/health/api/v1/data?type=&from=&to=&limit=&cursor=` | Read Token | 范围查询 |
| GET | `/health/api/v1/weeks` | Read Token | 归档周列表 |
| GET | `/health/api/v1/archive/{week}` | Read Token | 周归档 Markdown |

## 数据类型

| 类型 | 来源表 | 说明 |
|------|--------|------|
| `heart_rate` | XIAOMI_ACTIVITY_SAMPLE | 心率 (bpm) |
| `steps` | XIAOMI_ACTIVITY_SAMPLE | 逐采样步数 |
| `steps_daily` | XIAOMI_DAILY_SUMMARY_SAMPLE | 每日步数汇总 |
| `sleep_stage` | XIAOMI_SLEEP_STAGE_SAMPLE | 睡眠分期（原始代码） |

## 安全约束

- 上传和读取使用分离的令牌，拉取客户端无法修改数据
- 令牌永不写入配置文件、日志或错误消息
- 生产环境强制 HTTPS
- 上传数据经过 SQLite 完整性校验和 SHA-256 去重
- 归档文件和 latest.json 使用原子写入（临时文件 → rename）
- 响应体有界读取，防止内存溢出

## 测试

```sh
# 服务端测试（122 个）
cd server/ && python3 -m pytest tests/ -v

# 客户端测试（201 个，含推送 + 拉取）
cd client/ && python3 -m pytest tests/ -v

# 端到端验证
# 1. 启动服务端
# 2. 上传真实 Gadgetbridge.db
# 3. 拉取 latest / range / weeks / archive
# 4. 运行 watch 模式
```

## 文件结构

```
古法文件管理/
├── client/                    # 推送 + 拉取客户端
│   ├── health_push.py         # 推送 CLI
│   ├── health_pull.py         # 拉取 CLI
│   ├── health_bridge/
│   │   ├── push_config.py     # 推送配置
│   │   ├── push_transport.py  # 推送传输
│   │   ├── push_service.py    # 推送服务
│   │   ├── push_snapshot.py   # 快照处理
│   │   ├── push_state.py      # 状态管理
│   │   ├── pull_config.py     # 拉取配置
│   │   ├── pull_transport.py  # 拉取传输
│   │   ├── pull_commands.py   # 拉取命令
│   │   ├── pull_watch.py      # 拉取监听
│   │   └── pull_output.py     # 拉取输出
│   ├── tests/                 # 201 个测试
│   ├── examples/
│   │   ├── push-config.example.json
│   │   └── pull-config.example.json
│   ├── README-push-zh.md
│   └── README-pull-zh.md
├── server/                    # VPS 服务端
│   ├── app/
│   │   ├── main.py            # FastAPI 入口
│   │   ├── config.py          # 配置
│   │   ├── database.py        # SQLite 存储
│   │   ├── auth.py            # 认证
│   │   ├── ingest.py          # 上传管线
│   │   ├── schema_inspector.py # Schema 探测
│   │   ├── normalizer.py      # 归一化
│   │   ├── archive.py         # 归档生成
│   │   ├── latest.py          # 最新值生成
│   │   └── adapters/          # Schema 适配器
│   │       ├── heart_rate.py
│   │       ├── steps.py
│   │       └── sleep.py
│   ├── tests/                 # 122 个测试
│   ├── nginx-health-bridge.conf
│   ├── requirements.txt
│   └── README.md
├── docs/
│   ├── server-architecture-plan.md
│   └── pull-client-architecture-plan.md
└── health-bridge-design.md    # 总体架构设计
```
