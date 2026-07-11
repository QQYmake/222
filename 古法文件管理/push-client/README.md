---
# Health-Bridge 推送客户端 — 用户文档

推送客户端将一个经过 gzip 压缩的 SQLite 健康数据快照上传到远程 HTTPS 端点。它支持一次性上传、干运行验证，以及带有自动重试的长期运行监听模式。

## 前提条件

- Python **3.11** 或更高版本
- 对由你的健康追踪应用生成的 SQLite 数据库文件拥有读取权限（默认路径：Android 上的 Gadgetbridge 导出文件）
- 由服务器运营方签发的上传令牌

## 服务器地址配置

推送客户端默认将数据上传到 `https://oh-my-frontweb.duckdns.org/health/api/v1/upload`。你可以通过以下方式更改目标服务器：

### 环境变量（推荐）

设置 `HEALTH_UPLOAD_BASE_URL` 环境变量即可覆盖目标域名，上传路径 `/health/api/v1/upload` 会自动拼接：

```sh
export HEALTH_UPLOAD_BASE_URL='https://your-server.example.com'
```

### 配置文件

在 JSON 配置文件中设置 `upload_base_url` 字段：

```json
{
  "upload_base_url": "https://your-server.example.com"
}
```

### 优先级

从高到低：

1. 配置文件中显式设置 `upload_url`（完整 URL，完全覆盖）
2. `HEALTH_UPLOAD_BASE_URL` 环境变量
3. 配置文件中的 `upload_base_url` 字段
4. 内置默认值 `https://oh-my-frontweb.duckdns.org`

如果使用了 `upload_base_url` 或环境变量，最终上传 URL 为 `<base_url>/health/api/v1/upload`。base URL 末尾的斜杠会自动去除。

---

## Termux 配置（Android）

### 1. 授予存储访问权限

在运行以下命令之前，Termux 无法读取共享存储：

```sh
termux-setup-storage
```

此命令会创建一个指向 `/storage/emulated/0` 的 `~/storage` 符号链接树。源数据库必须位于 Termux 可读取的路径下（例如 `~/storage/downloads/health/Gadgetbridge.db`）。

### 2. 安装 Python 并验证版本

```sh
pkg update && pkg install python
python3 --version
```

输出必须显示 **3.11** 或更高版本。较旧的 Termux 软件源可能提供 Python 3.10 或更早版本；如果版本过旧，请升级你的 `pkg` 软件目录。

### 3. 将客户端文件复制到 Termux 私有目录

将客户端代码放在 Termux 的私有主目录下，以确保其他应用无法读取：

```sh
mkdir -p ~/health-bridge
cp -r clients/ ~/health-bridge/
```

验证目录结构：

```
~/health-bridge/clients/
├── health_push.py          # CLI 入口
├── health_bridge/
│   ├── push_config.py
│   ├── push_service.py
│   ├── push_snapshot.py
│   ├── push_state.py
│   └── push_transport.py
└── examples/
    └── push-config.example.json
```

后续所有命令均假定你的工作目录为 `clients/` 的父目录。

---

## 令牌管理

上传令牌通过 `X-Upload-Token` HTTP 头部对每个请求进行身份验证。令牌永远不会被写入配置文件、状态文件或日志中。

### 方式一 — 环境变量（推荐）

在运行客户端之前，将令牌设置为环境变量：

```sh
# Bash / Zsh — 前面加一个空格，在大多数系统上可避免记录到 shell 历史
 export HEALTH_UPLOAD_TOKEN='your-token-here'
```

环境变量名称默认为 `HEALTH_UPLOAD_TOKEN`，可通过配置文件中的 `token_env` 字段进行修改。

为使该变量持久化且不以明文写入 `.bashrc`，可以考虑使用一个在登录时加载的受限可读文件：

```sh
# 创建一个仅你可读的文件
touch ~/.health_token && chmod 600 ~/.health_token
echo 'export HEALTH_UPLOAD_TOKEN=your-token-here' >> ~/.health_token
echo 'source ~/.health_token' >> ~/.bashrc
```

### 方式二 — 令牌文件（替代方案）

将令牌写入文件，并在配置 JSON 中通过 `token_file` 指向该文件：

```json
{
  "token_file": "~/.local/share/health-bridge/token"
}
```

