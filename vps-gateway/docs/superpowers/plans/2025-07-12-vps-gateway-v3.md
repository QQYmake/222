# VPS Gateway V3 记忆引擎——实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 vps-gateway v2 基线上增量引入基于 ebbingflow 的认知记忆引擎，使"沉"在聊天过程中自动浮现关联记忆。

**Architecture:** 在现有六边形架构中新增记忆引擎子系统（领域端口 + 应用层编排器 + 适配器层实现），通过 system message 文本替换注入记忆，主 LLM 无感知。ebbingflow 组件 vendoring 至适配器层，Neo4j 适配为 SQLite 递归 CTE。

**Tech Stack:** Python 3.12+ / uv / FastAPI / httpx(async) / SQLite3(短连接) / ChromaDB(嵌入式) / rank-bm25 / sentence-transformers(可选) / pytest+pytest-asyncio

## Global Constraints

- 依赖方向：适配器 → 应用层 → 领域接口 → 基础设施，禁止反向
- SQLite 每次操作短连接，不长期共享 connection
- MEMORY_ENABLED=false 时行为与 v2 完全一致
- ebbingflow vendored 组件不引用 vps-gateway config.py，配置通过构造注入
- 主 LLM 不感知记忆引擎，记忆注入是 system message 文本替换
- TDD：先写测试再实现
- API Key 不写入 Sample 或日志
- 业务代码不含硬编码绝对路径
- 单进程单实例，禁止多 worker
- 不自动 commit、push 或创建 PR

## 基线状态

- v2 测试基线：380 passed + 3 skipped (6.67s)
- Git：clean working tree
- Python 3.12.3, uv 已安装
- 缺失依赖：chromadb, rank-bm25, sentence-transformers
- ebbingflow 源码：/tmp/ebbingflow/
- app.py 接线缺失：ToolRegistry/WakeController/WakePlanner/ModelToolLoop 未注入

---

## 数据流图

### 数据流 1：用户回合 @6 无查询路径

```
前端 POST /v1/chat/completions
  → ChatController 校验
  → TurnRunner.run_user_turn()
  → MemoryEngine.recall(trigger, raw_messages)
      → IntentClassifier.classify(text) → no_query, confidence=0.9, source=rule
      → BufferManager.read_surface() → @e 条目或 None (已读即删)
      → MemoryRecall { mode="no_query", text=@e内容或"" }
  → SampleReader.read_all() → AllSamples
  → ContextBuilder.build(samples, trigger, memory_recall)
      → <memories> 块使用 memory_recall.text (可为空)
  → ModelToolLoop.run() → 主 LLM
  → MemoryEngine.after_turn(raw_messages, response, turn_id)
      → BufferManager.append_raw(user, content, platform, turn_id)
      → BufferManager.append_raw(assistant, content, platform, turn_id)
  → 返回 response
```

### 数据流 2：用户回合 @4 查询路径

```
前端 POST /v1/chat/completions
  → ChatController 校验
  → TurnRunner.run_user_turn()
  → MemoryEngine.recall(trigger, raw_messages)
      → IntentClassifier.classify(text) → query, confidence=0.85, intent_type="fact"
      → _run_query_path():
          → asyncio.create_task(_execute_retrieval_pipeline(...))
          → 等待 20s:
              [后台任务]:
                R2: MEM_EMBED.embed(text) → query_vector
                R3: KnowledgeEngine + VectorRetriever + BM25 + EventRepository → candidates
                R4: HybridScorer.score(candidates, "fact") → ranked
                R5: MEM_GEN.generate(ranked, raw_messages) → raw_d
                R7: MEM_POLISH.polish(raw_d) → polished_d
                → BufferManager.write_recall(...)
              [前台]:
                完成 → BufferManager.read_recall_latest() → MemoryRecall { mode="query", text=polished_d }
                超时 → MemoryRecall { mode="degraded", text="" } (后台任务继续)
  → SampleReader.read_all() → AllSamples
  → ContextBuilder.build(samples, trigger, memory_recall)
      → <memories> 块使用 memory_recall.text (degraded 时为空)
  → ModelToolLoop.run() → 主 LLM
  → MemoryEngine.after_turn(raw_messages, response, turn_id)
  → 返回 response
```

### 数据流 3：新窗口衔接

```
前端 POST /v1/chat/completions (Header: X-Memory-Mode: new_window)
  → ChatController 校验
  → TurnRunner.run_user_turn()
  → MemoryEngine.recall(trigger, raw_messages)
      → 检测 X-Memory-Mode: new_window
      → BufferManager.read_recent_recall(N=15) → 最近 15 条 @d 内容
      → 拼接为连续文本
      → MemoryRecall { mode="new_window", text=拼接文本 }
  → SampleReader.read_all()
  → ContextBuilder.build(samples, trigger, memory_recall)
  → ModelToolLoop.run() → 主 LLM
  → after_turn(...)
  → 返回 response
```

### 数据流 4：主动唤醒 + memory_recall 工具

