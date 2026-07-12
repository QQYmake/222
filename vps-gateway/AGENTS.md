# AGENTS.md — 项目规范入口

> 完整架构、数据流、模块索引和扩展蓝图见 [README.md](./README.md)

## 项目概述

OpenAI Chat Completions 兼容的 API 网关。从四份只读 Sample 注入状态为 system message，转发对话请求；v2 增加 VPS 工具循环、持久化唤醒计划和单读取者 Outbox 长轮询，保证用户回合与主动回合互不阻塞、主动回合永不并发。

## 技术栈

Python 3.12+ / uv / pytest + pytest-asyncio / FastAPI + uvicorn + httpx (async) / SQLite3 (短连接)

## 关键入口

- 启动：`app/main.py` → `uvicorn app.main:app`
- 应用工厂：`app/application/app.py` → `create_app(config)`
- 回合编排：`app/application/turn_runner.py` → `TurnRunner.run()` (async)
- 工具循环：`app/application/model_tool_loop.py` → `ModelToolLoop.run()`
- 唤醒控制：`app/application/wake_controller.py` → `WakeController`
- 长轮询：`app/application/outbox_longpoll_service.py` → `OutboxLongPollService`
- 配置：`app/infrastructure/config.py` → `Config.load_from_env()`

## 目录结构

```
app/
├── domain/models/  # chat_completion, tool, turn, wake_job, outbox, sample
├── domain/ports/   # model_client, outbox_store, wake_job_store
├── application/    # turn_runner, model_tool_loop, tool_dispatcher, wake_controller, wake_planner, active_turn_gate, single_reader_gate, outbox_longpoll_service, schedule_admission_policy, wake_start_policy
├── adapters/http/  # chat_controller, outbox_controller
├── adapters/models/ # async_openai_client
├── adapters/tools/ # registry, tool_dispatcher, get_server_time, test_tools, wake_tools
├── adapters/wakeups/ # sqlite_wake_job_store
├── adapters/outbox/ # sqlite_outbox_store, in_process_notifier
├── adapters/scheduler/ # local_scheduler
└── infrastructure/ # config, logging
```

## 运行与预览

- 不可预览（后端 API 服务）
- 运行：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 测试：`uv run pytest`（383 passed）
- 配置：复制 `.env.example` 为 `.env`

## 用户偏好与长期约束

- 依赖方向：适配器 → 应用层 → 领域接口 → 基础设施，禁止反向
- 实现计划只写伪代码，标注数据输入+输出+指令
- 后续必须使用 superpowers 展开任务
- TDD：先写测试再实现
- API Key 不写入 Sample 或日志
- 业务代码不包含硬编码本地绝对路径
- 本地与 VPS 使用同一套代码，只替换环境配置
- SQLite 每次操作短连接，不长期共享 connection
- 用户回合不获取 ActiveTurnGate，可与主动回合并行
- 两个主动回合永远串行；冲突任务直接 expired
- 工具失败不重试，转成 ToolResult 回灌模型
- 最多 5 轮、10 次工具调用、单工具 15 秒
- 长轮询等待期间不持有 SQLite connection/transaction
- FastAPI lifespan 是 Scheduler 唯一启动与停止入口

## 实现进度

v2 M0—M7 全部完成，383 个测试通过（264 v1 基线 + 119 v2 新增），真实 API 工具冒烟通过。
v2 架构不变量（第 15 节 18 条）全部实现并验证。
第 14 节扩展方向（状态演化、可靠 ACK、多设备、前端工具、多实例）未实现，保留接口。

## 常见问题和预防

- identity Sample 缺失/无效 → 拒绝请求（503），不降级
- 三类可选 Sample 缺失/损坏 → 降级为空值 + 警告
- 同一 trigger_id 幂等（ON CONFLICT DO NOTHING + SELECT）
- `<NO_MESSAGE>` 不写 Outbox
- 上游 base_url 可能已含 `/v1`，客户端自动处理
- Config.validate() 启动时 fail-fast
- LocalScheduler 用 threading.Event 可中断睡眠
- 前端 tools/tool_choice → 400 client_tools_not_allowed
- 重启遗留 running → expired(crash_recovery)
- pending 超过 START_GRACE → expired(missed_deadline)，不补跑
- 全量测试运行时 smoke test 需显式设置 UPSTREAM_BASE_URL 防止环境变量污染
