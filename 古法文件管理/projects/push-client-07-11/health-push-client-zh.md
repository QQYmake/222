---
# Health Push Client 实现计划

> **面向 agentic worker：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 来逐任务实现本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 构建一个可靠的 Python 客户端，用于验证 Gadgetbridge 数据库导出文件，并以一次性模式或长期运行的监控模式将仅变更的快照上传至 VPS。

**架构：** 一个轻量 CLI 将职责委托给各专注模块，分别负责配置、快照准备、HTTP 传输、状态持久化以及监控/重试编排。所有生产环境行为均使用 Python 标准库，验证 HTTPS 证书，在检查前暂存变更中的 Android 导出文件，并以原子方式记录服务器最后接受的 SHA-256。

**技术栈：** Python 3.11+，标准库（`argparse`、`sqlite3`、`gzip`、`hashlib`、`http.client`、`ssl`、`tempfile`、`json`、`pathlib`、`urllib.parse`、`unittest`）

---

## 范围与仓库约束

本计划仅实现已批准系统设计中的推送客户端子项目。拉取客户端和 VPS 交接提示将在本客户端通过测试后获得各自的独立计划。

当前工作区包含一个空的 `.git` 目录而非有效仓库，因此 worktree 和提交均不可用。未经用户批准，不得初始化或修复 Git 元数据。通过计划复选框、测试输出和显式文件验证来保存进度。

真实用户数据库已复制到 `work/fixtures/Gadgetbridge.db`。其 SHA-256 与源文件 `D:\Gadgetbridge.db` 一致，大小为 835,584 字节，`PRAGMA quick_check` 返回 `ok`。该数据库包含个人健康数据，严禁复制到交付物、日志、测试或未来仓库中。

## 文件结构

创建：

```text
clients/
  health_push.py                 # 仅 CLI 入口
  health_bridge/
    __init__.py                  # 包标记和版本
    push_config.py               # 类型化配置加载与验证
    push_snapshot.py             # 稳定性检查、暂存、SQLite 验证、哈希、gzip
    push_transport.py            # 流式 HTTPS multipart 传输
    push_state.py                # 原子化最后接受状态
    push_service.py              # 一次性、重试和监控编排
  examples/
    push-config.example.json     # 占位符，不含令牌
  tests/
    __init__.py
    test_push_config.py
    test_push_snapshot.py
    test_push_transport.py
    test_push_state.py
    test_push_service.py
  README-push.md
```

模块依赖流向内：

```text
health_push.py -> push_service -> push_config
                               -> push_snapshot
                               -> push_transport
                               -> push_state
```

`push_snapshot`、`push_state` 和 `push_config` 不得导入传输模块或 CLI。

## 任务 1：配置契约

**文件：**

- 创建：`clients/health_bridge/__init__.py`
- 创建：`clients/health_bridge/push_config.py`
- 创建：`clients/tests/__init__.py`
- 创建：`clients/tests/test_push_config.py`
- 创建：`clients/examples/push-config.example.json`

- [ ] **步骤 1：编写失败的配置测试**

覆盖：

- 源路径、上传 URL、状态路径、轮询间隔、稳定性延迟、超时时间、重试次数、块大小和最大响应字节数的默认值。
- 从 JSON 配置文件加载非敏感值。
- 从配置的环境变量名读取令牌，默认为 `HEALTH_UPLOAD_TOKEN`，具有最高优先级。
- 当环境变量不存在时，回退到配置的权限受限令牌文件。令牌文件仅包含令牌，末尾换行符会被去除。
- 拒绝具有组/其他用户权限位的 POSIX 令牌文件。在 Windows 上，文档应说明该文件必须存储在用户的私有配置目录下；不得声称 POSIX 模式检查能保障 Windows ACL 的安全性。
- 在非 dry-run 模式下拒绝缺少令牌的情况。
- 拒绝非 HTTPS 上传 URL。
- 拒绝非正数的间隔、超时、大小限制和块大小。
- 确保异常消息中永远不包含令牌值。

预期的公开接口：

```python
@dataclass(frozen=True)
class PushConfig:
    source_path: Path
    upload_url: str
    state_path: Path
    poll_interval_seconds: float
    stability_delay_seconds: float
    request_timeout_seconds: float
    max_retries: int
    max_uncompressed_bytes: int
    chunk_size: int
    max_response_bytes: int
    token_env: str
    token_file: Path | None
    upload_token: str | None

def load_push_config(path: Path | None, environ: Mapping[str, str], *, dry_run: bool) -> PushConfig: ...
```

- [ ] **步骤 2：运行测试并验证失败**

运行：

```text
python -m unittest clients.tests.test_push_config -v
```

预期：因 `push_config.py` 不存在导致导入失败。

