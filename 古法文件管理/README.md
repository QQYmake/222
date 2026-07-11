# Health-Bridge 使用指南

从 Gadgetbridge 导出的 SQLite 数据库，到远程服务器存储、归档、查询的完整健康数据管线。

## 系统架构

```
┌──────────────┐     gzip + HTTPS      ┌──────────────────────────┐     HTTPS GET      ┌──────────────┐
│  推送客户端   │  ──────────────────▶  │     VPS API 服务端        │  ◀──────────────  │  扉取客户端   │
│ health_push  │   X-Upload-Token      │  FastAPI (127.0.0.1:8765) │   Bearer Token    │ health_pull  │
│ (平板/Termux) │                       │  Nginx 反代 /health/      │                   │ (任意设备)    │
└──────────────┘                       │  SQLite + Markdown 归档   │                    └──────────────┘
                                       └──────────────────────────┘
```

## 文件结构

```
古法文件管理/
├── push-client/                    # 推送客户端（独立模块）
│   ├── health_push.py              # 推送 CLI 入口
│   ├── health_bridge/
│   │   ├── push_config.py          # 配置解析（环境变量 + JSON）
│   │   ├── push_transport.py       # HTTPS 传输层
│   │   ├── push_service.py         # 业务编排（dry-run / once / watch）
│   │   ├── push_snapshot.py        # 快照处理（拷贝 / 校验 / 哈希 / 压缩）
│   │   └── push_state.py           # 状态文件管理
│   ├── examples/
│   │   └── push-config.example.json
│   ├── tests/                      # 145 个测试
│   └── README-push-zh.md
├── pull-client/                    # 拉取客户端（独立模块）
│   ├── health_pull.py              # 拉取 CLI 入口
│   ├── health_bridge/
│   │   ├── pull_config.py          # 配置解析
│   │   ├── pull_transport.py       # HTTPS 传输层
│   │   ├── pull_commands.py        # 命令处理器（latest / range / weeks / archive）
│   │   ├── pull_watch.py           # watch 模式轮询
│   │   └── pull_output.py          # 输出格式化
│   ├── examples/
│   │   └── pull-config.example.json
│   ├── tests/                      # 56 个测试
│   └── README-pull-zh.md
├── server/                         # VPS 服务端（独立模块）
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口（6 端点）
│   │   ├── config.py               # 环境变量配置
│   │   ├── database.py             # SQLite 存储 + 设备/用户映射
│   │   ├── auth.py                 # 令牌认证（常量时间校验）
│   │   ├── ingest.py               # 上传管线（解压→校验→去重→导入→归档）
│   │   ├── schema_inspector.py     # 上传 DB 表结构探测
│   │   ├── normalizer.py           # 时区转换 + 去重键 + ISO 周
│   │   ├── archive.py              # Markdown 归档生成（原子写入）
│   │   ├── latest.py               # latest.json 生成（原子写入）
│   │   └── adapters/               # Gadgetbridge schema 适配器
│   │       ├── base.py
│   │       ├── heart_rate.py       # 心率（XIAOMI_ACTIVITY_SAMPLE）
│   │       ├── steps.py            # 步数双源（逐采样 + 每日汇总）
│   │       └── sleep.py            # 睡眠分期（原始 stage code）
│   ├── tests/                      # 122 个测试
│   ├── nginx-health-bridge.conf    # Nginx 反代配置模板
│   ├── requirements.txt
│   └── README.md
├── docs/
│   ├── server-architecture-plan.md  # 服务端架构计划
│   └── pull-client-architecture-plan.md  # 拉取客户端架构计划
└── README.md                        # 本文件
```

---

## 完整安装指南（面向新手）

三大模块可独立安装，按以下顺序操作：**服务端 → 推送客户端 → 拉取客户端**。

---

### 一、服务端安装指南

服务端运行在 VPS 上，负责接收上传、存储数据、生成归档、提供查询 API。

#### 1.1 前置条件

- 一台 VPS（已安装 Nginx 并配置好 HTTPS）
- Python 3.11 或更高版本
- 已有域名解析到该 VPS

#### 1.2 上传代码

