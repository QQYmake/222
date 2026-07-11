# M6 — 部署 VPS + 沙盒终端总体测试

## 范围

把网关部署为可运行的单进程服务，使用真实 API 进行端到端冒烟测试。
本地沙盒模拟 VPS 环境：单进程 uvicorn + SQLite + 文件系统 Sample。

## Task 分解

### Task 1: 启动入口完善

**数据输入:**
- 环境变量（从 `.env` 加载）
- `Config.load_from_env()`
- `create_app(config)`

**针对输入数据执行的指令:**
1. 创建 `app/main.py`
2. 加载 `.env`（使用 `python-dotenv` 或手动 `os.environ`）
3. 调用 `Config.load_from_env()`
4. 调用配置校验
5. 调用 `create_app(config)`
6. uvicorn 启动

**数据输出:**
- 监听 `GATEWAY_HOST:GATEWAY_PORT` 的 HTTP 服务

### Task 2: 真实 API 冒烟测试

**数据输入:**
- `.env` 中的真实 API 凭据
- 真实 Sample 文件
- 真实 SQLite 数据库

**针对输入数据执行的指令:**

测试 A — 被动回合 (12.2):
1. 启动网关服务
2. 发送标准 OpenAI Chat 请求到 `POST /v1/chat/completions`
3. 验证返回体格式（choices[0].message.content 非空）
4. 验证上游确实收到了 system message（从返回内容推断模型理解了角色设定）

测试 B — 主动回合 (12.3):
1. 调用 `LocalScheduler` 手动触发一次主动回合
2. 验证 Outbox 中写入一条消息（或 NO_MESSAGE）
3. 验证 `GET /v1/outbox` 能查询到

测试 C — Outbox API (12.4):
1. `GET /v1/outbox?after=0&limit=10`
2. 验证返回格式、游标、不重复

测试 D — 可迁移性 (12.5):
1. 检查无硬编码路径
2. 验证所有路径来自环境变量

**数据输出:**
- 结构化的测试结果日志

### Task 3: 沙盒终端验证

**数据输入:**
- 完整的应用栈

**针对输入数据执行的指令:**
1. 后台启动 uvicorn
2. 用 `curl` 模拟外部客户端进行端到端测试
3. 验证完整链路: 请求 → 鉴权 → Sample 读取 → 上下文注入 → 上游调用 → 响应 → Outbox
4. 关闭服务，验证 SQLite 数据持久

**数据输出:**
- 端到端测试通过/失败报告

## 验收基线 (12.2 + 12.3 + 12.4 + 12.5)

| # | 验收项 | 测试方式 |
|---|--------|----------|
| 1 | 标准 OpenAI Chat 请求可以调用本地网关 | curl POST /v1/chat/completions |
| 2 | 上游实际收到服务器 system message | 从返回内容推断 |
| 3 | 返回体可被 OpenAI Chat 客户端解析 | JSON 结构校验 |
| 4 | stream=true 得到 400 | curl 验证 |
| 5 | 主动回合消息写入 Outbox | 手动触发 + GET /v1/outbox |
| 6 | GET /v1/outbox 游标查询正常 | curl 验证 |
| 7 | 业务代码不包含硬编码路径 | 代码审查 |
| 8 | 服务重启后 Outbox 消息持久 | 重启后 GET /v1/outbox |