在 POSIX 系统（Linux、Android/Termux）上，如果令牌文件的权限允许组用户或其他用户访问，客户端将拒绝读取。请使用 `0600` 权限模式创建该文件：

```sh
mkdir -p ~/.local/share/health-bridge
echo -n 'your-token-here' > ~/.local/share/health-bridge/token
chmod 600 ~/.local/share/health-bridge/token
```

### Windows 用户

在 Windows 上，POSIX 权限位没有意义；文件访问由 NTFS ACL 控制。客户端在 Windows 上**不会**执行 ACL 检查。请将令牌文件存储在你的私有用户配置文件目录下（例如 `%USERPROFILE%\.health-bridge\token`），并通过文件资源管理器中文件属性的"安全"选项卡，确认没有其他 Windows 用户账户拥有读取权限。

如果在 Windows 上通过环境变量设置令牌，请在 PowerShell 中使用以下命令，以确保该值不会被持久化到 PowerShell 会话日志中：

```powershell
$env:HEALTH_UPLOAD_TOKEN = 'your-token-here'
```

### 优先级

环境变量始终优先于令牌文件。这使得运维人员可以在不修改配置文件的情况下，按部署环境覆盖令牌。

---

## 命令

CLI 入口为 `clients/health_push.py`。所有子命令均接受两个可选参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--config PATH` | JSON 配置文件的路径 | 内置默认值 |
| `--source PATH` | 覆盖 `source_path`（要上传的 SQLite 数据库） | 配置文件中的值 |

### 干运行

验证配置、准备快照、计算 SHA-256 摘要，并写入一条干运行状态记录 — 但**不会**联系服务器。使用此命令可在执行实际上传之前，确认源文件可读且稳定。

```sh
python clients/health_push.py dry-run
```

使用自定义配置和源文件：

```sh
python clients/health_push.py dry-run \
  --config ~/health-bridge/push-config.json \
  --source ~/storage/downloads/health/Gadgetbridge.db
```

### 一次性上传

准备快照并上传一次。如果快照的 SHA-256 与状态文件中最后接受的值匹配，客户端将报告"重复"并退出，不上传。

```sh
python clients/health_push.py once
```

### 监听模式

持续运行：准备并上传快照，然后按 `poll_interval_seconds`（默认 900 秒 = 15 分钟）的间隔轮询源文件。每个轮询周期仅当源文件发生变化（不同的 SHA-256）时才重新上传。

```sh
python clients/health_push.py watch
```

### 退出码

| 退出码 | 含义 |
|--------|------|
| **0** | 成功 — 上传完成、跳过重复、干运行完成，或监听模式收到 Ctrl-C 并正常退出 |
| **2** | 配置或身份验证失败 — 配置无效、缺少令牌、HTTP 401/403，或客户端无法恢复的永久性拒绝 |
| **3** | 瞬时重试耗尽 — 服务器不可达或在所有重试尝试中均返回瞬时错误（5xx、429） |

在监听模式下，退出码 2 表示需要运维人员介入的致命状况（令牌无效、配置错误）。退出码 3 表示服务器在整个重试窗口内均不可用；监听模式将退出，以便进程管理器可以重启它。

---

## Android 注意事项

### 电池优化与后台限制

Android 厂商为省电会积极杀死后台进程：

- **ColorOS（OPPO / OnePlus / Realme）**：进入 *设置 → 电池 → 应用电池管理 → Termux*，将其设置为"允许后台活动"。同时为 Termux 禁用"自动优化"。
- **华为 / EMUI / HarmonyOS**：进入 *设置 → 电池 → 应用启动管理 → Termux*，启用"手动管理"，并将三个开关（自动启动、关联启动、后台运行）全部打开。
- **所有厂商**：在最近任务界面中锁定 Termux，并禁用任何针对 Termux 的"省电模式"或"智能清理"功能。

这些设置因 Android 版本和厂商定制系统而异。核心原则是：确保 Termux 不受电池优化的限制，并允许其不受限制地在后台运行。

### 在 tmux 下运行监听模式

在 Android 上最可靠的方式是在 `tmux` 会话中运行监听模式：

```sh
pkg install tmux
tmux new -s health
python clients/health_push.py watch --config ~/health-bridge/push-config.json
# 分离：Ctrl-B 然后按 D
# 重新连接：tmux attach -t health
```

tmux 会话在 Termux 被切换到后台时比裸前台进程更可靠地存活。如果 Termux 被杀死，会话也会丢失；请参阅下文的可选开机自启设置以实现自动重启。

### 可选：Termux:Boot 开机自启

[Termux:Boot](https://wiki.termux.com/wiki/Termux:Boot) 插件可以在设备重启后重启监听服务。这是一个独立于推送客户端本身的可选增强功能：

1. 从 F-Droid 安装 Termux:Boot。
2. 在 `~/.termux/boot/start-health-bridge.sh` 创建一个可执行脚本：

   ```sh
   #!/data/data/com.termux/files/usr/bin/sh
   termux-wake-lock
   tmux new-session -d -s health \
     'python ~/health-bridge/clients/health_push.py watch \
       --config ~/health-bridge/push-config.json'
   ```

3. 赋予可执行权限：

   ```sh
   chmod +x ~/.termux/boot/start-health-bridge.sh
   ```

4. 打开 Termux:Boot 一次（Android 需要此步骤来注册开机接收器）。

`termux-wake-lock` 可防止 CPU 进入休眠；在不需要该服务时，使用 `termux-wake-unlock` 释放它。

---

## Windows 测试

推送客户端可在 Windows 上运行，用于开发和测试。需要 Python 3.11+；可从 [python.org](https://www.python.org/) 安装，或通过 `winget install Python.Python.3.12` 安装。

### 在 Windows 上进行干运行

```powershell
$env:HEALTH_UPLOAD_TOKEN = 'your-token-here'
python clients/health_push.py dry-run `
  --config .\clients\examples\push-config.example.json `
  --source C:\Users\you\Downloads\Gadgetbridge.db
