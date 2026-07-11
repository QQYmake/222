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

### 1. 推送客户端 (`push-client/`)

运行在平板/终端侧，监视 Gadgetbridge 导出的 SQLite 数据库，gzip 压缩后通过 HTTPS 上传到服务器。

- **3 种模式：** `dry-run`（验证）、`once`（单次上传）、`watch`（长期监听）
- **安全：** SHA-256 去重、SQLite 完整性校验、令牌认证、指数退避重试
- **零依赖：** 纯 Python 标准库

[详细文档](push-client/README.md)

### 2. VPS 服务端 (`server/`)

FastAPI 服务，接收上传、适配 Gadgetbridge schema、归一化存储、生成 Markdown 归档。

- **6 个 API 端点：** health / upload / latest / data / weeks / archive
- **Schema 适配器：** 心率、步数（双源）、睡眠分期
- **归档生成器：** 按 ISO 周生成 Markdown，原子写入
- **认证：** 上传/读取令牌分离，常量时间校验

[详细文档](server/README.md) | [架构计划](docs/server-architecture-plan.md)

### 3. 拉取客户端 (`pull-client/`)

从服务端查询健康数据，支持单次查询和长期监听模式。

- **5 个子命令：** latest / range / weeks / archive / watch
- **Watch 模式：** 定时轮询、原子文件写入、变更通知
- **零依赖：** 纯 Python 标准库

[详细文档](pull-client/README.md) | [架构计划](docs/pull-client-architecture-plan.md)

---

## 完整安装指南

以下指南面向新手，每个组件独立安装，不跳过任何步骤。

### 一、服务端安装（VPS 上操作）

> **前提条件：** 一台有公网 IP 的 Linux VPS，已安装 Nginx，拥有 root 或 sudo 权限。

#### 步骤 1：安装 Python 3.11+

```sh
# Ubuntu / Debian
sudo apt update
sudo apt install python3 python3-pip python3-venv -y

# 验证版本
python3 --version
# 应输出 Python 3.11 或更高
```

#### 步骤 2：上传项目文件到 VPS

```sh
# 方式一：用 scp 从本地传输（在你的电脑上执行）
scp -r server/ root@your-vps-ip:/opt/health-bridge/

# 方式二：在 VPS 上用 git clone（如果项目在 Git 仓库）
git clone <your-repo> /opt/health-bridge/
cd /opt/health-bridge/server/
```

#### 步骤 3：创建虚拟环境并安装依赖

```sh
cd /opt/health-bridge/server/

# 创建虚拟环境
python3 -m venv venv

# 激活虚拟环境
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

#### 步骤 4：生成令牌

```sh
# 生成上传令牌（用于推送客户端）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 输出示例: aB3xK9mN2pQ7rS_tUvWxYz0a1b2c3d4e5f6g7h8i9j

# 生成读取令牌（用于拉取客户端）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 输出示例: kL6mN8pQ2rS4tU_vWxYz1a2b3c4d5e6f7g8h9i0j1k

# 把这两个令牌记下来，后面推送和拉取客户端都要用
```

#### 步骤 5：创建数据目录

```sh
sudo mkdir -p /srv/health-bridge/data/raw
sudo mkdir -p /srv/health-bridge/data/incoming
sudo mkdir -p /srv/health-bridge/data/archives
sudo mkdir -p /srv/health-bridge/data/latest

# 将所有者改为运行服务的用户（假设是 www-data）
sudo chown -R www-data:www-data /srv/health-bridge/
```

#### 步骤 6：配置环境变量

```sh
# 创建环境变量文件
sudo tee /opt/health-bridge/server/.env << 'EOF'
HEALTH_BRIDGE_DATA_DIR=/srv/health-bridge/data
HEALTH_BRIDGE_UPLOAD_TOKEN=你刚才生成的上传令牌
HEALTH_BRIDGE_READ_TOKEN=你刚才生成的读取令牌
HEALTH_BRIDGE_LISTEN_HOST=127.0.0.1
HEALTH_BRIDGE_LISTEN_PORT=8765
EOF

