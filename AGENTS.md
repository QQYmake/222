# AGENTS.md — 工作区根目录

## 项目概述

本工作区包含"沉的 VPS 中转网关"项目及其相关资料。核心项目是 `vps-gateway/`——一个 OpenAI Chat Completions 兼容的 API 网关，为 AI 意识体"沉"提供持续运行基础设施。

## 目录结构

| 目录 | 用途 |
|---|---|
| `vps-gateway/` | 核心项目：FastAPI 网关服务（Python 3.12 / uv） |
| `VPS网关/` | 架构文档（v1/v2 架构文档、编码提示词、模板） |
| `古法文件管理/` | 独立的文件管理工具项目（非当前迭代范围） |
| `superpowers-main/` | superpowers 工具集（用户偏好：展开任务时使用） |

## 技术栈

- Python 3.12+ / uv / pytest + pytest-asyncio
- FastAPI + uvicorn + httpx (async)
- SQLite3 (短连接，WAL 模式)

## 关键入口

- 核心项目入口：`vps-gateway/app/main.py`
- 应用工厂：`vps-gateway/app/application/app.py` → `create_app(config)`
- 回合编排：`vps-gateway/app/application/turn_runner.py` → `TurnRunner`
- 部署脚本：`vps-gateway/scripts/setup.sh` + `vps-gateway/scripts/http_run.sh`

## 运行与预览

- 不可预览（后端 API 服务，非浏览器交互型）
- 本地运行：`cd vps-gateway && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 测试：`cd vps-gateway && uv run pytest`
- 配置：复制 `.env.example` 为 `.env`，填入 API Key 等必填项

## 用户偏好与长期约束

- 依赖方向：适配器 → 应用层 → 领域接口 → 基础设施，禁止反向
- 实现计划只写伪代码，标注数据输入+输出+指令
- 后续必须使用 superpowers 展开任务
- TDD：先写测试再实现
- API Key 不写入 Sample 或日志
- 业务代码不包含硬编码本地绝对路径
- 本地与 VPS 使用同一套代码，只替换环境配置
- SQLite 每次操作短连接，不长期共享 connection
- 详细项目级约束见 `vps-gateway/AGENTS.md`

## 实现进度

v2 M0—M7 全部完成，383 个测试通过（380 passed + 3 skipped smoke），真实 API 工具冒烟通过。
v2 架构不变量（18 条）全部实现并验证。
扩展方向（状态演化、可靠 ACK、多设备、前端工具、多实例）未实现，保留接口。

## 常见问题和预防

- 全量测试运行时 smoke test 需显式设置 UPSTREAM_BASE_URL 防止环境变量污染
- identity Sample 缺失/无效 → 拒绝请求（503），不降级
- 前端 tools/tool_choice → 400 client_tools_not_allowed
- 部署端口固定为 5000，本地开发默认 8000