将 `server/` 目录整体上传到 VPS：

```sh
# 在本地执行，将 server/ 上传到 VPS
scp -r server/ user@your-server:/opt/health-bridge/
```

#### 1.3 安装 Python 依赖

```sh
# SSH 登录 VPS
ssh user@your-server

# 进入服务端目录
cd /opt/health-bridge

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

依赖列表（仅 3 个包）：
- `fastapi` — Web 框架
- `uvicorn[standard]` — ASGI 服务器
- `python-multipart` — 文件上传解析

#### 1.4 生成令牌

需要两个令牌：一个用于上传，一个用于读取。使用 `openssl` 生成随机字符串：

```sh
# 生成上传令牌
echo "HEALTH_BRIDGE_UPLOAD_TOKEN=$(openssl rand -hex 32)"

# 生成读取令牌
echo "HEALTH_BRIDGE_READ_TOKEN=$(openssl rand -hex 32)"
```

记下这两个值，后面推送客户端和拉取客户端都要用。

#### 1.5 配置环境变量

```sh
# 创建数据目录
sudo mkdir -p /srv/health-bridge/data /srv/health-bridge/archives /srv/health-bridge/latest
sudo chown -R $(whoami) /srv/health-bridge

# 写入环境变量（创建 .env 文件）
cat > /opt/health-bridge/.env << 'EOF'
HEALTH_BRIDGE_DATA_DIR=/srv/health-bridge/data
HEALTH_BRIDGE_ARCHIVES_DIR=/srv/health-bridge/archives
HEALTH_BRIDGE_LATEST_DIR=/srv/health-bridge/latest
HEALTH_BRIDGE_UPLOAD_TOKEN=把上面生成的上传令牌粘贴到这里
HEALTH_BRIDGE_READ_TOKEN=把上面生成的读取令牌粘贴到这里
HEALTH_BRIDGE_LISTEN_HOST=127.0.0.1
HEALTH_BRIDGE_LISTEN_PORT=8765
EOF

chmod 600 /opt/health-bridge/.env
```

#### 1.6 验证启动

```sh
cd /opt/health-bridge
source .venv/bin/activate
set -a && source .env && set +a

# 启动服务（前台运行，用于验证）
uvicorn app.main:app --host 127.0.0.1 --port 8765

# 另开一个终端，验证服务是否正常
curl http://127.0.0.1:8765/health/api/v1/health
# 应返回：{"status":"ok","version":"1.0.0"}
```

看到 `{"status":"ok"}` 说明服务端启动成功。按 `Ctrl+C` 停止前台进程。

#### 1.7 配置 Nginx 反向代理

编辑 Nginx 配置，在已有的 443 端口 `server { }` 块**内部**添加 `/health/` 路径反代：

```sh
sudo nano /etc/nginx/sites-available/your-site
```

在 `server { }` 块内粘贴以下内容（参考 `nginx-health-bridge.conf`）：

```nginx
# --- Health-Bridge reverse proxy ---
location ^~ /health/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 100m;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
}
# --- End Health-Bridge ---
```

测试并重载 Nginx：

```sh
sudo nginx -t && sudo systemctl reload nginx
```

通过域名验证：

```sh
curl https://your-server.com/health/api/v1/health
# 应返回：{"status":"ok","version":"1.0.0"}
```

#### 1.8 配置 systemd 守护进程（生产环境）

```sh
sudo cat > /etc/systemd/system/health-bridge.service << 'EOF'
[Unit]
Description=Health-Bridge API Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/health-bridge
EnvironmentFile=/opt/health-bridge/.env
ExecStart=/opt/health-bridge/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable health-bridge
sudo systemctl start health-bridge

# 确认运行状态
sudo systemctl status health-bridge
```

服务端安装完成。

---

### 二、推送客户端安装指南

推送客户端运行在平板/手机上（通过 Termux），监视 Gadgetbridge 导出的 SQLite 数据库并上传到服务端。

#### 2.1 前置条件

- Android 平板/手机，已安装 Termux
- 已安装 Gadgetbridge 并导出过健康数据库
- 服务端已部署完成（需要上传令牌和服务端域名）

#### 2.2 安装 Termux 和 Python

```sh
# 从 F-Droid 安装 Termux（不要用 Google Play 版，已过时）
# 打开 Termux 后执行：