```
WakeScheduler 扫描到期任务
  → WakeController → ActiveTurnGate.try_acquire()
  → TurnRunner.run_wake_turn(WakeTrigger)
  → 构造伪用户输入 (timer_user_message)
  → MemoryEngine.recall(trigger, [pseudo_input])
      → IntentClassifier → no_query (明确为无需查询状态)
      → BufferManager.read_surface() → @e 内容
      → MemoryRecall { mode="no_query", text=@e内容 }
  → SampleReader.read_all()
  → ContextBuilder.build(samples, trigger, memory_recall)
  → ModelToolLoop.run(prepared, extra_tools=[memory_recall_tool])
      → 主 LLM 收到 system message (含 @e 浮现)
      → 主 LLM 可选择调用 memory_recall 工具:
          → tool_call(memory_recall, { query: "..." })
          → MemoryEngine.recall_as_tool(query)
              → _execute_retrieval_pipeline(...) (完整 @4 流程)
              → 返回润色后的 @d 内容
          → tool result 回灌主 LLM
          → 主 LLM 继续生成
      → 或不调用工具，直接生成回复
  → <NO_MESSAGE> → 不写 Outbox
  → 普通文本 → Outbox enqueue + notify
  → after_turn([pseudo_input], response, turn_id)
  → release Gate
```

### 数据流 5：@e 周期生成

```
SurfaceGenerator (独立 asyncio 后台任务)
  循环:
    1. sleep(900s ± random(0, 300s))  # ~15min + 随机感
    2. BufferManager.scan_recall_for_surface() → @d 候选列表
    3. 候选为空 → continue (surface_skipped 日志)
    4. RandomSurfaceSelector.select(candidates) → 1-3 条
    5. random() < FEEL_THRESHOLD (默认 0.3)?
       → impression: 生成印象浮现
       → association: 生成关联记忆
    6. MEM_SURF.generate(selected, surface_type) → raw_surface
    7. MEM_POLISH.polish(raw_surface, "surface") → polished_surface
    8. BufferManager.write_surface(polished_surface, raw_surface, surface_type, source_ids)
```

### 数据流 6：2am 沉淀管线

```
ConsolidationPipeline (每日 02:00 触发, consolidation_id 关联)
  W1: EventExtractor.extract(@a + @d) → events
     (MEM_EXTRACT, 复用 ebbingflow)
  W2: PersonaManager.observe(@a, events) → persona_snapshot
     (MEM_PERSONA, 复用 ebbingflow)
  W3: 规则校验 + 可选 LLM 轻量校验
  W4: SagaManager.cluster(events, episodes) → saga_updates
     (MEM_SAGA, 复用 ebbingflow)
  W5: VectorStorer.store_batch(@a messages) → ChromaDB
     (MEM_EMBED, 复用 ebbingflow)
  W6: PolishBridge.polish(persona_snapshot) + PolishBridge.polish(saga_updates)
  持久化:
     → GraphStore.write_events / write_episodes / write_sagas
     → PersonaStore.write
  清理:
     → BufferManager.clear_raw() (清空 @a)
     → BufferManager.clear_recall() (清空 @d)
```

### 数据流 7：ebbingflow 组件复用映射

```
直接复用 (15个, 零改动或仅 import 路径调整):
  event/slots.py        → adapters/memory/ebbingflow/event_slots.py
  scoring/hybrid_scorer.py → adapters/memory/ebbingflow/hybrid_scorer.py
  event/extractor.py    → adapters/memory/ebbingflow/event_extractor.py
  integration/saga_manager.py → adapters/memory/ebbingflow/saga_manager.py
  integration/episode_manager.py → adapters/memory/ebbingflow/episode_manager.py
  identity/canonical.py → adapters/memory/ebbingflow/identity_canonical.py
  identity/resolver.py  → adapters/memory/ebbingflow/identity_resolver.py
  identity/state_reducer.py → adapters/memory/ebbingflow/identity_state_reducer.py
  identity/schema.py    → adapters/memory/ebbingflow/identity_schema.py
  vector/storer.py      → adapters/memory/ebbingflow/vector_storer.py
  vector/retriever.py   → adapters/memory/ebbingflow/vector_retriever.py
  sql/event_repository.py → adapters/memory/ebbingflow/sql_event_repository.py

适配 (3个, 只改存储层和配置注入):
  sql/pool.py           → adapters/memory/ebbingflow/sql_pool.py (去 asyncpg, 保留 SQLite)
  bridge/llm.py         → adapters/memory/ebbingflow/llm_bridge.py (解耦 token_monitor, 改可选回调)
  knowledge_engine.py   → adapters/memory/ebbingflow/knowledge_engine.py (Neo4j→SQLiteGraphStore)
  identity/manager.py   → adapters/memory/ebbingflow/persona_manager.py (Neo4j→SQLitePersonaStore)

新开发:
  domain/models/memory.py (MemoryRecall, MemorySurface, IntentResult, RecallMode)
  domain/ports/memory_engine.py (MemoryPort ABC)
  domain/ports/graph_store.py (GraphStore ABC)
  domain/ports/persona_store.py (PersonaStore ABC)
  domain/ports/buffer_store.py (BufferStore ABC)
  application/memory/memory_engine.py (MemoryEngine)
  application/memory/buffer_manager.py (BufferManager)
  application/memory/intent_classifier.py (IntentClassifier)
  application/memory/surface_generator.py (SurfaceGenerator)
  application/memory/surface_selector.py (SurfaceSelector + RandomSurfaceSelector)
  application/memory/consolidation_pipeline.py (ConsolidationPipeline)
  application/memory/polish_bridge.py (PolishBridge)
  adapters/memory/sqlite_graph_store.py (SqliteGraphStore)
  adapters/memory/sqlite_persona_store.py (SqlitePersonaStore)
  adapters/memory/sqlite_buffer_store.py (SqliteBufferStore)
  adapters/memory/memory_recall_tool.py (MemoryRecallTool)
  infrastructure/memory_config.py (MemoryConfig, 8 模型配置)
```