- [ ] **步骤 3：实现最小化的已验证配置加载**

使用 JSON 进行跨平台配置。按以下顺序合并：内置默认值、JSON 文件、环境变量令牌。不允许在 JSON 示例中包含令牌字面量，也不得将令牌值写入 repr/日志输出。

默认生产值：

```json
{
  "source_path": "/storage/emulated/0/Download/health/Gadgetbridge.db",
  "upload_url": "https://oh-my-frontweb.duckdns.org/health/api/v1/upload",
  "state_path": "~/.local/state/health-bridge/push-state.json",
  "poll_interval_seconds": 900,
  "stability_delay_seconds": 5,
  "request_timeout_seconds": 120,
  "max_retries": 5,
  "max_uncompressed_bytes": 104857600,
  "chunk_size": 1048576,
  "max_response_bytes": 1048576,
  "token_env": "HEALTH_UPLOAD_TOKEN",
  "token_file": null
}
```

- [ ] **步骤 4：运行配置测试**

预期：所有配置测试通过。

- [ ] **步骤 5：记录检查点**

记录准确的通过测试数量。因工作区不是有效仓库，跳过 Git 提交。

## 任务 2：稳定快照准备

**文件：**

- 创建：`clients/health_bridge/push_snapshot.py`
- 创建：`clients/tests/test_push_snapshot.py`

- [ ] **步骤 1：编写失败的快照测试**

使用临时合成的 SQLite 数据库，绝不使用个人数据库。覆盖：

- 源路径缺失。
- 拒绝超过配置的未压缩大小限制的源文件。
- 检测在稳定性检查之间发生变化的文件。
- 仅在两次 `stat` 观察结果一致后才复制。
- 使用 SQLite URI `mode=ro` 打开暂存副本。
- 在打开之前，拒绝前 16 字节不是 SQLite 魔数头 `SQLite format 3\x00` 的暂存输入。
- 接受 `PRAGMA quick_check == "ok"`。
- 拒绝任意字节和损坏的 SQLite 文件。
- 生成预期的 SHA-256。
- 生成有效的 gzip，其解压字节与暂存副本一致。
- 在成功和失败后清理所有临时文件。

预期的公开接口：

```python
@dataclass(frozen=True)
class PreparedSnapshot:
    source_path: Path
    staged_db_path: Path
    gzip_path: Path
    sha256: str
    uncompressed_bytes: int
    compressed_bytes: int

@contextmanager
def prepare_snapshot(config: PushConfig, sleep: Callable[[float], None] = time.sleep) -> Iterator[PreparedSnapshot]: ...
```

- [ ] **步骤 2：运行测试并验证失败**

运行：

```text
python -m unittest clients.tests.test_push_snapshot -v
```

预期：因 `push_snapshot.py` 不存在导致导入失败。

- [ ] **步骤 3：实现暂存与验证**

使用私有 `TemporaryDirectory`、`shutil.copyfile`、URI 只读 SQLite 连接、`PRAGMA quick_check`、分块 SHA-256 和分块 gzip 输出。绝不查询或记录健康数据行。

- [ ] **步骤 4：运行快照测试**

预期：所有快照测试通过。

- [ ] **步骤 5：运行真实夹具的 dry-run 准备检查**

对 `work/fixtures/Gadgetbridge.db` 调用准备函数，使用隔离的状态路径且不提供令牌。验证：

- quick_check 成功。
- 报告的字节数为 835,584。
- SHA-256 为 `bb7329b9fc3dc03e73902d7f1a1ed013297863eca1b980c0c3181e8c26a36ba9`。
- 不打印任何健康数据行值或设备标识符。

- [ ] **步骤 6：记录检查点**

记录单元测试和真实夹具验证结果。

## 任务 3：原子状态持久化

**文件：**

- 创建：`clients/health_bridge/push_state.py`
- 创建：`clients/tests/test_push_state.py`

- [ ] **步骤 1：编写失败的状态测试**

覆盖：

- 状态文件缺失时返回空状态。
- 有效状态的往返读写。
- 原子替换后不残留临时文件。
- 父目录创建。
- 损坏的状态产生明确错误且不覆盖原文件。
- 状态仅包含已接受的 SHA-256/时间/状态、用于抑制未变更永久失败的已拒绝快照指纹或哈希/原因、以及上次失败摘要。
- 令牌值永不出现。

预期的公开接口：

```python
@dataclass(frozen=True)
class PushState:
    accepted_sha256: str | None = None
    accepted_at: str | None = None
    server_status: str | None = None
    rejected_fingerprint: str | None = None
    rejected_reason: str | None = None
    last_failure: str | None = None

def load_state(path: Path) -> PushState: ...
def save_state(path: Path, state: PushState) -> None: ...
```

- [ ] **步骤 2：运行测试并验证失败**