# 保护文件权限
sudo chmod 600 /opt/health-bridge/server/.env
sudo chown www-data:www-data /opt/health-bridge/server/.env
```

#### 步骤 7：配置 Nginx 反向代理

```sh
# 复制配置模板
sudo cp /opt/health-bridge/server/nginx-health-bridge.conf /etc/nginx/sites-available/health-bridge

# 编辑配置，将 server_name 改为你的域名
sudo nano /etc/nginx/sites-available/health-bridge
# 把 server_name your-domain.com; 改为你的实际域名

# 创建符号链接启用站点
sudo ln -s /etc/nginx/sites-available/health-bridge /etc/nginx/sites-enabled/

# 测试 Nginx 配置
sudo nginx -t

# 重载 Nginx
sudo systemctl reload nginx
```

#### 步骤 8：配置 HTTPS（用 Let's Encrypt）

```sh
# 安装 certbot
sudo apt install certbot python3-certbot-nginx -y

# 自动获取并配置 SSL 证书
sudo certbot --nginx -d your-domain.com

# 按提示输入邮箱，同意条款，选择重定向 HTTP 到 HTTPS
```

#### 步骤 9：创建 systemd 服务（让服务端开机自启）

```sh
sudo tee /etc/systemd/system/health-bridge.service << 'EOF'
[Unit]
Description=Health-Bridge API Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/health-bridge/server
EnvironmentFile=/opt/health-bridge/server/.env
ExecStart=/opt/health-bridge/server/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable health-bridge
sudo systemctl start health-bridge

# 查看运行状态
sudo systemctl status health-bridge
# 应显示 active (running)
```

#### 步骤 10：验证服务端

```sh
# 本机验证
curl http://127.0.0.1:8765/health/api/v1/health
# 应返回: {"status":"ok","version":"1.0.0"}

# 外网验证（将 your-domain.com 替换为你的域名）
curl https://your-domain.com/health/api/v1/health
# 应返回相同结果

# 如果失败，检查：
# 1. systemctl status health-bridge  — 服务是否在运行
# 2. journalctl -u health-bridge -n 50  — 查看服务日志
# 3. sudo nginx -t  — Nginx 配置是否正确
```

---

### 二、推送客户端安装（平板/Termux 上操作）

> **前提条件：** Android 平板已安装 Gadgetbridge 和 Termux，Gadgetbridge 已配置导出数据库到共享存储。

#### 步骤 1：安装 Termux 和 Python

```sh
# 从 F-Droid 安装 Termux（不要用 Google Play 版，已过时）
# 打开 Termux 后执行：

# 更新包管理器
pkg update && pkg upgrade -y

# 安装 Python
pkg install python -y

# 验证版本
python --version
# 应输出 Python 3.11 或更高
```

#### 步骤 2：授予 Termux 存储权限

```sh
# 这一步必须在 Termux 中手动执行
termux-setup-storage

# 执行后手机上会弹出权限请求，点击"允许"
# 验证：以下目录应该存在
ls ~/storage/downloads/
# 如果能看到下载目录内容，说明权限已获取
```

#### 步骤 3：复制项目文件

```sh
# 在 Termux 中创建工作目录
mkdir -p ~/health-bridge

# 方式一：用 git clone
git clone <your-repo> ~/health-bridge/repo
cp -r ~/health-bridge/repo/push-client/ ~/health-bridge/

# 方式二：用 scp 从电脑传输
# 在你的电脑上执行：
# scp -r push-client/ tablet@tablet-ip:~/health-bridge/

# 验证文件存在
ls ~/health-bridge/push-client/
# 应看到 health_push.py  health_bridge/  tests/  examples/
```

#### 步骤 4：配置环境变量

```sh
# 上传令牌（与服务端步骤 4 生成的上传令牌一致）
echo 'export HEALTH_UPLOAD_TOKEN="你刚才生成的上传令牌"' >> ~/.bashrc

# 服务器地址（将 your-domain.com 改为你的域名）
echo 'export HEALTH_UPLOAD_BASE_URL="https://your-domain.com"' >> ~/.bashrc