---

## 反馈点

1. **M0 完成**：app.py v2 组件接线补全，ToolRegistry/WakeController/WakePlanner 运行
2. **M2 完成**：ebbingflow 组件 vendoring 完成，各模块可导入、基本功能可用
3. **M5 完成**：@4 查询路径完整闭环（含超时降级 γ）
4. **M6 完成**：@6 无查询路径 + @e 周期生成器运行
5. **M8 完成**：2am 沉淀管线完整执行并清空缓冲
6. **M9 完成**：ContextBuilder 替换 `<memories>` + TurnRunner 注入 MemoryPort + app.py 完整接线
7. **M10 完成**：全量回归不下降、MEMORY_ENABLED=false 向后兼容、真实 API 冒烟、操作性冒烟

---

## M0: app.py v2 组件接线补全

ebbingflow 来源：
- 无（纯 v2 修复）

修改位置：
- 修改: `app/application/app.py` (create_app)
- 修改: `app/application/turn_runner.py` (增加 tool_registry 和 model_tool_loop 参数)
- 修改: `app/adapters/tools/registry.py` (增加 register_for_wake_only)
- 新增: `tests/integration/test_m0_app_wiring.py`

数据输入：
- Config (环境变量加载)
- v2 既有组件（ToolRegistry, ToolDispatcher, ModelToolLoop, WakeController, WakePlanner, ActiveTurnGate, WakeJobStore）

处理指令：
1. 在 create_app 中创建 ToolRegistry，注册 VPS 工具（get_server_time, schedule_wakeup, list_wakeups, cancel_wakeup, 测试工具按配置）
2. 创建 ToolDispatcher，注入 tool_registry
3. 创建 ModelToolLoop，注入 model_client 和 dispatcher
4. 创建 ActiveTurnGate
5. 创建 SQLiteWakeJobStore
6. 创建 WakeStartPolicy
7. 创建 WakeController，注入 store, gate, policy, turn_runner
8. 创建 WakePlanner，注入 store 和 config
9. TurnRunner 构造增加 tool_registry 和 model_tool_loop 可选参数
10. TurnRunner.run_user_turn 增加工具循环：build → ModelToolLoop.run(ctx, tools=schemas)
11. TurnRunner.run_wake_turn 增加工具循环
12. lifespan startup 增加：WakeJobStore.recover_after_restart() + WakePlanner.start() + 非阻塞扫描循环
13. lifespan shutdown 增加：WakePlanner.stop()
14. ToolRegistry 增加 register_for_wake_only(tool) 方法
15. MEMORY_ENABLED=false 时行为不变

数据输出：
- app.py 启动后 ToolRegistry 非空
- WakeController/WakePlanner 运行
- 主动回合经过 ModelToolLoop
- v2 全量回归通过

接口关系：
Config → create_app → TurnRunner(+tool_registry, +model_tool_loop) + WakeController + WakePlanner

测试：
- test_app_wiring_tool_registry_non_empty: create_app 后 app.state.tool_registry.schemas() 非空
- test_app_wiring_wake_controller_started: lifespan 启动后 wake_controller 存在
- test_app_wiring_wake_planner_started: lifespan 启动后 wake_planner 存在
- test_app_wiring_model_tool_loop_in_turn_runner: TurnRunner 持有 model_tool_loop
- test_register_for_wake_only: register_for_wake_only 标记的工具仅在 wake 回合暴露
- 全量回归不下降

---

## M1: 记忆基础设施

ebbingflow 来源：
- 新开发: GraphStore/PersonaStore/BufferStore 端口
- 新开发: SqliteGraphStore/SqlitePersonaStore/SqliteBufferStore 适配器

修改位置：
- 新增: `app/domain/ports/graph_store.py`
- 新增: `app/domain/ports/persona_store.py`
- 新增: `app/domain/ports/buffer_store.py`
- 新增: `app/domain/models/memory.py`
- 新增: `app/adapters/memory/sqlite_graph_store.py`
- 新增: `app/adapters/memory/sqlite_persona_store.py`
- 新增: `app/adapters/memory/sqlite_buffer_store.py`
- 修改: `pyproject.toml` (增加 chromadb, rank-bm25 依赖)
- 新增: `tests/unit/memory/test_sqlite_graph_store.py`
- 新增: `tests/unit/memory/test_sqlite_persona_store.py`
- 新增: `tests/unit/memory/test_sqlite_buffer_store.py`

数据输入：
- SQLite 文件路径（配置注入）
- 表结构 DDL（架构文档 5.4-5.8 节）