运行 `python -m unittest clients.tests.test_push_state -v`。

- [ ] **步骤 3：实现状态加载与原子保存**

写入临时同级文件，flush 并 `os.fsync`，然后 `os.replace`。在 POSIX 上，请求模式 `0600`；不要仅因 POSIX 模式不同就在 Windows 上失败。

- [ ] **步骤 4：运行状态测试并记录检查点**

预期：所有状态测试通过。

## 任务 4：流式 HTTPS multipart 传输

**文件：**

- 创建：`clients/health_bridge/push_transport.py`
- 创建：`clients/tests/test_push_transport.py`

- [ ] **步骤 1：编写失败的传输测试**

使用注入的模拟连接对象；不访问网络。覆盖：

- URL 解析仅接受 HTTPS。
- 请求路径在存在查询字符串时包含该查询字符串。
- 请求头包含 `X-Upload-Token`、multipart 内容类型、确定性的内容长度、gzip 内容编码元数据、快照 SHA-256 和非敏感的 user agent。
- 压缩文件按配置的块大小发送，而非完全加载到内存中。
- HTTP 200/201 的 JSON 成功响应。
- HTTP 202 且 `status=unsupported_schema` 被归类为已送达但需关注。
- HTTP 401/403/413/422 为永久失败。
- HTTP 408/429 和 5xx 为临时失败。
- 格式错误或响应体超过 `max_response_bytes` 时产生有界的脱敏错误，且不会将无界数据读入内存。
- 异常和 repr 中永不包含令牌。

预期的公开接口：

```python
@dataclass(frozen=True)
class UploadResult:
    http_status: int
    status: str
    delivered: bool
    response: dict[str, object]

class TransientUploadError(RuntimeError): ...
class PermanentUploadError(RuntimeError): ...

def upload_snapshot(config: PushConfig, snapshot: PreparedSnapshot, connection_factory=None) -> UploadResult: ...
```

- [ ] **步骤 2：运行测试并验证失败**

运行 `python -m unittest clients.tests.test_push_transport -v`。

- [ ] **步骤 3：实现传输**

使用 `http.client.HTTPSConnection` 配合 `ssl.create_default_context()`。构建 multipart 前导/后导字节，在发送前计算内容长度，然后以分块方式流式发送 gzip 文件。不提供证书验证绕过选项。

- [ ] **步骤 4：运行传输测试并记录检查点**

预期：所有传输测试在无网络访问的情况下通过。

## 任务 5：一次性与监控编排

**文件：**

- 创建：`clients/health_bridge/push_service.py`
- 创建：`clients/tests/test_push_service.py`

- [ ] **步骤 1：编写失败的服务测试**

注入快照、传输、状态、时钟、随机数和休眠函数。覆盖：

- 当准备好的 SHA 等于已接受状态时，一次性模式跳过传输。
- HTTP 200/201 更新已接受状态。
- HTTP 202 `unsupported_schema` 更新已接受哈希和状态，但记录一条警告日志。
- 永久错误不重试。
- `max_retries` 表示在初始请求之后的额外重试次数，因此总尝试次数最多为 `max_retries + 1`。
- 临时错误重试至配置上限。
- 指数退避有上限且添加抖动。
- 后续监控迭代可在失败后恢复。
- 监控模式在源文件缺失或临时上传错误时不终止。
- 认证/授权失败立即终止监控模式并返回永久失败结果，因为需要用户干预。
- 本地验证失败和服务器快照拒绝（如 HTTP 413/422）会记录被拒绝的源指纹或 SHA-256，且不会在每次轮询时重试；监控模式会等待源文件变更后再尝试。
- 键盘中断干净退出。
- 日志仅包含操作元数据和缩写哈希，绝不包含令牌或健康数据行。

预期的公开接口：

```python
class PushOutcome(Enum):
    UPLOADED = "uploaded"
    DUPLICATE = "duplicate"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    DRY_RUN = "dry_run"
    PERMANENT_FAILURE = "permanent_failure"
    TRANSIENT_EXHAUSTED = "transient_exhausted"

def run_once(config: PushConfig, *, dry_run: bool = False, dependencies: Dependencies | None = None) -> PushOutcome: ...
def run_watch(config: PushConfig, *, dependencies: Dependencies | None = None) -> int: ...
```

`run_once()` 返回 `PushOutcome` 供 CLI 映射。`run_watch()` 拥有持久循环并直接返回最终进程退出码：0 表示干净中断，2 表示致命配置/认证/永久失败，3 仅在配置策略明确指定在临时重试耗尽后终止时使用。监控模式内的普通临时失败会被记录并在后续轮询迭代中重试，而非终止进程。

- [ ] **步骤 2：运行测试并验证失败**

运行 `python -m unittest clients.tests.test_push_service -v`。