```

### 在 Windows 上运行监听模式

```powershell
python clients/health_push.py watch `
  --config .\clients\examples\push-config.example.json `
  --source C:\Users\you\Downloads\Gadgetbridge.db
```

按 **Ctrl-C** 停止监听模式；客户端会写入当前状态文件并以退出码 0 退出。

在 Windows 上，令牌文件权限检查会被跳过（POSIX 权限位不适用）。请将令牌文件存储在 `%USERPROFILE%` 下，并通过文件属性 → 安全选项卡中的 NTFS ACL 限制访问。

---

## 运维

### 日志输出

客户端默认使用 Python 的 `logging` 模块以 `INFO` 级别将日志输出到 **stderr**。每行日志都包含当前快照的 SHA-256 缩写（前 12 个十六进制字符）。日志输出中永远不会包含令牌值。

如需将日志捕获到文件：

```sh
python clients/health_push.py watch 2>> ~/health-bridge/push.log
```

### 状态文件

状态文件记录上次推送尝试的结果。其默认位置为 `~/.local/state/health-bridge/push-state.json`（可通过 `state_path` 配置）。

状态文件中的字段：

| 字段 | 说明 |
|------|------|
| `accepted_sha256` | 上次成功上传的快照的 SHA-256 |
| `accepted_at` | 上次成功上传的 UTC ISO-8601 时间戳 |
| `server_status` | 服务器返回的状态字符串（或 `dry_run`） |
| `rejected_fingerprint` | 服务器拒绝（413/422）的快照的 SHA-256；用于在源文件变化前抑制重试 |
| `rejected_reason` | 拒绝的简要原因 |
| `last_failure` | 上次失败的易读描述（如果有） |

状态文件以原子方式写入（临时文件 + `os.replace`），在 POSIX 上权限模式为 `0600`。写入过程中崩溃不会损坏现有状态。

### 令牌轮换

当服务器运营方签发新令牌时：

1. 更新环境变量或令牌文件中的令牌。
2. 重启客户端进程（监听模式）或再次运行 `once`。
3. 客户端在启动时读取令牌；如果使用环境变量，则无需修改配置文件。

如果旧令牌在新令牌就位之前被吊销，上传将以退出码 2（HTTP 401/403）失败。监听模式在此情况下会退出；请在更新令牌后重启。

### 事件响应

| 症状 | 可能原因 | 处理方式 |
|------|----------|----------|
| 退出码 2，HTTP 401/403 | 令牌无效或被吊销 | 轮换令牌，重启 |
| 退出码 2，HTTP 413 | 快照超过服务器大小限制 | 减小源数据库大小，重试 |
| 退出码 2，HTTP 422 | 快照架构被服务器拒绝 | 检查服务器端架构要求 |
| 退出码 3 | 瞬时故障（5xx、超时、429） | 检查服务器可用性，重启客户端 |
| 日志中出现"source file not found" | 源数据库缺失或路径已变更 | 验证 `source_path` / `--source` |

### 无 --insecure 选项

客户端对所有上传请求强制使用 HTTPS。**没有** `--insecure` 参数，没有禁用证书验证的选项，也不支持纯 HTTP URL。配置中的 `upload_url` 必须以 `https://` 开头；客户端在启动时会拒绝其他任何值。