处理指令：
1. 定义 RecallEntry, SurfaceEntry 数据类（domain/models/memory.py）
2. 定义 MemoryRecall, MemorySurface, IntentResult 数据类
3. 定义 GraphStore ABC: query_events(entity, max_hops) / query_episodes(intent_type) / query_sagas(status) / query_plans() / write_event / write_episode / write_saga
4. 定义 PersonaStore ABC: read_persona(actor_id) / write_persona / write_observation / read_observations
5. 定义 BufferStore ABC: append_raw / read_recent_recall / write_recall / read_recall_latest / scan_recall_for_surface / write_surface / read_surface / clear_raw / clear_recall / read_all_raw / read_all_recall
6. SqliteGraphStore: 创建 events/relations/episodes/sagas 表，递归 CTE 查询
7. SqlitePersonaStore: 创建 persona_profiles/persona_observations 表
8. SqliteBufferStore: 创建 buffer_raw/buffer_recall/buffer_surface 表
9. 每次操作短连接，不共享 connection
10. read_surface 读取后删除该行（已读即删）
11. read_recall_latest 标记 read_at 但不删除

数据输出：
- 表创建成功，CRUD 操作正确
- 递归 CTE 查询返回正确结果
- @e 读取后删除

接口关系：
MemoryEngine → BufferStore/GraphStore/PersonaStore (端口) → SqliteXxxStore (适配器)

测试：
- test_graph_store_table_creation: 表存在
- test_graph_store_write_and_query_event: 写入事件并查询
- test_graph_store_recursive_cte: 多跳关系遍历
- test_persona_store_crud: 人格数据 CRUD
- test_buffer_store_raw_append_and_clear: @a 追加和清空
- test_buffer_store_recall_write_and_read: @d 写入、读取、不删除
- test_buffer_store_surface_read_and_delete: @e 读取后删除
- test_buffer_store_read_recent_recall: 最近 N 条 @d

---

## M2: ebbingflow 组件 vendoring

ebbingflow 来源：
- 直接复用(15): event_slots, hybrid_scorer, event_extractor, saga_manager, episode_manager, identity_canonical, identity_resolver, identity_state_reducer, identity_schema, vector_storer, vector_retriever, sql_event_repository
- 适配(3): sql_pool(去 asyncpg), llm_bridge(解耦 token_monitor), knowledge_engine(Neo4j→SQLite)
- 适配(1): persona_manager(Neo4j→SQLite)
- 新开发: ebbingflow/__init__.py

修改位置：
- 新增: `app/adapters/memory/ebbingflow/` 目录及所有模块文件
- 修改: `pyproject.toml` (增加 openai 依赖)
- 新增: `tests/unit/memory/test_ebbingflow_vendoring.py`

数据输入：
- ebbingflow 源码 (/tmp/ebbingflow/)
- vps-gateway 配置注入（MemoryConfig）

处理指令：
1. 复制 12 个直接复用模块，调整 import 路径为相对导入
2. 适配 sql_pool: 去掉 asyncpg 分支和 `from config import` 语句，SQLite 路径保留，db_path 通过参数注入
3. 适配 llm_bridge: 去掉 `from config import LLMConfig`，改为构造注入 base_url/api_key/model/temperature/timeout；去掉 `from core.monitoring import token_monitor`，改为可选回调 `on_usage: Callable | None`
4. 适配 knowledge_engine: 去掉 `from neo4j import`，GraphStore 端口注入；去掉 `from config import`，配置注入；`infer_query_intent()` 静态方法直接复用
5. 适配 persona_manager: 去掉 `from neo4j import`，PersonaStore 端口注入；去掉 `from config import`，配置注入
6. 所有模块不引用 vps-gateway config.py
7. 创建 __init__.py 统一导出

数据输出：
- 各模块可导入
- 直接复用模块基本功能可用
- 适配模块存储层行为正确

接口关系：
MemoryEngine → ebbingflow 组件 (适配器层内部) → GraphStore/PersonaStore/BufferStore 端口

测试：
- test_import_all_modules: 所有 vendored 模块可导入
- test_llm_bridge_no_config_dependency: LLMBridge 构造不引用 config.py
- test_llm_bridge_optional_callback: on_usage=None 时不报错
- test_sql_pool_no_asyncpg: sql_pool 不引用 asyncpg
- test_knowledge_engine_no_neo4j: knowledge_engine 不引用 neo4j
- test_knowledge_engine_infer_query_intent: 静态方法行为不变
- test_persona_manager_no_neo4j: persona_manager 不引用 neo4j
- test_hybrid_scorer_basic: HybridScorer 打分功能可用
- test_event_extractor_basic: EventExtractor 可构造

---

## M3: MemoryPort + MemoryEngine 骨架 + BufferManager

ebbingflow 来源：
- 新开发: MemoryPort, MemoryEngine, BufferManager

修改位置：
- 新增: `app/domain/ports/memory_engine.py` (MemoryPort ABC)
- 新增: `app/application/memory/__init__.py`
- 新增: `app/application/memory/memory_engine.py`
- 新增: `app/application/memory/buffer_manager.py`
- 新增: `tests/unit/memory/test_memory_engine_skeleton.py`
- 新增: `tests/unit/memory/test_buffer_manager.py`

数据输入：
- BufferStore 端口实例
- Trigger + raw_messages

处理指令：
1. 定义 MemoryPort ABC: recall / after_turn / start_background_tasks / stop_background_tasks / recall_as_tool
2. MemoryEngine 构造注入: buffer_store, graph_store, persona_store, intent_classifier(可选), llm_bridges(可选), polish_bridge(可选), config
3. MemoryEngine.recall: 占位实现，返回 MemoryRecall(mode="no_query", text="")
4. MemoryEngine.after_turn: 调用 BufferManager.append_raw(user+assistant)
5. MemoryEngine.start_background_tasks: 占位
6. MemoryEngine.stop_background_tasks: 占位
7. MemoryEngine.recall_as_tool: 占位
8. BufferManager 封装 BufferStore 操作，提供高级 API
9. MEMORY_ENABLED=false 时 MemoryEngine 可不被创建

