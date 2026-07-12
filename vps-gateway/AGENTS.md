# AGENTS.md — 项目规范入口

> 完整架构、数据流、模块索引和扩展蓝图见 [README.md](./README.md)

## 项目概述

OpenAI Chat Completions 兼容的 API 网关。从四份只读 Sample 注入状态为 system message，转发对话请求；v2 增加 VPS 工具循环、持久化唤醒计划和单读取者 Outbox 长轮询；v3 增量引入认知记忆引擎（ebbingflow vendoring + ChromaDB + SQLite 图谱 + 意图分类 + 多轨检索 + @4/@6 路径 + 2am 沉淀管线）。

## 技术栈

Python 3.12+ / uv / pytest + pytest-asyncio / FastAPI + uvicorn + httpx (async) / SQLite3 (短连接) / ChromaDB (嵌入式) / rank-bm25 / sentence-transformers (可选) / ebbingflow (vendored)

## 关键入口

- 启动：`app/main.py` → `uvicorn app.main:app`
- 应用工厂：`app/application/app.py` → `create_app(config)`
- 回合编排：`app/application/turn_runner.py` → `TurnRunner.run_user_turn()` (async)
- 工具循环：`app/application/model_tool_loop.py` → `ModelToolLoop.run()`
- 唤醒控制：`app/application/wake_controller.py` → `WakeController`
- 长轮询：`app/application/outbox_longpoll_service.py` → `OutboxLongPollService`
- 配置：`app/infrastructure/config.py` → `Config.load_from_env()`
- 记忆引擎：`app/application/memory/memory_engine.py` → `MemoryEngine`
- 意图分类：`app/application/memory/intent_classifier.py` → `IntentClassifier`
- 检索管线：`app/application/memory/retrieval_pipeline.py` → `RetrievalPipeline`
- 润色桥接：`app/application/memory/polish_bridge.py` → `PolishBridge`
- 表面生成：`app/application/memory/surface_generator.py` → `SurfaceGenerator`
- 沉淀管线：`app/application/memory/consolidation_pipeline.py` → `ConsolidationPipeline`
- 缓冲管理：`app/application/memory/buffer_manager.py` → `BufferManager`
- ebbingflow 组件：`app/adapters/memory/ebbingflow/` (15 直接复用 + 3 适配)

## 目录结构

```
app/
├── domain/models/  # chat_completion, tool, turn, wake_job, outbox, sample, memory
├── domain/ports/   # model_client, outbox_store, wake_job_store, graph_store, persona_store, buffer_store, memory_engine
├── application/    # turn_runner, model_tool_loop, wake_controller, wake_planner, active_turn_gate, single_reader_gate, outbox_longpoll_service, schedule_admission_policy, wake_start_policy
├── application/memory/  # memory_engine, intent_classifier, retrieval_pipeline, polish_bridge, surface_generator, consolidation_pipeline, buffer_manager
├── adapters/http/  # chat_controller, outbox_controller
├── adapters/models/ # async_openai_client
├── adapters/tools/ # registry, tool_dispatcher, get_server_time, test_tools, wake_tools, wake_tool_definitions, memory_recall_tool
├── adapters/memory/ # sqlite_buffer_store, sqlite_graph_store, sqlite_persona_store, chroma_vector_store, ebbingflow/
├── adapters/wakeups/ # sqlite_wake_job_store
├── adapters/outbox/ # sqlite_outbox_store, in_process_notifier
├── adapters/scheduler/ # local_scheduler
└── infrastructure/ # config, logging
```

## 运行与预览

- 不可预览（后端 API 服务）
- 运行：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 测试：`uv run --no-sync pytest`（541 passed + 3 skipped）
- 配置：复制 `.env.example` 为 `.env`
- 记忆引擎：设置 `MEMORY_ENABLED=true` 并配置 8 个 MEM_* 模型参数

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
- MEMORY_ENABLED=false 时系统行为与 v2 完全一致
- vendored ebbingflow 组件不引用 vps-gateway config.py；配置通过构造注入
- Neo4j 代码全部适配为 SQLite 递归 CTE
- @4 和 @6 互斥：同一回合只读取单个区域
- @e 内容已读即删；@d 内容已读不删，仅 2am 清空
- memory_recall 工具仅在主动唤醒回合暴露

## 实现进度

v3 M0—M10 全部完成，541 个测试通过（380 v2 基线 + 161 v3 新增）+ 3 skipped。
- M0: app.py v2 组件接线补全（ToolRegistry/WakeController/WakePlanner/ModelToolLoop）
- M1: 记忆基础设施（GraphStore/PersonaStore/BufferStore 端口 + SQLite 适配器 + ChromaDB）
- M2: ebbingflow 组件 vendoring（15 直接复用 + 3 适配 + LLMBridge 解耦）
- M3: MemoryPort + MemoryEngine 骨架 + BufferManager
- M4: IntentClassifier（规则层 + LLM 兜底 + 周期校准）
- M5: @4 查询路径完整链路（R2-R7 + 超时降级 γ + 润色）
- M6: @6 无查询路径 + SurfaceGenerator + RandomSurfaceSelector
- M7: memory_recall 工具 + 主动回合工具暴露 + 新窗口衔接
- M8: ConsolidationPipeline（W1-W6 + 清理）
- M9: ContextBuilder 适配 + TurnRunner 注入 MemoryPort + app.py v3 接线
- M10: 全量回归 + 向后兼容验证
v3 架构不变量（28 条）全部实现并验证。

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
- chromadb 未安装时延迟导入报错，不影响 MEMORY_ENABLED=false 运行
- @4 检索超时后后台 task 继续运行，不阻塞主 LLM 请求（γ 降级）
- 2am 沉淀是 MemoryEngine 内部定时器，不是 WakeJob
- 记忆引擎 LLM 失败不阻塞主回合，降级为空 recall