pkg update && pkg upgrade -y
pkg install python tmux -y
```

#### 2.3 授予存储权限

```sh
termux-setup-storage
```

弹出权限对话框时点击「允许」。这会创建 `~/storage` 符号链接，用于访问共享存储。

#### 2.4 上传代码

将 `push-client/` 目录上传到 Termux：

```sh
# 在本地执行
scp -r push-client/ tablet:~/push-client/
```

或者在 Termux 中用 git 拉取（如果项目在 git 仓库中）。

#### 2.5 配置环境变量

```sh
# 编辑 Termux 启动脚本，使环境变量持久化
nano ~/.bashrc
```

在文件末尾添加：

```sh
# Health-Bridge 推送配置
export HEALTH_UPLOAD_BASE_URL='https://your-server.com'
export HEALTH_UPLOAD_TOKEN='把服务端生成的上传令牌粘贴到这里'
```

保存后重新加载：

```sh
source ~/.bashrc
```

#### 2.6 确认 Gadgetbridge 数据库路径

Gadgetbridge 导出数据库的默认路径：

```sh
# 检查文件是否存在
ls -la ~/storage/downloads/health/Gadgetbridge.db
```

如果你的导出路径不同，后续命令中用 `--source` 指定实际路径。

#### 2.7 验证安装（dry-run 模式）

```sh
cd ~/push-client

# dry-run 不联系服务器，只验证源数据库可读、完整性正常
python health_push.py dry-run --source ~/storage/downloads/health/Gadgetbridge.db
```

看到类似以下输出说明安装正确：

```
[INFO] source: /storage/emulated/0/Download/health/Gadgetbridge.db (835584 bytes)
[INFO] stability check passed
[INFO] SQLite integrity: ok
[INFO] sha256: bb7329b9fc3d...
[INFO] state saved
[OK] dry-run complete
```

#### 2.8 首次上传（once 模式）

```sh
python health_push.py once --source ~/storage/downloads/health/Gadgetbridge.db
```

看到 `[OK] uploaded` 说明上传成功。

#### 2.9 长期监听（watch 模式）

建议在 tmux 会话中运行 watch 模式，断开 SSH 后不会被杀死：

```sh
# 启动 tmux 会话
tmux new -s health-push

# 在 tmux 内运行 watch 模式（默认每 15 分钟检查一次）
cd ~/push-client
python health_push.py watch --source ~/storage/downloads/health/Gadgetbridge.db

# 按 Ctrl+B 然后按 D 脱离 tmux（进程继续运行）
# 重新进入：tmux attach -s health-push
```

#### 2.10 电池优化豁免

Android 会杀后台进程。进入系统设置 → 电池 → 找到 Termux → 选择「不优化」或「无限制」。

推送客户端安装完成。详细配置参考 [push-client/README-push-zh.md](push-client/README-push-zh.md)。

---

### 三、拉取客户端安装指南

拉取客户端可在任意设备上运行（电脑、平板、手机），从服务端查询健康数据。

#### 3.1 前置条件

- Python 3.11 或更高版本
- 服务端已部署完成（需要读取令牌和服务端域名）

#### 3.2 上传代码

将 `pull-client/` 目录复制到目标设备：

```sh
# 在本地执行
cp -r pull-client/ ~/health-pull/
```

#### 3.3 安装 Python

**Linux / macOS：**

```sh
# 大多数系统已预装 Python，检查版本
python3 --version
# 需要 3.11 或更高版本
```

**Windows：**

从 https://python.org 下载并安装 Python 3.11+。安装时勾选「Add Python to PATH」。

拉取客户端**零第三方依赖**，不需要 `pip install`。

#### 3.4 配置环境变量

**Linux / macOS：**

```sh
# 添加到 ~/.bashrc 或 ~/.zshrc
echo 'export HEALTH_PULL_BASE_URL="https://your-server.com"' >> ~/.bashrc
echo 'export HEALTH_READ_TOKEN="把服务端生成的读取令牌粘贴到这里"' >> ~/.bashrc
source ~/.bashrc
```

**Windows (PowerShell)：**

```powershell
# 设置用户级环境变量
[Environment]::SetEnvironmentVariable("HEALTH_PULL_BASE_URL", "https://your-server.com", "User")
[Environment]::SetEnvironmentVariable("HEALTH_READ_TOKEN", "把读取令牌粘贴到这里", "User")