数据输出：
- recall/after_turn 签名正确
- 缓冲 CRUD 正确

接口关系：
TurnRunner → MemoryPort → MemoryEngine → BufferManager → BufferStore

测试：
- test_memory_port_is_abstract: MemoryPort 不可直接实例化
- test_memory_engine_recall_returns_no_query: recall 返回 no_query MemoryRecall
- test_memory_engine_after_turn_appends_raw: after_turn 追加 @a
- test_buffer_manager_append_raw: 追加 @a
- test_buffer_manager_write_and_read_recall: 写入和读取 @d
- test_buffer_manager_write_and_read_surface: 写入和读取 @e (读取后删除)
- test_buffer_manager_clear: 清空 @a/@d

---

## M4: IntentClassifier

ebbingflow 来源：
- 新开发: IntentClassifier
- 直接复用: knowledge_engine.infer_query_intent (静态方法)

修改位置：
- 新增: `app/application/memory/intent_classifier.py`
- 新增: `tests/unit/memory/test_intent_classifier.py`

数据输入：
- 用户最新消息文本
- LLMBridge(MEM_INTENT) 可选
- 配置: confidence_threshold, calibration_interval

处理指令：
1. 规则层: 匹配查询关键词模式 (查一下/记得XX吗/之前/上一次/XX天前/XX时候/那天)
2. 规则层: 匹配无查询模式 (纯闲聊/陈述/感叹)
3. confidence >= 0.8 → 直接返回 (source=rule)
4. confidence < 0.8 → LLM 兜底 (source=llm_fallback)
5. LLM 失败 → 默认 no_query (source=default)
6. 周期校准: 每 N 条消息随机抽 1 条，跑 LLM 分类，记录偏差（不阻塞路由）
7. 返回 IntentResult { label, confidence, matched_patterns, source, intent_type }

数据输出：
- IntentResult 正确分类

接口关系：
MemoryEngine → IntentClassifier → LLMBridge(MEM_INTENT)

测试：
- 12.2 验收矩阵全部覆盖
- test_rule_query_pattern: "查一下上次聊的" → query, confidence>=0.8, source=rule
- test_rule_no_query_pattern: "今天天气真好" → no_query, confidence>=0.8, source=rule
- test_llm_fallback: 模糊文本触发 LLM 兜底
- test_default_fallback: 规则和 LLM 均失败 → no_query
- test_calibration: 第 N 条消息触发校准（不阻塞）
- test_calibration_mismatch: 校准发现偏差记录日志

---

## M5: @4 查询路径完整链路

ebbingflow 来源：
- 直接复用: hybrid_scorer, vector_retriever, sql_event_repository
- 适配: knowledge_engine, llm_bridge
- 新开发: PolishBridge, 检索管线编排

修改位置：
- 新增: `app/application/memory/polish_bridge.py`
- 修改: `app/application/memory/memory_engine.py` (实现 _run_query_path, _execute_retrieval_pipeline)
- 新增: `tests/unit/memory/test_query_path.py`
- 新增: `tests/unit/memory/test_polish_bridge.py`

数据输入：
- IntentResult + raw_messages
- LLMBridge(MEM_EMBED/MEM_GEN/MEM_POLISH)
- GraphStore, VectorRetriever, BM25, EventRepository

处理指令：
1. _run_query_path:
   a. asyncio.create_task(_execute_retrieval_pipeline(...))
   b. asyncio.wait_for(task, timeout=20s)
   c. 超时 → MemoryRecall(mode="degraded", text="")，task 后台继续
   d. 完成 → BufferManager.read_recall_latest() → MemoryRecall(mode="query", text=polished)
2. _execute_retrieval_pipeline:
   a. R2: MEM_EMBED.embed(text) → query_vector (失败跳过向量轨道)
   b. R3: 并行多轨检索 (KnowledgeEngine/VectorRetriever/BM25/EventRepository)
   c. R4: HybridScorer.score(candidates, intent_type) → ranked
   d. R5: MEM_GEN.generate(ranked, raw_messages) → raw_d (失败返回降级)
   e. R7: PolishBridge.polish(raw_d) → polished_d (失败用 raw_d)
   f. BufferManager.write_recall(trigger_id, polished_d, raw_d, metadata)
3. PolishBridge:
   a. 构造润色 prompt
   b. MEM_POLISH.generate(raw_text, context_type) → polished_text
   c. 失败返回 raw_text

数据输出：
- MemoryRecall(mode="query"/"degraded")
- @d 缓存写入

接口关系：
MemoryEngine._run_query_path → _execute_retrieval_pipeline → ebbingflow 组件 + PolishBridge

测试：
- 12.3 验收矩阵全部覆盖
- test_query_path_normal: 正常完成 (<20s), mode=query, text 非空
- test_query_path_timeout: 超时, mode=degraded, text=""
- test_query_path_timeout_then_write: 超时后 @d 最终写入
- test_query_path_gen_failed: MEM_GEN 失败 → degraded
- test_query_path_embed_failed: 向量化失败 → 检索继续
- test_query_path_graph_failed: 图谱查询失败 → 检索继续
- test_hybrid_scorer_ranking: 候选按分数降序
- test_polish_failed: 润色失败用 raw_content

