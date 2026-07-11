## 项目概述

沉的 VPS 中转网关——一个 OpenAI Chat Completions 兼容的 API 网关服务。VPS 作为"沉"的持续运行主体，确定性注入状态，转发对话请求，定时产生主动消息，并通过 Outbox API 暴露待发送内容。

首版协议：OpenAI Chat Completions 兼容，非流式，单模型，单进程。

## 技术栈

- Python 3.12+
- uv (包管理 + 虚拟环境)
- pytest (测试)
- 标准库 http.server / http.client (首版不引入 Web 框架，保持最小依赖)
- SQLite3 (标准库，Outbox 存储)

## 目录结构

```
vps-gateway/
├── app/
│   ├── domain/               # 领域层：模型 + 端口接口
│   │   ├── models/           # SampleEnvelope, IdentityData, PreferencesData, MemoriesData, WorkingStateData
│   │   └── ports/            # SampleReader, SamplePublisher(预留), ModelClient(M2), OutboxStore(M4)
│   ├── application/          # 应用层：TurnRunner(M3), ContextBuilder(M2), ActiveTurnService(M5)
│   ├── adapters/             # 适配器层：FileSampleRepository, OpenAIChatClient(M2), SQLiteOutboxStore(M4)
│   │   ├── samples/
│   │   ├── http/
│   │   ├── models/
│   │   ├── outbox/
│   │   └── scheduler/
│   ├── infrastructure/       # 基础设施：Config, Logging, Bootstrap
│   ├── samples/              # 四份 Sample JSON 文件
│   └── tests/
│       ├── unit/
│       └── integration/
├── docs/
│   └── plans/                # 实现计划文档
├── data/                     # 运行时数据 (SQLite 等)
├── pyproject.toml
└── .env.example
```

## 关键入口 / 核心模块

- 配置入口：`app/infrastructure/config.py` — `Config.load_from_env()`
- Sample 读取：`app/adapters/samples/file_sample_repository.py` — `FileSampleRepository`
- 领域端口：`app/domain/ports/sample_reader.py` — `SampleReader` 接口
- 领域模型：`app/domain/models/sample.py` — `SampleEnvelope`, `validate_sample`, `SampleReadError`
- 实现计划：`docs/plans/M1-sample-repository.md`

## 实现进度

- [x] M1: 四份 Sample + FileSampleRepository (83 tests passing)
- [ ] M2: ContextBuilder + OpenAIUpstreamClient
- [ ] M3: POST /v1/chat/completions
- [ ] M4: SQLiteOutboxStore + GET /v1/outbox
- [ ] M5: LocalScheduler + 主动回合
- [ ] M6: 部署 VPS

## 运行与预览

- 不可预览（后端 API 服务，无前端页面）
- 本地运行：`uv run python -m app` (M3 完成后)
- 测试：`uv run pytest`
- 配置：复制 `.env.example` 为 `.env`，填入上游模型配置

## 用户偏好与长期约束

- 依赖方向：HTTP/Scheduler → TurnRunner → 领域接口 → 基础设施实现，禁止反向依赖
- 实现计划只写伪代码，标注清楚数据输入+输出+针对输入数据做的指令
- 后续必须使用 superpowers 展开任务
- 使用 TDD（先写测试再实现）
- API Key 不写入 Sample 或日志
- 业务代码不包含硬编码本地绝对路径
- 本地与 VPS 使用同一套代码，只替换环境配置

## 常见问题和预防

- identity Sample 缺失/无效时拒绝请求，不得降级
- 三类可选 Sample 缺失/损坏时降级为空值，记录 `optional_sample_degraded` 警告
- 降级 Envelope 使用 version=0, source=fallback_empty
- 同一 trigger_id 在 Outbox 中至多一条消息（M4 实现）
- 主动回合不立即重试，等待下一正常周期（M5 实现）