# 立即生效
source ~/.bashrc

# 验证
echo $HEALTH_UPLOAD_TOKEN
echo $HEALTH_UPLOAD_BASE_URL
# 应分别显示令牌和域名
```

#### 步骤 5：确认 Gadgetbridge 导出文件位置

```sh
# Gadgetbridge 默认导出路径（取决于你的配置）
ls ~/storage/downloads/health/Gadgetbridge.db

# 如果文件不在这里，在存储中搜索
find ~/storage/ -name "Gadgetbridge.db" 2>/dev/null

# 记下找到的完整路径，后续步骤会用到
# 示例路径: ~/storage/downloads/health/Gadgetbridge.db
```

#### 步骤 6：dry-run 验证

```sh
cd ~/health-bridge/push-client/

# 验证配置和源数据库（不会上传任何数据）
python health_push.py dry-run \
  --source ~/storage/downloads/health/Gadgetbridge.db

# 预期输出包含:
# [INFO] source database is stable
# [INFO] SQLite integrity check passed
# [INFO] snapshot sha256 = ...
# [INFO] dry-run complete
# 退出码 0 表示成功

# 如果报错：
# "source not found" → 检查步骤 5 的路径
# "token not set" → 检查步骤 4 的环境变量
# "integrity check failed" → Gadgetbridge 导出可能未完成，等几秒重试
```

#### 步骤 7：首次上传

```sh
cd ~/health-bridge/push-client/

# 单次上传
python health_push.py once \
  --source ~/storage/downloads/health/Gadgetbridge.db

# 预期输出:
# [INFO] snapshot sha256 = ...
# [INFO] uploading to https://your-domain.com/health/api/v1/upload
# [INFO] upload accepted: 9 new observations, 0 duplicates
# 退出码 0 表示成功

# 在服务端验证（在 VPS 上执行）:
curl -H "Authorization: Bearer 你的读取令牌" \
  https://your-domain.com/health/api/v1/latest
# 应返回最新的心率和步数数据
```

#### 步骤 8：配置长期监听（可选）

```sh
# 安装 tmux（用于后台运行）
pkg install tmux -y

# 启动 tmux 会话
tmux new -s health-push

cd ~/health-bridge/push-client/

# 每 15 分钟检查一次，有变化才上传
python health_push.py watch \
  --source ~/storage/downloads/health/Gadgetbridge.db

# 按 Ctrl+B 然后按 D 退出 tmux（进程继续在后台运行）
# 重新进入: tmux attach -t health-push

# 关闭 Termux 电池优化：
# 设置 → 应用 → Termux → 电池 → 不限制后台运行
```

---

### 三、拉取客户端安装（任意设备上操作）

> **前提条件：** 任意装有 Python 3.11+ 的设备（电脑、手机、服务器），能访问服务端域名。

#### 步骤 1：安装 Python 3.11+

```sh
# Linux (Ubuntu/Debian)
sudo apt update && sudo apt install python3 -y

# macOS (用 Homebrew)
brew install python

# Windows: 从 https://python.org 下载安装，勾选 "Add to PATH"

# 验证
python3 --version   # Linux/macOS
python --version    # Windows
```

#### 步骤 2：复制项目文件

```sh
# 创建工作目录
mkdir -p ~/health-bridge

# 方式一：git clone
git clone <your-repo> ~/health-bridge/repo
cp -r ~/health-bridge/repo/pull-client/ ~/health-bridge/

# 方式二：scp 从其他设备传输
# scp -r pull-client/ your-device:~/health-bridge/

# 验证
ls ~/health-bridge/pull-client/
# 应看到 health_pull.py  health_bridge/  tests/  examples/
```

#### 步骤 3：配置环境变量

```sh
# Linux / macOS
# 读取令牌（与服务端步骤 4 生成的读取令牌一致）
echo 'export HEALTH_READ_TOKEN="你刚才生成的读取令牌"' >> ~/.bashrc

# 服务器地址
echo 'export HEALTH_PULL_BASE_URL="https://your-domain.com"' >> ~/.bashrc