---

## M6: @6 无查询路径 + SurfaceGenerator

ebbingflow 来源：
- 新开发: SurfaceGenerator, SurfaceSelector, RandomSurfaceSelector
- 直接复用: llm_bridge (MEM_SURF)

修改位置：
- 新增: `app/application/memory/surface_generator.py`
- 新增: `app/application/memory/surface_selector.py`
- 修改: `app/application/memory/memory_engine.py` (实现 _run_surface_path)
- 修改: `app/application/memory/memory_engine.py` (实现 start_background_tasks/stop_background_tasks)
- 新增: `tests/unit/memory/test_surface_path.py`
- 新增: `tests/unit/memory/test_surface_generator.py`

数据输入：
- 无（独立后台任务）
- LLMBridge(MEM_SURF/MEM_POLISH)
- BufferStore

处理指令：
1. _run_surface_path:
   a. BufferManager.read_surface() → @e 条目或 None (已读即删)
   b. 有条目 → MemoryRecall(mode="no_query", text=条目.content)
   c. 无条目 → MemoryRecall(mode="no_query", text="")
2. SurfaceGenerator (后台 asyncio 循环):
   a. sleep(SURFACE_INTERVAL ± random_jitter)
   b. scan_recall_for_surface() → @d 候选
   c. 候选为空 → continue (surface_skipped 日志)
   d. RandomSurfaceSelector.select(candidates) → 1-3 条
   e. random() < FEEL_THRESHOLD → impression, else → association
   f. MEM_SURF.generate(selected, surface_type) → raw_surface
   g. PolishBridge.polish(raw_surface, "surface") → polished_surface
   h. write_surface(polished_surface, raw_surface, surface_type, source_ids)
   i. 异常 → 日志记录，下一周期继续
3. RandomSurfaceSelector: 纯随机选 1-3 条
4. SurfaceSelector ABC: select(candidates) → list[RecallEntry]
5. start_background_tasks: 启动 SurfaceGenerator asyncio.Task
6. stop_background_tasks: 取消 asyncio.Task

数据输出：
- MemoryRecall(mode="no_query")
- @e 浮现区写入

接口关系：
MemoryEngine._run_surface_path → BufferManager.read_surface
SurfaceGenerator → BufferManager + LLMBridge(MEM_SURF) + PolishBridge

测试：
- 12.4, 12.5 验收矩阵全部覆盖
- test_surface_path_with_content: @e 有内容 → mode=no_query, text=@e 内容
- test_surface_path_empty: @e 为空 → mode=no_query, text=""
- test_surface_read_then_delete: read_surface 第二次返回 None
- test_surface_generator_with_candidates: @d 有内容 → @e 写入
- test_surface_generator_empty: @d 为空 → surface_skipped
- test_feel_threshold_zero: FEEL=0.0 → 全 association
- test_feel_threshold_one: FEEL=1.0 → 全 impression
- test_surface_jitter: 两次间隔不完全相同
- test_surface_generator_exception_recovery: 异常后下一周期正常

---

## M7: memory_recall 工具 + 主动回合工具暴露 + 新窗口衔接

ebbingflow 来源：
- 新开发: MemoryRecallTool

修改位置：
- 新增: `app/adapters/memory/memory_recall_tool.py`
- 修改: `app/adapters/tools/registry.py` (register_for_wake_only 已在 M0 添加)
- 修改: `app/application/memory/memory_engine.py` (实现 recall_as_tool)
- 修改: `app/application/memory/memory_engine.py` (实现 new_window 路径)
- 修改: `app/application/turn_runner.py` (run_wake_turn 增加额外工具)
- 新增: `tests/unit/memory/test_memory_recall_tool.py`
- 新增: `tests/unit/memory/test_new_window.py`

数据输入：
- 工具调用参数 { query: string }
- X-Memory-Mode 请求头

处理指令：
1. MemoryRecallTool (implements ToolExecutor):
   a. execute(arguments={query}, context) → MemoryEngine.recall_as_tool(query)
   b. recall_as_tool 内部: 构造 IntentResult(label="query"), 执行 _execute_retrieval_pipeline, 读取 @d 最新
   c. 返回 ToolResult(content=润色后文本)
   d. 超时沿用 v2 TOOL_TIMEOUT_SECONDS (15s)
2. ToolRegistry.register_for_wake_only(tool): 标记仅在主动唤醒回合暴露
3. TurnRunner.run_wake_turn: ModelToolLoop.run(ctx, tools=base_schemas + wake_only_schemas)
4. TurnRunner.run_user_turn: ModelToolLoop.run(ctx, tools=base_schemas) (不含 wake_only)
5. MemoryEngine.recall: 检测 X-Memory-Mode: new_window
   a. BufferManager.read_recent_recall(N=15)
   b. 拼接为连续文本
   c. MemoryRecall(mode="new_window", text=拼接文本)

数据输出：
- ToolResult (润色后的 @d 文本)
- MemoryRecall(mode="new_window")

接口关系：
ModelToolLoop → MemoryRecallTool → MemoryEngine.recall_as_tool → _execute_retrieval_pipeline
TurnRunner → MemoryEngine.recall (new_window) → BufferManager.read_recent_recall