- [ ] **步骤 3：实现编排**

将重试逻辑保留在传输模块之外。持久化失败摘要和非敏感的已拒绝快照指纹/哈希，不含任何敏感信息。将 202 unsupported schema 视为已送达，以防止无限重传循环。区分致命的认证/配置错误与特定快照的永久拒绝，使监控模式知道是退出还是等待变更的导出文件。

- [ ] **步骤 4：运行服务测试并记录检查点**

预期：所有服务测试通过。

## 任务 6：CLI 入口

**文件：**

- 创建：`clients/health_push.py`
- 创建：`clients/tests/test_health_push_cli.py`

- [ ] **步骤 1：编写失败的 CLI 测试**

覆盖：

- `once`、`watch` 和 `dry-run` 子命令。
- 可选的 `--config` 路径。
- 所有子命令的可选 `--source` 路径覆盖；它优先于 JSON 配置和内置源默认值。
- 有用的退出码：0 成功/重复/dry-run，2 配置或永久验证失败，3 临时上传重试耗尽失败。
- dry-run 不需要令牌且永不发送请求。
- `--help` 文档记录确切的 Android 路径和上传 URL 默认值。

- [ ] **步骤 2：运行测试并验证失败**

运行 `python -m unittest clients.tests.test_health_push_cli -v`。

- [ ] **步骤 3：实现轻量 CLI**

入口点解析参数、加载配置、应用显式源路径覆盖、初始化日志、调用服务，并将 `PERMANENT_FAILURE` 映射为退出码 2，`TRANSIENT_EXHAUSTED` 映射为退出码 3。业务逻辑保留在各模块中。

- [ ] **步骤 4：运行 CLI 测试**

预期：所有 CLI 测试通过。

- [ ] **步骤 5：在 Windows 上运行真实数据库 dry-run**

运行：

```text
python clients/health_push.py dry-run --source work/fixtures/Gadgetbridge.db
```

预期：成功验证，确切的缩写预期哈希，835,584 字节，压缩字节数，且无网络请求。

## 任务 7：文档与长期运行操作

**文件：**

- 创建：`clients/README-push.md`
- 修改：`clients/examples/push-config.example.json`

- [ ] **步骤 1：编写文档验收检查**

手动验证文档包含：

- Termux `termux-setup-storage` 前置条件。
- Python 版本检查。
- 将文件复制到私有 Termux 目录。
- 导出 `HEALTH_UPLOAD_TOKEN` 时尽可能不将其写入 shell 历史记录。
- 创建模式为 `0600` 的令牌文件作为环境变量的替代方案，并提供单独的 Windows 用户配置目录指导。
- 一次性 dry-run、一次性上传和监控命令。
- ColorOS/华为的电池优化和后台进程注意事项。
- 在 `tmux` 下运行监控模式作为首选的简单方案。
- 后续可选的 Termux:Boot 设置，与程序明确分开。
- Windows Python dry-run/监控示例用于测试。
- 日志和状态文件位置。
- 令牌轮换和事件处理指南。
- 无 `--insecure` 绕过方案。

- [ ] **步骤 2：编写简洁文档和安全的示例配置**

不包含真实令牌、数据库内容、设备标识符或用户信息。

- [ ] **步骤 3：验证每个文档记录命令的 `--help` 或 dry-run 行为**

预期：命令与实际 CLI 语法一致。

## 任务 8：完整验证

**文件：**

- 测试：所有 `clients/tests/test_*.py`
- 检查：`clients/health_push.py`、`clients/health_bridge/*.py`、`clients/README-push.md`

- [ ] **步骤 1：运行完整单元测试套件**

运行：

```text
python -m unittest discover -s clients/tests -p "test_*.py" -v
```

预期：所有测试通过。

- [ ] **步骤 2：运行编译检查**

运行：

```text
python -m compileall -q clients
```

预期：退出码 0。

- [ ] **步骤 3：运行真实夹具 dry-run 两次**

预期：两次运行验证相同的哈希和大小；无源文件修改且无网络请求。

- [ ] **步骤 4：验证源数据库保持不变**

将 `D:\Gadgetbridge.db` 和 `work/fixtures/Gadgetbridge.db` 的 SHA-256 与原始记录的哈希进行比对（前提是源文件此后未被用户替换）。绝不覆盖源文件。

- [ ] **步骤 5：检查敏感信息泄露和不安全的 TLS 选项**

在源代码、测试、示例和文档中搜索令牌值、`verify=False`、不安全的 SSL 上下文和个人标识符。预期：无。

- [ ] **步骤 6：更新父任务计划**

仅在捕获新的验证输出后才将推送客户端阶段标记为完成。然后创建独立的拉取客户端实现计划和 VPS Codex 交接计划。