# 重启 PowerShell 使环境变量生效
```

#### 3.5 验证安装

```sh
cd ~/health-pull

# 查看可用归档周列表
python health_pull.py weeks

# 应返回类似：
# {
#   "weeks": ["2026-W28"]
# }
```

看到 JSON 输出说明安装正确。

#### 3.6 日常使用命令

```sh
# 查看所有类型的最新值
python health_pull.py latest

# 只看最新心率
python health_pull.py latest heart_rate

# 查询心率历史（最近 100 条）
python health_pull.py range heart_rate --limit 100

# 按时间范围查询
python health_pull.py range heart_rate \
  --from 2026-07-01T00:00:00+08:00 \
  --to 2026-07-08T00:00:00+08:00

# 下载周归档（Markdown 格式）
python health_pull.py archive 2026-W28

# 长期监听模式（每 60 秒轮询，写入 JSON 文件）
python health_pull.py watch heart_rate steps_daily \
  --interval 60 \
  --output-dir ./latest
```

#### 3.7 输出到文件

所有命令都支持 `--output` 参数将结果写入文件：

```sh
python health_pull.py weeks --output weeks.json
python health_pull.py archive 2026-W28 --output week-28.md
```

拉取客户端安装完成。详细配置参考 [pull-client/README-pull-zh.md](pull-client/README-pull-zh.md)。

---

## 环境变量汇总

### 推送客户端 (`push-client/`)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_UPLOAD_BASE_URL` | `https://oh-my-frontweb.duckdns.org` | 服务器地址 |
| `HEALTH_UPLOAD_TOKEN` | (必需) | 上传令牌 |

### 服务端 (`server/`)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_BRIDGE_DATA_DIR` | `/srv/health-bridge/data` | 数据目录（数据库 + 原始快照） |
| `HEALTH_BRIDGE_ARCHIVES_DIR` | `/srv/health-bridge/archives` | Markdown 归档目录 |
| `HEALTH_BRIDGE_LATEST_DIR` | `/srv/health-bridge/latest` | latest.json 目录 |
| `HEALTH_BRIDGE_UPLOAD_TOKEN` | (必需) | 上传令牌 |
| `HEALTH_BRIDGE_READ_TOKEN` | (必需) | 读取令牌 |
| `HEALTH_BRIDGE_LISTEN_HOST` | `127.0.0.1` | 监听地址 |
| `HEALTH_BRIDGE_LISTEN_PORT` | `8765` | 监听端口 |

### 拉取客户端 (`pull-client/`)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_PULL_BASE_URL` | `https://oh-my-frontweb.duckdns.org` | 服务器地址 |
| `HEALTH_READ_TOKEN` | (必需) | 读取令牌 |
| `HEALTH_PULL_TIMEOUT` | `30` | 请求超时（秒） |
| `HEALTH_PULL_TIMEZONE` | `Asia/Shanghai` | 时区 |

---

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

---

## 测试

三大模块可独立测试：

```sh
# 服务端测试（122 个）
cd server/ && python3 -m pytest tests/ -v

# 推送客户端测试（145 个）
cd push-client/ && python3 -m pytest tests/ -v

# 拉取客户端测试（56 个）
cd pull-client/ && python3 -m pytest tests/ -v
```

## 安全约束

- 上传和读取使用分离的令牌，拉取客户端无法修改数据
- 令牌永不写入配置文件、日志或错误消息
- 生产环境强制 HTTPS（拉取客户端可用 `--insecure` 调试）
- 上传数据经过 SQLite 完整性校验和 SHA-256 去重
- 归档文件和 latest.json 使用原子写入（临时文件 → rename）
- 响应体有界读取，防止内存溢出
- Nginx 限制上传体积 100MB