测试：
- 12.6, 12.7 验收矩阵全部覆盖
- test_memory_recall_tool_schema: 工具 Schema 正确
- test_wake_turn_exposes_memory_recall: 主动回合工具列表包含 memory_recall
- test_user_turn_no_memory_recall: 用户回合工具列表不包含 memory_recall
- test_memory_recall_tool_executes: 工具调用执行 @4 流程
- test_memory_recall_tool_timeout: 工具超时返回 tool_timeout
- test_new_window_mode: X-Memory-Mode → mode=new_window, text=最近15条
- test_new_window_insufficient: @d 不足 15 条 → 拼接全部
- test_new_window_empty: @d 为空 → text=""
- test_new_window_then_normal: 新窗口后恢复正常路径

---

## M8: ConsolidationPipeline

ebbingflow 来源：
- 直接复用: event_extractor, saga_manager, episode_manager, vector_storer
- 适配: persona_manager, llm_bridge

修改位置：
- 新增: `app/application/memory/consolidation_pipeline.py`
- 修改: `app/application/memory/memory_engine.py` (start_background_tasks 增加 2am 定时器)
- 新增: `tests/unit/memory/test_consolidation_pipeline.py`

数据输入：
- 无（每日 02:00 触发）
- LLMBridge(MEM_EXTRACT/MEM_PERSONA/MEM_SAGA/MEM_EMBED/MEM_POLISH)
- BufferStore, GraphStore, PersonaStore

处理指令：
1. W1: BufferManager.read_all_raw() + read_all_recall() → EventExtractor.extract() → events
   (MEM_EXTRACT, 复用 ebbingflow)
2. W2: PersonaManager.observe(@a, events) → persona_snapshot
   (MEM_PERSONA, 复用 ebbingflow)
3. W3: 规则校验 (去重、冲突检测、字段标准化) + 可选 LLM 轻量校验
4. W4: SagaManager.cluster(events, episodes) → saga_updates
   (MEM_SAGA, 复用 ebbingflow)
5. W5: VectorStorer.store_batch(@a messages) → ChromaDB
   (MEM_EMBED, 复用 ebbingflow)
6. W6: PolishBridge.polish(persona_snapshot) + PolishBridge.polish(saga_updates)
7. 持久化: GraphStore.write_events/write_episodes/write_sagas, PersonaStore.write
8. 清理: BufferManager.clear_raw() + clear_recall()
9. W1 失败 → 中断管线，不清空 @a/@d
10. W2-W6 部分失败 → 已完成步骤持久化，后续跳过，清空 @a/@d
11. 2am 定时器: 每日 MEM_CONSOLIDATION_TIME 触发，不受 WakeJob 时间窗口约束
12. consolidation_id 关联所有日志

数据输出：
- 事件/人格/Saga 持久化
- @a/@d 清空

接口关系：
ConsolidationPipeline → EventExtractor + PersonaManager + SagaManager + VectorStorer + PolishBridge + BufferManager + GraphStore + PersonaStore

测试：
- 12.8 验收矩阵全部覆盖
- test_consolidation_normal: 正常沉淀，events 写入，persona 更新，saga 归并
- test_consolidation_clears_buffers: @a/@d 清空
- test_consolidation_w1_failed: W1 失败 → 中断，不清空
- test_consolidation_w4_failed: W4 失败 → W1-W3 已持久化，清空
- test_consolidation_then_retrieval: 沉淀后新事件可在 @4 检索到
- test_consolidation_vectorization: ChromaDB 可检索到新向量

---

## M9: ContextBuilder 适配 + TurnRunner 适配 + AppFactory v3 接线 + 种子数据导入

ebbingflow 来源：
- 无（纯 vps-gateway 集成层）

修改位置：
- 修改: `app/domain/models/context_builder.py` (build 增加 memory_recall 参数)
- 修改: `app/application/turn_runner.py` (注入 MemoryPort, recall/after_turn 调用点)
- 修改: `app/application/app.py` (V3 完整接线)
- 新增: `app/infrastructure/memory_config.py` (MemoryConfig, 8 模型配置)
- 新增: `tests/unit/memory/test_context_builder_v3.py`
- 新增: `tests/unit/memory/test_turn_runner_v3.py`
- 新增: `tests/integration/test_m9_app_v3_wiring.py`

数据输入：
- AllSamples + Trigger + MemoryRecall (可选)
- MemoryConfig (8 模型配置)
- 种子数据 (memories.sample.json)

处理指令：
1. ContextBuilder.build(samples, trigger, memory_recall=None):
   a. 沿用 v2 步骤 1-4
   b. IF memory_recall is not None AND memory_recall.text != "":
        用 memory_recall.text 替换 <memories> 块
      ELSE:
        沿用 v2 从 samples.memories 渲染
   c. memory_recall=None 时行为与 v2 完全一致
2. TurnRunner 构造增加 memory_port 可选参数
3. TurnRunner.run_user_turn:
   a. IF memory_port: memory_port.recall(trigger, raw_messages) → MemoryRecall
   b. context_builder.build(samples, trigger, memory_recall)
   c. ModelToolLoop.run()
   d. IF memory_port: memory_port.after_turn(raw_messages, response, turn_id)
