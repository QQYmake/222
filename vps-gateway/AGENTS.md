# AGENTS.md — 项目规范入口

> 完整架构、数据流、模块索引和扩展蓝图见 [README.md](./README.md)

## 项目概述

OpenAI Chat Completions 兼容的 API 网关。从四份只读 Sample 注入状态为 system message，转发对话请求；定时触发主动回合，写入 Outbox 供前端拉取。

## 技术栈

Python 3.12+ / uv / pytest / FastAPI + uvicorn + httpx / SQLite3 (WAL)

## 关键入口

- 启动：`app/main.py` → `uvicorn app.main:app`
- 应用工厂：`app/application/app.py` → `create_app(config)`
- 回合编排：`app/application/turn_runner.py` → `TurnRunner.run()`
- 配置：`app/infrastructure/config.py` → `Config.load_from_env()`

## 运行与预览

- 不可预览（后端 API 服务）
- 运行：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 测试：`uv run pytest`（264 passed）
- 配置：复制 `.env.example` 为 `.env`

## 用户偏好与长期约束

- 依赖方向：适配器 → 应用层 → 领域接口 → 基础设施，禁止反向
- 实现计划只写伪代码，标注数据输入+输出+指令
- 后续必须使用 superpowers 展开任务
- TDD：先写测试再实现
- API Key 不写入 Sample 或日志
- 业务代码不包含硬编码本地绝对路径
- 本地与 VPS 使用同一套代码，只替换环境配置

## 实现进度

M1-M6 全部完成，264 个测试通过，真实 API 冒烟测试通过。

## 常见问题和预防

- identity Sample 缺失/无效 → 拒绝请求（503），不降级
- 三类可选 Sample 缺失/损坏 → 降级为空值 + 警告
- 同一 trigger_id 幂等（ON CONFLICT DO NOTHING + SELECT）
- `<NO_MESSAGE>` 不写 Outbox
- 上游 base_url 可能已含 `/v1`，客户端自动处理
- Config.validate() 启动时 fail-fast
- LocalScheduler 用 threading.Event 可中断睡眠