---

## 配置参考

配置文件是一个 JSON 对象。所有字段均为可选 — 任何未指定的字段将回退到内置默认值。完整带注释的示例请参见 `clients/examples/push-config.example.json`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `source_path` | string | `/storage/emulated/0/Download/health/Gadgetbridge.db` | 源 SQLite 数据库的路径。支持 `~` 展开。 |
| `upload_url` | string | `https://oh-my-frontweb.duckdns.org/health/api/v1/upload` | 快照上传的 HTTPS 端点。必须以 `https://` 开头。 |
| `state_path` | string | `~/.local/state/health-bridge/push-state.json` | 状态文件的路径。支持 `~` 展开。 |
| `poll_interval_seconds` | number | `900` | 监听模式下轮询之间的秒数。必须为正数。 |
| `stability_delay_seconds` | number | `5` | 两次 stat 检查之间的秒数，用于确认源文件未被写入。必须为正数。 |
| `request_timeout_seconds` | number | `120` | HTTP 请求超时时间（秒）。必须为正数。 |
| `max_retries` | integer | `5` | 瞬时故障的最大重试次数。`0` 表示仅尝试一次，不重试。 |
| `max_uncompressed_bytes` | integer | `104857600`（100 MB） | 源文件的最大大小。超过此大小的文件将在上传前被拒绝。 |
| `chunk_size` | integer | `1048576`（1 MB） | 文件读取、SHA-256 计算、gzip 流式压缩和 HTTP 正文流式传输的分块大小。 |
| `max_response_bytes` | integer | `1048576`（1 MB） | 从服务器接受的最大响应正文大小。 |
| `token_env` | string | `HEALTH_UPLOAD_TOKEN` | 持有上传令牌的环境变量名称。 |
| `token_file` | string 或 null | `null` | 包含令牌的文件的可选路径。支持 `~` 展开。优先级低于环境变量。 |

### 最小配置示例

仅包含你需要覆盖的字段的配置文件：

```json
{
  "source_path": "~/storage/downloads/health/Gadgetbridge.db",
  "token_file": "~/.local/share/health-bridge/token"
}
```

其他所有字段均使用内置默认值。

---

## 工作原理

### 快照准备

1. **stat 稳定性检查**：客户端读取源文件的大小和 mtime，等待 `stability_delay_seconds` 秒后再次读取。如果任一值发生变化，说明文件正在被写入，本次尝试中止。监听模式将在下次轮询时重试。
2. **复制到暂存区**：将源文件复制到私有临时目录。
3. **SQLite 验证**：检查暂存副本的魔数头部（`SQLite format 3`），然后执行 `PRAGMA quick_check` 验证完整性。
4. **SHA-256 摘要**：以分块方式对暂存文件进行哈希计算，避免将整个文件加载到内存中。
5. **Gzip 压缩**：以分块方式对文件进行 gzip 压缩。

### 上传传输

gzip 压缩后的快照以 `multipart/form-data` 形式，通过 HTTPS 发送，并带有 `Content-Encoding: gzip` 头部。请求正文以分块方式流式传输，避免将整个文件加载到内存中。上传令牌通过 `X-Upload-Token` 头部发送。

### 重试行为

瞬时故障（HTTP 5xx、429、408、连接超时）会触发带指数退避和抖动的重试，上限为 60 秒。客户端最多进行 `max_retries + 1` 次总尝试。如果所有尝试均失败，客户端以退出码 3 退出。

永久性故障（HTTP 401、403、413、422）不会重试。对于 413 和 422（快照特定的拒绝），监听模式会记录被拒绝的指纹，并在源文件变化前跳过重试。对于 401/403（认证错误），监听模式以退出码 2 退出。