4. TurnRunner.run_wake_turn:
   a. 构造伪用户输入
   b. IF memory_port: memory_port.recall(trigger, [pseudo_input])
   c. context_builder.build(samples, trigger, memory_recall)
   d. ModelToolLoop.run(prepared, extra_tools=[memory_recall_tool])
   e. Outbox 处理
   f. IF memory_port: memory_port.after_turn([pseudo_input], response, turn_id)
5. MemoryConfig: 8 模型独立配置 + 全局变量
6. AppFactory v3:
   a. IF MEMORY_ENABLED=true: 创建全部记忆组件，注入 TurnRunner
   b. IF MEMORY_ENABLED=false: TurnRunner 不注入 memory_port
   c. lifespan startup: MemoryEngine.start_background_tasks() + 种子数据导入
   d. lifespan shutdown: MemoryEngine.stop_background_tasks()
7. 种子数据导入: 若图谱为空，从 memories.sample.json 导入

数据输出：
- <memories> 块来源切换
- MEMORY_ENABLED=false 时行为不变
- 种子数据导入

接口关系：
AppFactory → MemoryEngine(全组件) → TurnRunner(+memory_port) → ContextBuilder(+memory_recall)

测试：
- 12.9 验收矩阵覆盖
- test_context_builder_with_memory_recall: memory_recall.text 替换 <memories>
- test_context_builder_without_memory_recall: memory_recall=None 时沿用 v2
- test_context_builder_empty_memory_recall: memory_recall.text="" 时沿用 v2
- test_turn_runner_user_turn_with_memory: recall + after_turn 调用
- test_turn_runner_wake_turn_with_memory: recall + extra_tools + after_turn
- test_turn_runner_no_memory_port: memory_port=None 时行为不变
- test_app_factory_memory_enabled: MEMORY_ENABLED=true 时 memory_port 注入
- test_app_factory_memory_disabled: MEMORY_ENABLED=false 时无 memory_port
- test_seed_data_import: 首次启动图谱非空

---

## M10: 全量回归 + 真实 API 冒烟 + 操作性冒烟

ebbingflow 来源：
- 无（验证阶段）

修改位置：
- 无（纯验证）

数据输入：
- 全量测试套件
- .env 真实凭据（8 模型配置）

处理指令：
1. 运行全量回归: `UPSTREAM_BASE_URL="" uv run pytest`
2. 验证 v2 基线不下降 (380 passed + 3 skipped)
3. 验证所有 v3 新增测试通过
4. MEMORY_ENABLED=false 回归验证
5. 真实 API 冒烟（需 .env 配置）:
   a. MEMORY_ENABLED=true 启动服务
   b. 发送 no_query 消息 → 验证 @6 路径
   c. 发送 query 消息 → 验证 @4 路径
   d. 验证 @e 生成
   e. 验证 memory_recall 工具
6. 操作性冒烟:
   a. 从零启动服务
   b. 健康检查
   c. 重启后数据持久
7. 凭据清理检查: grep 搜索代码和日志无真实 Key
8. ebbingflow vendored 组件不引用 config.py 检查
9. 逐项对照 12.1-12.10 验收矩阵

数据输出：
- 测试通过数、失败数、耗时
- 验收追踪矩阵（12.1-12.10 每行对应测试和实际证据）
- 冒烟测试结果

测试：
- 全量回归
- 12.10 可迁移性验收
- 凭据安全检查

---

## 验收追踪矩阵

| 架构验收编号 | 对应测试文件/测试名 | 预期结果 |
|---|---|---|
| 12.1.1 | test_m0_app_wiring::test_tool_registry_non_empty | ToolRegistry schemas 非空 |
| 12.1.2 | test_m0_app_wiring::test_wake_controller_started | wake_controller 存在 |
| 12.1.3 | test_m0_app_wiring::test_wake_planner_started | wake_planner 存在 |
| 12.1.4 | test_m0_app_wiring::test_model_tool_loop_in_turn_runner | TurnRunner 持有 model_tool_loop |
| 12.1.5 | 全量回归 | v2 基线不下降 |
| 12.2.1-6 | test_intent_classifier | 意图分类全部场景 |
| 12.3.1-8 | test_query_path | @4 路径全部场景 |
| 12.4.1-4 | test_surface_path | @6 路径全部场景 |
| 12.5.1-6 | test_surface_generator | @e 生成全部场景 |
| 12.6.1-4 | test_new_window | 新窗口全部场景 |
| 12.7.1-5 | test_memory_recall_tool | memory_recall 工具全部场景 |
| 12.8.1-6 | test_consolidation_pipeline | 沉淀管线全部场景 |
| 12.9.1-6 | test_m9_app_v3_wiring + 全量回归 | 集成与回归 |
| 12.10.1-4 | M10 静态检查 | 可迁移性 |

---

## 一致性自检

- [x] 计划只包含伪代码、数据流、接口与验收，不含完整实现代码
- [x] 每个 Task 标注 ebbingflow 来源（直接复用/适配/新开发）
- [x] 7 个数据流图已包含
- [x] 7 个反馈点已设置
- [x] M0-M10 依赖关系正确
- [x] 验收追踪矩阵覆盖 12.1-12.10
- [x] 向后兼容（MEMORY_ENABLED=false）贯穿所有里程碑
- [x] 并发边界约束已标注
- [x] 缓冲区读写规则已标注（@e 已读即删 / @d 已读不删 / @a 2am 清空）
- [x] 无 Critical/Important 问题