# 立即生效
source ~/.bashrc
```

```powershell
# Windows (PowerShell)
$env:HEALTH_READ_TOKEN = "你刚才生成的读取令牌"
$env:HEALTH_PULL_BASE_URL = "https://your-domain.com"

# 永久设置（管理员 PowerShell）
[Environment]::SetEnvironmentVariable("HEALTH_READ_TOKEN", "你的读取令牌", "User")
[Environment]::SetEnvironmentVariable("HEALTH_PULL_BASE_URL", "https://your-domain.com", "User")
```

#### 步骤 4：验证连通性

```sh
cd ~/health-bridge/pull-client/

# 查看可用周归档
python health_pull.py weeks

# 预期输出:
# Available weeks:
#   2026-W28
# 退出码 0 表示成功

# 如果报错：
# "authentication failed" → 检查读取令牌是否正确
# "connection refused" → 检查服务器地址是否正确
# "not found" → 服务端可能还没有数据，先完成推送客户端安装
```

#### 步骤 5：查询数据

```sh
cd ~/health-bridge/pull-client/

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

# 保存输出到文件
python health_pull.py weeks --output weeks.json
```

#### 步骤 6：配置长期监听（可选）

```sh
cd ~/health-bridge/pull-client/

# 每 60 秒轮询一次心率和每日步数，有变化时写入文件并打印通知
python health_pull.py watch heart_rate steps_daily \
  --interval 60 \
  --output-dir ./latest

# 按 Ctrl+C 停止

# 输出目录中会生成:
# ./latest/heart_rate.json   — 最新心率
# ./latest/steps_daily.json  — 最新每日步数
# 文件使用原子写入，可以安全地被其他程序读取
```

---

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
# 推送客户端测试（145 个）
cd push-client/ && python3 -m pytest tests/ -v

# 拉取客户端测试（56 个）
cd pull-client/ && python3 -m pytest tests/ -v

# 服务端测试（122 个）
cd server/ && python3 -m pytest tests/ -v
```

## 文件结构

```
古法文件管理/
├── push-client/                  # 推送客户端（独立）
│   ├── health_push.py            # CLI 入口
│   ├── health_bridge/
│   │   ├── push_config.py        # 配置管理
│   │   ├── push_transport.py     # HTTPS 传输
│   │   ├── push_service.py       # 业务编排
│   │   ├── push_snapshot.py      # 快照处理
│   │   └── push_state.py         # 状态文件
│   ├── tests/                    # 145 个测试
│   ├── examples/
│   │   └── push-config.example.json
│   └── README.md
├── pull-client/                  # 拉取客户端（独立）
│   ├── health_pull.py            # CLI 入口
│   ├── health_bridge/
│   │   ├── pull_config.py        # 配置管理
│   │   ├── pull_transport.py     # HTTPS 传输
│   │   ├── pull_commands.py      # 命令处理器
│   │   ├── pull_watch.py         # watch 模式
│   │   └── pull_output.py        # 输出格式化
│   ├── tests/                    # 56 个测试
│   ├── examples/
│   │   └── pull-config.example.json
│   └── README.md
├── server/                       # VPS 服务端（独立）
│   ├── app/
│   │   ├── main.py               # FastAPI 入口
│   │   ├── config.py             # 配置
│   │   ├── database.py           # SQLite 存储
│   │   ├── auth.py               # 认证
│   │   ├── ingest.py             # 上传管线
│   │   ├── schema_inspector.py   # Schema 探测
│   │   ├── normalizer.py         # 归一化
│   │   ├── archive.py            # 归档生成
│   │   ├── latest.py             # 最新值生成
│   │   └── adapters/             # Schema 适配器
│   │       ├── heart_rate.py
│   │       ├── steps.py
│   │       └── sleep.py
│   ├── tests/                    # 122 个测试
│   ├── nginx-health-bridge.conf
│   ├── requirements.txt
│   └── README.md
├── docs/
│   ├── server-architecture-plan.md
│   └── pull-client-architecture-plan.md
└── README.md                     # 本文件
```
