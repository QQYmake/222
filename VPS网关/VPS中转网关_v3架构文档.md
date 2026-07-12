# 沉的 VPS 中转网关——V3 架构文档（记忆引擎）
> 文档状态：待审阅
> 运行阶段：本地先运行
> 核心目标：在 vps-gateway 中引入基于 ebbingflow 的认知记忆引擎，使"沉"在聊天过程中自动浮现关联记忆，无需主 LLM 显式查询或消耗额外 token

---

## 1. 首版边界

### 1.1 本版必须实现

1. **app.py v2 组件接线补全**——ToolRegistry / WakeController / WakePlanner / ModelToolLoop 在 app.py 中完成注入和启动（v2 遗留遗漏）
2. **MemoryPort 端口定义**——领域层抽象接口，隔离记忆引擎实现
3. **MemoryEngine 编排器**——实现 @0—@7 完整流程编排，管理 @a/@d/@e 缓冲区
4. **@a 记忆原料区**——跨平台跨窗口的今日聊天记录副本，每回合追加，2am 随沉淀清空
5. **@d 记忆缓存区**——查询路径生成并润色的记忆内容，已读不删，2am 清空
6. **@e 记忆浮现区**——周期生成的关联记忆/印象浮现，已读即删
7. **意图分类器**——置信度路由（规则优先）+ LLM 兜底（uncertain 触发）+ 周期校准（随机抽查）
8. **@4 查询路径**——R2 向量化 → R3 多轨检索 → R4 HybridScorer 重排 → R5 @d 生成 → R7 润色，20s 超时降级（γ 后台继续，前台无记忆转发）
9. **@6 无查询路径**——读 @e 浮现区内容，拼接转发
10. **@e 周期生成器**——~15 分钟轮询（含随机感），扫描 @d 缓存，纯数学随机选材（预留接口），LLM 语义拼装，润色写入
11. **润色桥**——对 @d / @e / 人格观察 / Saga 输出执行文学化润色
12. **memory_recall 工具**——主动唤醒回合暴露为 VPS 工具，主 LLM 自主调用触发 @4 流程
13. **新窗口衔接**——截取最近 15 条 @d 缓存拼接转发，模拟无缝换窗
14. **2am 沉淀管线**——W1 事件抽取 → W2 人格观察 → W3 校验 → W4 Saga 归并 → W5 向量化 → W6 润色 → 清空 @a/@d
15. **ContextBuilder 适配**——build() 增加可选 memory_recall 参数，润色内容替换 `<memories>` 块
16. **TurnRunner 适配**——注入 MemoryPort，recall() + after_turn() 调用点
17. **ebbingflow 组件 vendoring**——15 个直接复用模块导入适配，3 个需适配模块（Neo4j → SQLite）
18. **8 模型独立配置**——MEM_EMBED / MEM_INTENT / MEM_GEN / MEM_SURF / MEM_EXTRACT / MEM_PERSONA / MEM_SAGA / MEM_POLISH

### 1.2 本版只保留接口，不实现逻辑

1. **"手表数据" Sample 类型**——预留 Sample 数据模型和注入占位，不接真实数据源
2. **"身体" Sample 类型**——同上
3. **@e 选材数学模型**——当前纯随机挑选，预留 `SurfaceSelector` 接口供后续实现基于影响力的选材算法
4. **Neo4j 升级路径**——`GraphStore` 端口已定义，SQLiteGraphStore 是第一个适配器，未来可换 Neo4j 适配器
5. **SamplePublisher 自动回写**——沿用 v2 预留端口，V3 不实现 working_state/preferences 的自动回写

### 1.3 本版明确不做

1. **不引入 Neo4j**——VPS 2C4G 无法承载，用 SQLite 递归 CTE 替代图谱遍历
2. **不引入 PostgreSQL**——继续使用 SQLite
3. **不做流式响应（SSE）**——沿用 v2 状态，stream=true 仍返回 400
4. **不做多模型路由**——主 LLM 仍为单一上游模型；8 个记忆模型是独立配置，不是主 LLM 路由
5. **不做前端**——前端是独立迭代方向，V3 只保证 API 就绪
6. **不做"随心小屋"和"做梦"**——属于后续迭代，V3 为其提供记忆基础设施
7. **不修改唤醒时间窗口**——2am 沉淀是 MemoryEngine 内部定时器，不是 WakeJob，不受 08:00—24:00 约束
8. **不做多实例部署**——继续单进程

---

## 2. 核心术语

### 2.1 记忆引擎（MemoryEngine）

VPS 中转站内的独立记忆维护系统。基于 ebbingflow 组件构建，负责记忆的检索、生成、浮现、沉淀全链路。主 LLM 不感知记忆引擎的存在——它只接收已润色的记忆文本注入到 system message 中。

**生命周期**：由 FastAPI lifespan 启动和停止。启动时初始化 ChromaDB / SQLite / ebbingflow 组件，启动 @e 轮询任务和 2am 沉淀定时器。

### 2.2 @a 记忆原料区（buffer_raw）

今日用户 + 助手的纯文本聊天记录副本。不包含工具调用、system message 或其他提示词。跨平台（所有前端写入同一份）、跨窗口（所有对话窗口写入同一份）。每回合追加新消息，无新消息则 pass。凌晨 2 点随沉淀一起清空。

**读写规则**：每回合 after_turn 追加；沉淀管线读取后清空。

### 2.3 @d 记忆缓存区（buffer_recall）

查询路径（@4）生成的记忆内容缓存。包含润色后的最终文本和未润色的原始内容（[MEMORY] 事实区 + [NARRATIVE] 叙事区）。已读不删去——内容累积供 @e 选材和新窗口衔接使用。凌晨 2 点随沉淀清空。

**读写规则**：@4 路径写入；@5 读取（等待 @7 完成）；@e 生成器扫描选材；新窗口衔接读取最近 15 条；2am 清空。

### 2.4 @e 记忆浮现区（buffer_surface）

周期生成的记忆浮现内容。两种类型：关联记忆（association）和印象浮现（impression）。FEEL 浮点数全局变量控制印象浮现的概率。已读取即删去——一次性消费。

**读写规则**：@e 生成器周期写入；@6 路径读取后删除。

### 2.5 意图分类器（IntentClassifier）

三层结构：规则分类（每回合，0ms）→ LLM 兜底（仅 uncertain 触发，~1s）→ 周期校准（每 N 条随机抽查 1 条）。

输出 `IntentResult { label, confidence, matched_patterns }`，label 为 `query` / `no_query`。

### 2.6 记忆浮现生成器（SurfaceGenerator）

独立周期运行的后台任务。每 ~15 分钟（含随机感）轮询，扫描 @d 缓存区，纯数学随机挑选原料（预留 `SurfaceSelector` 接口），LLM 语义拼装，润色后写入 @e 浮现区。

### 2.7 沉淀管线（ConsolidationPipeline）

每日凌晨 2 点触发的批处理管线。从 @a 原料 + @d 缓存中抽取事件/事实/关系，更新人格画像，归并 Saga 长期主线，向量化消息存入 ChromaDB，润色人格和 Saga 输出。完成后清空 @a 和 @d。

### 2.8 润色桥（PolishBridge）

专用润色模型（MEM_POLISH）的调用封装。对 @d / @e / 人格观察 / Saga 输出执行文学化润色。保留准确的基本信息，仅改变表达风格。主 LLM 和前端接收到的都是润色后的文本。

### 2.9 memory_recall 工具（@d 工具）

主动唤醒回合暴露给主 LLM 的 VPS 工具。主 LLM 调用时传入查询字符串，工具内部触发完整 @4 流程（R2→R3→R4→R5→R7），返回润色后的 @d 内容。仅主动唤醒回合可用，用户回合不暴露。

### 2.10 既有 v2 术语

TurnRunner / TurnContext / ModelToolLoop / ToolRegistry / ToolDispatcher / WakeController / WakePlanner / ActiveTurnGate / SingleReaderGate / OutboxLongPollService / LocalScheduler / Sample / ContextBuilder——定义不变，参见 v2 架构文档。

---

## 3. 总体架构

```text
                         前端/测试端
                             │
                    POST /v1/chat/completions
                             │
                    ┌────────▼────────┐
                    │ ChatController   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  TurnRunner v3   │
                    │  1. recall()     │──── MemoryEngine ──┐
                    │  2. read_all()   │         │           │
                    │  3. build()      │    ┌────▼─────┐     │
                    │  4. tool_loop()  │    │ @0 意图  │     │
                    │  5. after_turn() │    │ 分类器   │     │
                    └────────┬────────┘    └────┬─────┘     │
                             │                  │            │
              ┌──────────────┼──────────────    │            │
              │              │                  │            │
         @4 查询路径     @6 无查询路径           │            │
         (需查询)        (无需查询)              │            │
              │              │                  │            │
    ┌─────────▼──────┐  ┌────▼─────┐           │            │
    │ R2 向量化      │  │ 读 @e    │           │            │
    │ R3 多轨检索    │  │ (已读删) │           │            │
    │ R4 HybridScorer│  └────┬─────┘           │            │
    │ R5 @d 生成     │       │                  │            │
    │ R7 润色        │       │                  │            │
    │ → 写 @d 缓存   │       │                  │            │
    └────────┬───────┘       │                  │            │
             │               │                  │            │
             └───────┬───────┘                  │            │
                     │                          │            │
              MemoryRecall (润色文本)            │            │
                     │                          │            │
              ┌──────▼──────┐                   │            │
              │ContextBuilder│                  │            │
              │ 替换<memories>│                  │            │
              └──────┬──────┘                   │            │
                     │                          │            │
              ┌──────▼──────┐                   │            │
              │ ModelToolLoop│                   │            │
              │ → 主 LLM     │                   │            │
              └──────┬──────┘                   │            │
                     │                          │            │
                     ▼                          │            │
              OpenAI 兼容响应                    │            │
                                          ┌─────▼──────────▼─────┐
                                          │  ebbingflow 组件      │
                                          │  ├ KnowledgeEngine    │
                                          │  ├ HybridScorer       │
                                          │  ├ EventExtractor     │
                                          │  ├ EpisodeManager     │
                                          │  ├ SagaManager        │
                                          │  ├ PersonaManager     │
                                          │  ├ VectorStorer/Retr  │
                                          │  ├ EventRepository    │
                                          │  └ LLMBridge (×8)     │
                                          └─────┬────────────────┘
                                                │
                                    ┌───────────┼───────────┐
                                    │           │           │
                              ┌─────▼────┐ ┌────▼────┐ ┌────▼────┐
                              │ SQLite   │ │ChromaDB │ │ SQLite  │
                              │ Graph    │ │(嵌入)   │ │ Buffer  │
                              │ Store    │ │         │ │ Store   │
                              └──────────┘ └─────────┘ └─────────┘

  独立后台任务：
  ┌──────────────────┐     ┌──────────────────────┐
  │ @e 周期生成器     │     │ 2am 沉淀管线          │
  │ ~15min 轮询      │     │ W1→W2→W3→W4→W5→W6    │
  │ 扫描@d→选材→     │     │ 清空 @a/@d            │
  │ LLM→润色→写@e    │     │                      │
  └──────────────────┘     └──────────────────────┘

  主动唤醒回合：
  WakeTrigger → 伪用户输入 → @0(no_query) → @6(读@e)
  + memory_recall 工具暴露 → 主 LLM 自主调用 → 触发 @4 流程
```

### 3.1 依赖方向

```text
HTTP/Scheduler 适配器 (app/adapters/)
  ↓
应用层 TurnRunner + MemoryEngine (app/application/)
  ↓
领域接口 (端口) (app/domain/ports/)
  ↓
领域模型 (app/domain/models/)
  ↓
基础设施实现 (app/infrastructure/, app/adapters/)
```

ebbingflow vendored 组件位于 `app/adapters/memory/ebbingflow/`，属于适配器层。它们不直接引用应用层或领域层，只通过 LLMBridge 接口和配置注入与上层交互。

禁止：
- MemoryEngine 不直接执行 SQL（通过 BufferStore / GraphStore 端口）
- ContextBuilder 不调用 LLM（只接收已润色的 MemoryRecall 文本）
- ebbingflow 组件不引用 vps-gateway 的 config.py（通过构造注入）
- TurnRunner 不感知 ebbingflow 内部结构（只调 MemoryPort.recall() / after_turn()）
- 主 LLM 不感知记忆引擎存在（只接收注入的 system message）

### 3.2 并发边界

| 并行对 | 是否允许 | 约束 |
|---|---|---|
| 用户回合 + 主动回合 | 允许 | 沿用 v2 不变量 #6 |
| 两个主动回合 | 禁止 | 沿用 v2 不变量 #7 |
| 用户回合 + @e 生成 | 允许 | @e 生成器是独立后台任务，不持有回合级锁 |
| 用户回合 + 2am 沉淀 | 允许 | 沉淀是独立后台任务；@a/@d 清空时用短事务，不影响正在进行的 recall |
| @4 超时降级 + 后台继续 | 允许 | asyncio.Task 后台运行，前台请求不带记忆转发 |
| memory_recall 工具调用 + 主回合 | 同一回合内串行 | 工具在 ModelToolLoop 内顺序执行，沿用 v2 工具循环约束 |

### 3.3 ebbingflow 组件复用映射

| ebbingflow 模块 | 处理方式 | V3 位置 | 适配说明 |
|---|---|---|---|
| `memory/event/slots.py` | 直接复用 | `adapters/memory/ebbingflow/event_slots.py` | 零改动 |
| `memory/scoring/hybrid_scorer.py` | 直接复用 | `adapters/memory/ebbingflow/hybrid_scorer.py` | 零改动；`graph_hop_score` 由 SQLiteGraphStore 提供输入值 |
| `memory/event/extractor.py` | 直接复用 | `adapters/memory/ebbingflow/event_extractor.py` | LLMBridge 注入适配 |
| `memory/integration/saga_manager.py` | 直接复用 | `adapters/memory/ebbingflow/saga_manager.py` | LLMBridge 注入适配 |
| `memory/integration/episode_manager.py` | 直接复用 | `adapters/memory/ebbingflow/episode_manager.py` | LLMBridge 注入适配 |
| `memory/identity/canonical.py` | 直接复用 | `adapters/memory/ebbingflow/identity_canonical.py` | 零改动 |
| `memory/identity/resolver.py` | 直接复用 | `adapters/memory/ebbingflow/identity_resolver.py` | 零改动 |
| `memory/identity/state_reducer.py` | 直接复用 | `adapters/memory/ebbingflow/identity_state_reducer.py` | 零改动 |
| `memory/identity/schema.py` | 直接复用 | `adapters/memory/ebbingflow/identity_schema.py` | 零改动 |
| `memory/vector/storer.py` | 直接复用 | `adapters/memory/ebbingflow/vector_storer.py` | ChromaDB 嵌入式模式，embed_config 注入 |
| `memory/vector/retriever.py` | 直接复用 | `adapters/memory/ebbingflow/vector_retriever.py` | 零改动 |
| `memory/sql/event_repository.py` | 直接复用 | `adapters/memory/ebbingflow/sql_event_repository.py` | 已原生支持 SQLite |
| `memory/sql/pool.py` | 适配 | `adapters/memory/ebbingflow/sql_pool.py` | 去掉 asyncpg 分支，保留 SQLite 路径 |
| `bridge/llm.py` | 适配 | `adapters/memory/ebbingflow/llm_bridge.py` | 解耦 `core.monitoring.token_monitor`，改为可选回调 |
| `memory/knowledge_engine.py` | 适配 | `adapters/memory/ebbingflow/knowledge_engine.py` | Neo4j driver → SQLiteGraphStore；Cypher → SQLite 递归 CTE；`infer_query_intent()` 直接复用 |
| `memory/identity/manager.py` | 适配 | `adapters/memory/ebbingflow/persona_manager.py` | Neo4j → SQLitePersonaStore；Big Five/EFSTB 逻辑不变 |
| `rank-bm25` | pip 依赖 | — | 纯 Python 库 |

---

## 4. 建议目录结构

```text
app/
├── domain/
│   ├── models/
│   │   ├── memory.py              # MemoryRecall, MemorySurface, IntentResult, RecallMode
│   │   └── ... (v2 既有模型不变)
│   └── ports/
│       ├── memory_engine.py       # MemoryPort (ABC)
│       ├── graph_store.py         # GraphStore (ABC) — 图谱存储端口
│       ├── persona_store.py       # PersonaStore (ABC) — 人格存储端口
│       ├── buffer_store.py        # BufferStore (ABC) — @a/@d/@e 缓冲端口
│       └── ... (v2 既有端口不变)
├── application/
│   ├── memory/
│   │   ├── memory_engine.py       # MemoryEngine (implements MemoryPort)
│   │   ├── buffer_manager.py      # @a/@d/@e 缓冲管理
│   │   ├── intent_classifier.py   # 置信度路由 + LLM 兜底 + 周期校准
│   │   ├── surface_generator.py   # @e 周期生成
│   │   ├── surface_selector.py    # 选材接口 (ABC) + 随机实现
│   │   ├── consolidation_pipeline.py # 2am 沉淀链路
│   │   └── polish_bridge.py       # 文学润色
│   └── ... (v2 既有应用层不变)
├── adapters/
│   ├── memory/
│   │   ├── ebbingflow/            # vendored ebbingflow 组件
│   │   │   ├── __init__.py
│   │   │   ├── event_slots.py
│   │   │   ├── event_extractor.py
│   │   │   ├── hybrid_scorer.py
│   │   │   ├── saga_manager.py
│   │   │   ├── episode_manager.py
│   │   │   ├── identity_canonical.py
│   │   │   ├── identity_resolver.py
│   │   │   ├── identity_state_reducer.py
│   │   │   ├── identity_schema.py
│   │   │   ├── vector_storer.py
│   │   │   ├── vector_retriever.py
│   │   │   ├── sql_event_repository.py
│   │   │   ├── sql_pool.py
│   │   │   ├── llm_bridge.py
│   │   │   ├── knowledge_engine.py    # 已适配 SQLite
│   │   │   └── persona_manager.py     # 已适配 SQLite
│   │   ├── sqlite_graph_store.py      # SQLite 图谱适配器 (implements GraphStore)
│   │   ├── sqlite_persona_store.py    # SQLite 人格适配器 (implements PersonaStore)
│   │   ├── sqlite_buffer_store.py     # SQLite 缓冲适配器 (implements BufferStore)
│   │   └── memory_recall_tool.py      # @d 工具 (implements ToolExecutor)
│   └── ... (v2 既有适配器不变)
├── infrastructure/
│   ├── memory_config.py           # 8 模型独立配置 + 全局变量
│   └── ... (v2 既有基础设施不变)
├── main.py
└── samples/
    ├── identity.sample.json
    ├── preferences.sample.json
    ├── memories.sample.json       # 降级为种子数据，MemoryEngine 初始化时导入
    └── working_state.sample.json
data/
├── outbox.sqlite3                 # v2 既有
├── wake_jobs.sqlite3              # v2 既有
├── memory_graph.sqlite3           # V3 新增：图谱存储 (events/relations/episodes/sagas)
├── memory_persona.sqlite3         # V3 新增：人格存储 (Big Five/EFSTB)
├── memory_buffer.sqlite3          # V3 新增：@a/@d/@e 缓冲
└── chromadb/                      # V3 新增：ChromaDB 嵌入式数据
tests/
├── unit/
│   ├── memory/                    # V3 新增
│   └── ... (v2 既有)
└── integration/
    ├── memory/                    # V3 新增
    └── ... (v2 既有)
```

---

## 5. 数据合同

### 5.1 MemoryRecall

```json
{
  "mode": "string — recall 路径：query | no_query | new_window | degraded",
  "text": "string — 润色后的记忆文本，直接注入 <memories> 块；degraded 时为空字符串",
  "source_recall_ids": "[int] — 关联的 @d 缓存条目 ID 列表",
  "metadata": {
    "intent": "string — query | no_query",
    "confidence": "float — 意图分类置信度",
    "retrieval_tracks": "[string] — 参与的检索轨道",
    "scorer_stats": "object — HybridScorer 打分统计",
    "timeout": "bool — 是否超时降级"
  }
}
```

校验规则：
- `mode` 为 `degraded` 时 `text` 必须为空字符串
- `mode` 为 `no_query` 时 `source_recall_ids` 为空数组
- `mode` 为 `new_window` 时 `text` 为最近 15 条 @d 内容拼接

### 5.2 MemorySurface（@e 条目）

```json
{
  "id": "int — 自增 ID",
  "content": "string — 润色后的浮现内容",
  "raw_content": "string — 未润色的原始内容",
  "surface_type": "string — association | impression",
  "source_recall_ids": "[int] — 选材来源的 @d 条目 ID 列表",
  "created_at": "ISO8601 — 生成时间"
}
```

校验规则：
- `surface_type` 必须为 `association` 或 `impression`
- `source_recall_ids` 至少包含 1 个元素
- 写入后 `read_at` 为 null；读取后设为时间戳并删除

### 5.3 IntentResult

```json
{
  "label": "string — query | no_query",
  "confidence": "float — 0.0-1.0",
  "matched_patterns": "[string] — 匹配到的规则模式列表",
  "source": "string — rule | llm_fallback | calibration",
  "intent_type": "string | null — fact | summary | long_term | semantic (仅 query 时)"
}
```

校验规则：
- `confidence` 范围 [0.0, 1.0]
- `source` 为 `rule` 时 `matched_patterns` 必须非空
- `label` 为 `no_query` 时 `intent_type` 为 null

### 5.4 @a 缓冲区（buffer_raw）

```sql
CREATE TABLE buffer_raw (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,           -- user | assistant
  content TEXT NOT NULL,        -- 纯文本，不含工具调用和 system message
  source_platform TEXT,         -- 来源前端标识
  turn_id TEXT NOT NULL,        -- 关联回合 ID
  created_at TEXT NOT NULL      -- ISO8601
);
CREATE INDEX idx_buffer_raw_created ON buffer_raw(created_at);
```

### 5.5 @d 缓冲区（buffer_recall）

```sql
CREATE TABLE buffer_recall (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trigger_id TEXT NOT NULL,     -- 关联的查询触发 ID
  content TEXT NOT NULL,        -- 润色后的最终文本
  raw_content TEXT NOT NULL,    -- 未润色原始内容 ([MEMORY]+[NARRATIVE])
  metadata TEXT,                -- JSON: 检索轨道、HybridScorer 统计
  created_at TEXT NOT NULL,     -- ISO8601
  read_at TEXT                  -- 已读取时间，可空（内容不删去）
);
CREATE INDEX idx_buffer_recall_created ON buffer_recall(created_at);
```

### 5.6 @e 浮现区（buffer_surface）

```sql
CREATE TABLE buffer_surface (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,        -- 润色后的浮现内容
  raw_content TEXT NOT NULL,    -- 未润色原始内容
  surface_type TEXT NOT NULL,   -- association | impression
  source_recall_ids TEXT NOT NULL, -- JSON array of buffer_recall.id
  created_at TEXT NOT NULL,     -- ISO8601
  read_at TEXT                  -- 读取后设值并删除该行
);
CREATE INDEX idx_buffer_surface_created ON buffer_surface(created_at);
```

### 5.7 图谱存储（memory_graph.sqlite3）

```sql
-- 事件层（替代 Neo4j Event nodes）
CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  subject TEXT, object TEXT, predicate TEXT,
  action_type TEXT NOT NULL,    -- STATE_CHANGE | INTERACTION | CONSUMPTION | PLAN | OPINION | ACHIEVEMENT | RELATIONSHIP | OTHER
  context TEXT,
  event_time TEXT,
  emotion_label TEXT,
  impact_score REAL DEFAULT 0.5,
  confidence REAL DEFAULT 0.7,
  source_msg_id TEXT NOT NULL,  -- 证据链回链
  created_at TEXT NOT NULL
);

-- 关系层（替代 Neo4j Relation edges）
CREATE TABLE relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,
  object TEXT NOT NULL,
  predicate TEXT NOT NULL,
  event_id TEXT REFERENCES events(event_id),
  created_at TEXT NOT NULL
);
CREATE INDEX idx_relations_subject ON relations(subject);
CREATE INDEX idx_relations_object ON relations(object);

-- 剧情层
CREATE TABLE episodes (
  episode_id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  start_time TEXT, end_time TEXT,
  event_ids TEXT,               -- JSON array
  big_five_snapshot TEXT,       -- JSON
  efstb_snapshot TEXT,          -- JSON
  source_msg_ids TEXT,          -- JSON array
  created_at TEXT NOT NULL
);

-- 主线层
CREATE TABLE sagas (
  saga_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  narrative TEXT NOT NULL,      -- 润色后的叙事文本
  raw_narrative TEXT NOT NULL,
  episode_ids TEXT,             -- JSON array
  status TEXT DEFAULT 'active', -- active | archived
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### 5.8 人格存储（memory_persona.sqlite3）

```sql
CREATE TABLE persona_profiles (
  actor_id TEXT PRIMARY KEY,
  big_five TEXT NOT NULL,       -- JSON: {openness, conscientiousness, extraversion, agreeableness, neuroticism}
  efstb TEXT NOT NULL,          -- JSON: {energy, friendliness, stress, thinking, behavior}
  aliases TEXT,                 -- JSON array
  updated_at TEXT NOT NULL
);

CREATE TABLE persona_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id TEXT NOT NULL,
  observation TEXT NOT NULL,    -- 润色后的人格观察文本
  raw_observation TEXT NOT NULL,
  source_episode_ids TEXT,      -- JSON array
  created_at TEXT NOT NULL
);
```

### 5.9 既有 v2 数据合同

ChatMessage / ToolCall / ToolResult / TurnContext / WakeJob / OutboxMessage / SampleEnvelope / AllSamples / PreparedTurn——定义不变，参见 v2 架构文档第 5 节。

---

## 6. 模块、输入输出、指令与接口

### 6.1 MemoryPort（领域端口）

```python
class MemoryPort(ABC):
    @abstractmethod
    async def recall(self, trigger: Trigger, raw_messages: list[ChatMessage]) -> MemoryRecall:
        """回合开始前调用。返回记忆注入内容。"""

    @abstractmethod
    async def after_turn(self, raw_messages: list[ChatMessage], response: ChatCompletionResponse, turn_id: str) -> None:
        """回合结束后调用。追加 @a 原料。"""

    @abstractmethod
    async def start_background_tasks(self) -> None:
        """启动 @e 周期生成器和 2am 沉淀定时器。"""

    @abstractmethod
    async def stop_background_tasks(self) -> None:
        """停止后台任务。"""

    @abstractmethod
    async def recall_as_tool(self, query: str) -> str:
        """memory_recall 工具调用入口。触发 @4 流程，返回润色后的 @d 内容。"""
```

### 6.2 MemoryEngine（应用层编排器）

```text
输入: Trigger + raw_messages
指令:
  1. 调用 IntentClassifier.classify(raw_messages[-1].content) → IntentResult
  2. 判断 recall_mode:
     a. HTTP 请求头 X-Memory-Mode: new_window → mode=new_window
     b. IntentResult.label == "query" → mode=query
     c. IntentResult.label == "no_query" → mode=no_query
  3. 根据 mode 执行对应路径:
     a. new_window → BufferManager.read_recent_recall(N=15) → 拼接 → MemoryRecall
     b. query → _run_query_path(intent_result, raw_messages) → MemoryRecall
     c. no_query → _run_surface_path() → MemoryRecall
输出: MemoryRecall

_run_query_path(intent_result, raw_messages):
输入: IntentResult + raw_messages
指令:
  1. asyncio.create_task(_execute_retrieval_pipeline(intent_result, raw_messages))
  2. 等待 task 完成，timeout=20s
  3. 超时 → mode=degraded, text=""，task 后台继续运行
  4. 完成 → 读取 @d 最新条目 → mode=query, text=润色文本
输出: MemoryRecall

_execute_retrieval_pipeline(intent_result, raw_messages):
输入: IntentResult + raw_messages
指令:
  1. R2: LLMBridge(MEM_EMBED).embed(raw_messages[-1].content) → query_vector
  2. R3: 并行执行多轨检索:
     a. KnowledgeEngine.retrieve(query_vector, intent_type) → graph_events
     b. VectorRetriever.search(query_vector, top_k=10) → vector_results
     c. BM25.search(raw_messages[-1].content, top_k=10) → bm25_results
     d. EventRepository.search_structured(intent_type) → sql_results
  3. R4: HybridScorer.score(all_candidates, intent_type) → ranked_candidates
  4. R5: LLMBridge(MEM_GEN).generate(ranked_candidates, raw_messages) → raw_d_content
  5. R7: PolishBridge.polish(raw_d_content) → polished_d_content
  6. BufferManager.write_recall(trigger_id, polished_d_content, raw_d_content, metadata)
输出: None (写入 @d 缓存)

_run_surface_path():
输入: 无
指令:
  1. BufferManager.read_surface() → @e 条目或 None
  2. 有条目 → mode=no_query, text=条目.content
  3. 无条目 → mode=no_query, text="" (空值可读)
输出: MemoryRecall
```

### 6.3 BufferManager（缓冲管理）

```text
输入: 缓冲操作请求
指令:
  append_raw(role, content, platform, turn_id) → 追加 @a
  read_recent_recall(n) → 读取最近 N 条 @d（不标记已读，不删除）
  write_recall(trigger_id, content, raw_content, metadata) → 写入 @d
  read_recall_latest() → 读取最新一条 @d（标记 read_at，不删除）
  scan_recall_for_surface() → 扫描 @d 供 @e 选材（不标记，不删除）
  write_surface(content, raw_content, surface_type, source_ids) → 写入 @e
  read_surface() → 读取并删除一条最早的 @e（FIFO）
  clear_raw() → 清空 @a
  clear_recall() → 清空 @d
输出: 缓冲数据或 None
```

接口：
```python
class BufferStore(ABC):
    @abstractmethod
    async def append_raw(self, role: str, content: str, platform: str, turn_id: str) -> None: ...
    @abstractmethod
    async def read_recent_recall(self, n: int) -> list[RecallEntry]: ...
    @abstractmethod
    async def write_recall(self, trigger_id: str, content: str, raw_content: str, metadata: dict) -> int: ...
    @abstractmethod
    async def read_recall_latest(self) -> RecallEntry | None: ...
    @abstractmethod
    async def scan_recall_for_surface(self) -> list[RecallEntry]: ...
    @abstractmethod
    async def write_surface(self, content: str, raw_content: str, surface_type: str, source_ids: list[int]) -> int: ...
    @abstractmethod
    async def read_surface(self) -> SurfaceEntry | None: ...
    @abstractmethod
    async def clear_raw(self) -> None: ...
    @abstractmethod
    async def clear_recall(self) -> None: ...
```

### 6.4 IntentClassifier（意图分类器）

```text
输入: 用户最新消息文本
指令:
  第一层——规则分类 (0ms):
    1. 匹配查询关键词模式 (查一下/记得XX吗/之前/上一次/XX天前/XX时候/那天)
    2. 匹配无查询模式 (纯闲聊/陈述/感叹)
    3. 输出 {label, confidence, matched_patterns}
    4. confidence >= 0.8 → 直接返回
    5. confidence < 0.8 → 进入第二层

  第二层——LLM 兜底 (~1s):
    6. LLMBridge(MEM_INTENT).classify(text) → "query" | "no_query" (1 token)
    7. 返回 {label, confidence=1.0, source="llm_fallback"}

  第三层——周期校准 (异步, 不阻塞):
    8. 每 N 条消息 (全局变量) 随机抽 1 条
    9. 无论规则置信度，跑一次 LLM 分类
    10. 与规则结果对比，记录偏差 (不参与实时路由)
输出: IntentResult
```

### 6.5 RetrievalPipeline（多轨检索，复用 ebbingflow）

```text
输入: query_vector + intent_type + raw_messages
指令:
  1. KnowledgeEngine.retrieve(query_vector, intent_type)
     ├ GraphStore.query_events(subject/object, max_hops=3) → graph_results
     ├ GraphStore.query_episodes(intent_type) → episode_results
     ├ GraphStore.query_sagas(status='active') → saga_results
     ├ GraphStore.query_plans() → plan_results
     └ infer_query_intent() → 复用 ebbingflow 静态方法
  2. VectorRetriever.search(query_vector, top_k=10) → vector_results
  3. BM25.search(text, top_k=10) → bm25_results
  4. EventRepository.search_structured(intent_type) → sql_results
输出: list[Candidate] (统一候选集)
```

注：KnowledgeEngine 内部的 Neo4j Cypher 查询已适配为 SQLiteGraphStore 的递归 CTE。GraphStore 端口定义：

```python
class GraphStore(ABC):
    @abstractmethod
    async def query_events(self, entity: str, max_hops: int) -> list[dict]: ...
    @abstractmethod
    async def query_episodes(self, intent_type: str) -> list[dict]: ...
    @abstractmethod
    async def query_sagas(self, status: str) -> list[dict]: ...
    @abstractmethod
    async def query_plans(self) -> list[dict]: ...
    @abstractmethod
    async def write_event(self, event: dict) -> None: ...
    @abstractmethod
    async def write_episode(self, episode: dict) -> None: ...
    @abstractmethod
    async def write_saga(self, saga: dict) -> None: ...
```

### 6.6 HybridScorer（重排，直接复用 ebbingflow）

```text
输入: list[Candidate] + intent_type
指令:
  1. 意图路由: fact → 优先 SQL/Graph/Structured; summary/long_term → 提升 Episode/Saga
  2. Ebbinghaus 时间衰减 (含置信度护栏)
  3. 多维打分: semantic_score / graph_hop_score / time_score / impact_score
  4. RRF 融合 + 意图感知配额控制
输出: list[ScoredCandidate] (按分数降序)
```

约束：纯计算，无 IO，无 LLM 调用。

### 6.7 SurfaceGenerator（@e 周期生成器）

```text
输入: 无 (独立后台任务)
指令:
  循环:
    1. sleep(SURFACE_INTERVAL_SECONDS ± random_jitter)
    2. BufferManager.scan_recall_for_surface() → @d 候选列表
    3. 候选为空 → continue
    4. SurfaceSelector.select(candidates) → selected_entries
       (当前实现: RandomSurfaceSelector, 随机选 1-3 条)
       (预留接口: ImpactSurfaceSelector, 基于影响力分数选材)
    5. random() < FEEL_THRESHOLD → surface_type=impression
       else → surface_type=association
    6. LLMBridge(MEM_SURF).generate(selected_entries, surface_type) → raw_surface
    7. PolishBridge.polish(raw_surface) → polished_surface
    8. BufferManager.write_surface(polished_surface, raw_surface, surface_type, source_ids)
输出: None (写入 @e 浮现区)
```

接口：
```python
class SurfaceSelector(ABC):
    @abstractmethod
    def select(self, candidates: list[RecallEntry]) -> list[RecallEntry]: ...

class RandomSurfaceSelector(SurfaceSelector):
    """当前实现：纯随机挑选 1-3 条。"""
    def select(self, candidates: list[RecallEntry]) -> list[RecallEntry]: ...
```

### 6.8 ConsolidationPipeline（2am 沉淀管线）

```text
输入: 无 (每日 02:00 触发)
指令:
  W1: 事件/事实抽取
    1. BufferManager.read_all_raw() → @a 全量
    2. BufferManager.read_all_recall() → @d 全量
    3. EventExtractor.extract(@a + @d) → list[MemoryEvent]
       (LLMBridge(MEM_EXTRACT), 复用 ebbingflow EventExtractor)
  W2: 人格观察与演化
    4. PersonaManager.observe(@a, events) → persona_snapshot
       (LLMBridge(MEM_PERSONA), 复用 ebbingflow PersonaManager)
  W3: 结构化事件校验
    5. 规则校验: 去重、冲突检测、字段标准化
    6. 可选: LLMBridge(MEM_INTENT).validate(events) → 轻量校验
  W4: Saga 聚类归并
    7. SagaManager.cluster(events, episodes) → saga_updates
       (LLMBridge(MEM_SAGA), 复用 ebbingflow SagaManager)
  W5: 消息向量化
    8. VectorStorer.store_batch(@a messages) → 存入 ChromaDB
       (LLMBridge(MEM_EMBED), 复用 ebbingflow VectorStorer)
  W6: 润色
    9. PolishBridge.polish(persona_snapshot) → polished_persona
    10. PolishBridge.polish(saga_updates) → polished_sagas
  清理:
    11. GraphStore.write_events(events)
    12. GraphStore.write_episodes(episodes)
    13. GraphStore.write_sagas(saga_updates)
    14. PersonaStore.write(persona_snapshot)
    15. BufferManager.clear_raw() → 清空 @a
    16. BufferManager.clear_recall() → 清空 @d
输出: None (持久化到图谱/人格/向量存储)
```

### 6.9 PolishBridge（润色桥）

```text
输入: raw_text + context_type (recall | surface | persona | saga)
指令:
  1. 构造润色 prompt (保留准确基本信息，仅文学化表达)
  2. LLMBridge(MEM_POLISH).generate(raw_text, context_type) → polished_text
输出: polished_text
```

### 6.10 SqliteGraphStore（图谱适配器）

```text
输入: GraphStore 方法参数
指令:
  query_events(entity, max_hops):
    1. 用递归 CTE 从 relations 表出发，做 1-max_hops 跳遍历
    2. 关联 events 表获取事件详情
    3. 返回事件列表
  query_episodes(intent_type):
    1. SELECT FROM episodes WHERE 时间范围匹配
    2. 关联 event_ids 获取事件列表
  query_sagas(status):
    1. SELECT FROM sagas WHERE status = ?
  write_event(event):
    1. INSERT INTO events
    2. INSERT INTO relations (subject, object, predicate)
  write_episode / write_saga:
    1. INSERT OR REPLACE
输出: 查询结果或 None
```

约束：每次操作短连接，不长期共享 connection。

### 6.11 memory_recall 工具（@d 工具）

```text
输入: tool_call 参数 { query: string }
指令:
  1. 调用 MemoryEngine.recall_as_tool(query)
  2. recall_as_tool 内部:
     a. 构造 IntentResult(label="query", intent_type=auto, source="tool_call")
     b. 执行 _execute_retrieval_pipeline(intent_result, [pseudo_message])
     c. 读取 @d 最新条目
     d. 返回润色后文本
  3. 工具结果回灌主 LLM
输出: ToolResult (content=润色后的 @d 文本)
```

工具 Schema：
```json
{
  "type": "function",
  "function": {
    "name": "memory_recall",
    "description": "查询记忆。传入你想回忆的内容或问题，系统会检索关联记忆并返回。",
    "parameters": {
      "type": "object",
      "properties": {
        "query": { "type": "string", "description": "查询内容" }
      },
      "required": ["query"]
    }
  }
}
```

约束：
- 仅主动唤醒回合注册到 ToolRegistry
- 用户回合不注册此工具
- 工具超时沿用 v2 TOOL_TIMEOUT_SECONDS（15s）；若检索管线超出，返回部分结果或超时提示

### 6.12 ContextBuilder v3

```text
输入: AllSamples + Trigger + MemoryRecall (可选)
指令:
  1-4. 沿用 v2: sort_memories / fit_within_budget / render_state_xml / extract_frontend_instructions
  5. IF memory_recall is not None AND memory_recall.text != "":
       用 memory_recall.text 替换 <memories> 块
     ELSE:
       沿用 v2: 从 samples.memories 渲染 <memories>
  6. 合并: [base_prompt + state_xml + frontend_instructions]
  7. 被动回合: [server_system] + conversation_messages
     主动回合: [server_system, timer_user_message]
输出: PreparedTurn
```

向后兼容：memory_recall=None 时行为与 v2 完全一致。

### 6.13 TurnRunner v3

```text
run_user_turn(UserTrigger):
输入: UserTrigger
指令:
  1. memory_port.recall(trigger, raw_messages) → MemoryRecall    ← V3 新增
  2. sample_reader.read_all() → AllSamples
  3. context_builder.build(samples, trigger, memory_recall) → PreparedTurn  ← 签名变更
  4. model_tool_loop.run(prepared) → ChatCompletionResponse
  5. memory_port.after_turn(raw_messages, response, turn_id)     ← V3 新增
  6. 返回 response
输出: ChatCompletionResponse

run_wake_turn(WakeTrigger):
输入: WakeTrigger
指令:
  1. 构造伪用户输入
  2. memory_port.recall(trigger, [pseudo_input]) → MemoryRecall  ← V3 新增
     (recall 内部 @0 判定 no_query → @6 读 @e)
  3. sample_reader.read_all() → AllSamples
  4. context_builder.build(samples, trigger, memory_recall) → PreparedTurn
  5. model_tool_loop.run(prepared, extra_tools=[memory_recall_tool])  ← V3 新增
     (ToolRegistry 额外注册 memory_recall 工具)
  6. <NO_MESSAGE> → 不写 Outbox
     普通文本 → Outbox enqueue + notify
  7. memory_port.after_turn([pseudo_input], response, turn_id)
输出: ActiveTurnResult
```

### 6.14 AppFactory v3

```text
create_app(config):
  1. 创建 Async HTTP Client (共享)
  2. 初始化 v2 组件:
     - OutboxStore / OutboxNotifier / SingleReaderGate / ActiveTurnGate
     - SampleReader (FileSampleRepository)
     - ModelClient (OpenAIUpstreamClient)
     - ToolRegistry + ToolDispatcher + VPS 工具
     - WakeJobStore / WakePlanner / WakeController
     - OutboxLongPollService
     - LocalScheduler
  3. 初始化 V3 记忆组件:
     - MemoryConfig (8 模型独立配置)
     - GraphStore (SqliteGraphStore)
     - PersonaStore (SqlitePersonaStore)
     - BufferStore (SqliteBufferStore)
     - VectorStorer / VectorRetriever (ChromaDB 嵌入式)
     - KnowledgeEngine (适配 SQLite)
     - PersonaManager (适配 SQLite)
     - EventExtractor / SagaManager / EpisodeManager
     - LLMBridge × 8 (独立配置)
     - PolishBridge
     - IntentClassifier
     - SurfaceGenerator + RandomSurfaceSelector
     - ConsolidationPipeline
     - BufferManager
     - MemoryEngine (注入以上全部)
  4. 注入 TurnRunner: model_client + sample_reader + context_builder + tool_registry + memory_port
  5. 注入 ToolDispatcher: tool_registry + 工具执行器
  6. lifespan startup:
     a. WakeJobStore.recover_after_restart()
     b. 启动 WakePlanner + 非阻塞扫描循环
     c. MemoryEngine.start_background_tasks() (@e 生成器 + 2am 定时器)
     d. MemoryEngine 初始化: 若图谱为空, 从 memories.sample.json 导入种子数据
  7. lifespan shutdown:
     a. 停止 Scheduler
     b. MemoryEngine.stop_background_tasks()
     c. 关闭 Async HTTP Client
输出: FastAPI app
```

### 6.15 既有 v2 模块

ChatController / OutboxController / ModelToolLoop / ToolRegistry / ToolDispatcher / WakeController / WakePlanner / WakeJobStore / LocalScheduler / OutboxLongPollService / OutboxStore / SampleReader / ModelClient / Config / Logging——定义不变，参见 v2 架构文档第 6 节。

唯一变更：ToolRegistry 增加 `register_for_wake_only(tool)` 方法，标记仅在主动唤醒回合暴露的工具。

---

## 7. 端到端数据流

### 7.1 用户回合——@6 无查询路径

```text
前端 POST /v1/chat/completions
  → ChatController 校验
  → TurnRunner.run_user_turn()
  → MemoryEngine.recall(trigger, raw_messages)
      → IntentClassifier.classify(text) → no_query, confidence=0.9
      → BufferManager.read_surface() → @e 条目或 None
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

### 7.2 用户回合——@4 查询路径

```text
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

### 7.3 新窗口衔接

```text
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

### 7.4 主动唤醒 + @d 工具

```text
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

### 7.5 @e 周期生成

```text
SurfaceGenerator (独立 asyncio 后台任务)
  循环:
    1. sleep(900s ± random(0, 300s))  # ~15min + 随机感
    2. BufferManager.scan_recall_for_surface() → @d 候选列表
    3. 候选为空 → continue
    4. RandomSurfaceSelector.select(candidates) → 1-3 条
    5. random() < FEEL_THRESHOLD (默认 0.3)?
       → impression: 生成印象浮现
       → association: 生成关联记忆
    6. MEM_SURF.generate(selected, surface_type) → raw_surface
    7. MEM_POLISH.polish(raw_surface, "surface") → polished_surface
    8. BufferManager.write_surface(polished_surface, raw_surface, surface_type, source_ids)
```

### 7.6 2am 沉淀管线

```text
ConsolidationPipeline (每日 02:00 触发)
  1. W1: EventExtractor.extract(@a + @d) → events
     (MEM_EXTRACT, 复用 ebbingflow)
  2. W2: PersonaManager.observe(@a, events) → persona_snapshot
     (MEM_PERSONA, 复用 ebbingflow)
  3. W3: 规则校验 + 可选 LLM 轻量校验
  4. W4: SagaManager.cluster(events, episodes) → saga_updates
     (MEM_SAGA, 复用 ebbingflow)
  5. W5: VectorStorer.store_batch(@a messages) → ChromaDB
     (MEM_EMBED, 复用 ebbingflow)
  6. W6: PolishBridge.polish(persona_snapshot) + PolishBridge.polish(saga_updates)
  7. 持久化:
     → GraphStore.write_events / write_episodes / write_sagas
     → PersonaStore.write
  8. 清理:
     → BufferManager.clear_raw() (清空 @a)
     → BufferManager.clear_recall() (清空 @d)
```

### 7.7 既有 v2 数据流

用户回合工具循环 / schedule_wakeup / 随机唤醒 / Outbox 长轮询 / 并行隔离——不变，参见 v2 架构文档第 7 节。

---

## 8. 错误与降级

| 错误场景 | 处理策略 | 外部可见行为 |
|---|---|---|
| @4 检索管线超时 (20s) | γ 降级：后台继续，前台无记忆转发 | 正常响应，`<memories>` 为空 |
| @4 后台任务异常 | 记录日志，不影响已降级的请求 | 正常响应 |
| IntentClassifier LLM 兜底失败 | 降级为规则结果 | 正常响应 |
| IntentClassifier 规则和 LLM 均失败 | 默认 no_query | 正常响应，走 @6 路径 |
| @d 生成 (MEM_GEN) 失败 | 返回降级 MemoryRecall | 正常响应，`<memories>` 为空 |
| 润色 (MEM_POLISH) 失败 | 使用未润色的 raw_content | 正常响应，文本未文学化 |
| 向量化 (MEM_EMBED) 失败 | 跳过向量检索轨道，其他轨道继续 | 正常响应，检索覆盖降低 |
| ChromaDB 初始化失败 | 跳过向量轨道，记录告警 | 正常响应 |
| SQLite GraphStore 查询失败 | 跳过图谱轨道，其他轨道继续 | 正常响应 |
| @e 生成器异常 | 记录日志，等待下一周期 | @6 路径可能读到空 @e |
| 2am 沉淀 W1 失败 | 中断管线，不清空 @a/@d，等待次日重试 | 无外部可见行为 |
| 2am 沉淀 W2-W6 部分失败 | 已完成步骤持久化，后续步骤跳过，清空 @a/@d | 无外部可见行为 |
| memory_recall 工具调用超时 | 沿用 v2 工具超时策略 (15s)，返回超时 ToolResult | 主 LLM 收到 tool_timeout |
| MemoryEngine 未配置 | ContextBuilder 沿用 v2 从 samples.memories 渲染 | 正常响应（向后兼容） |
| BufferStore 写入失败 | 记录日志，不阻塞回合 | 正常响应，@a/@d 可能丢失该条 |
| 种子数据导入失败 | 记录告警，记忆引擎以空库启动 | 正常响应，无历史记忆 |
| 既有 v2 错误场景 | 沿用 v2 降级策略 | 参见 v2 第 8 节 |

---

## 9. 配置接口

### 9.1 记忆引擎总开关

| 变量名 | 说明 | 默认值 | 必填 |
|---|---|---|---|
| `MEMORY_ENABLED` | 启用记忆引擎 | `false` | 否 |

### 9.2 8 模型独立配置

每个模型配置前缀为 `MEM_<ROLE>`，支持 `_BASE_URL` / `_API_KEY` / `_MODEL` / `_TEMPERATURE` / `_MAX_TOKENS` / `_TIMEOUT`。

| 模型 ID | 配置前缀 | 用途 | 必填 |
|---|---|---|---|
| MEM_EMBED | `MEM_EMBED_*` | 向量化 (R2/W5) | MEMORY_ENABLED=true 时是 |
| MEM_INTENT | `MEM_INTENT_*` | 意图分类 LLM 兜底 (R1) | 是 |
| MEM_GEN | `MEM_GEN_*` | @d 记忆生成 (R5) | 是 |
| MEM_SURF | `MEM_SURF_*` | @e 浮现生成 | 是 |
| MEM_EXTRACT | `MEM_EXTRACT_*` | 事件/事实抽取 (W1) | 是 |
| MEM_PERSONA | `MEM_PERSONA_*` | 人格观察 (W2) | 是 |
| MEM_SAGA | `MEM_SAGA_*` | Saga 归并 (W4) | 是 |
| MEM_POLISH | `MEM_POLISH_*` | 文学润色 (R7/W6) | 是 |

embedding 模型特殊配置：

| 变量名 | 说明 | 默认值 | 必填 |
|---|---|---|---|
| `MEM_EMBED_TYPE` | 嵌入方式：`local` (sentence-transformers) / `api` (OpenAI 兼容) | `local` | 否 |
| `MEM_EMBED_MODEL` | 嵌入模型名 | `paraphrase-multilingual-MiniLM-L12-v2` | 否 |

### 9.3 全局变量

| 变量名 | 说明 | 默认值 | 必填 |
|---|---|---|---|
| `MEM_RECALL_TIMEOUT_SECONDS` | @4 路径超时阈值 | `20` | 否 |
| `MEM_SURFACE_INTERVAL_SECONDS` | @e 生成周期基准 (秒) | `900` | 否 |
| `MEM_SURFACE_JITTER_SECONDS` | @e 生成随机抖动上限 (秒) | `300` | 否 |
| `MEM_FEEL_THRESHOLD` | 印象浮现概率 (0.0-1.0) | `0.3` | 否 |
| `MEM_NEW_WINDOW_RECALL_COUNT` | 新窗口截取 @d 条数 | `15` | 否 |
| `MEM_INTENT_CONFIDENCE_THRESHOLD` | 规则置信度阈值 (低于则 LLM 兜底) | `0.8` | 否 |
| `MEM_INTENT_CALIBRATION_INTERVAL` | 周期校准间隔 (消息条数) | `20` | 否 |
| `MEM_CONSOLIDATION_TIME` | 沉淀触发时间 (HH:MM) | `02:00` | 否 |
| `MEM_CONSOLIDATION_TIMEZONE` | 沉淀时区 | `Asia/Shanghai` | 否 |

### 9.4 存储路径

| 变量名 | 说明 | 默认值 | 必填 |
|---|---|---|---|
| `MEM_GRAPH_DATABASE_PATH` | 图谱 SQLite 路径 | `./data/memory_graph.sqlite3` | 否 |
| `MEM_PERSONA_DATABASE_PATH` | 人格 SQLite 路径 | `./data/memory_persona.sqlite3` | 否 |
| `MEM_BUFFER_DATABASE_PATH` | 缓冲 SQLite 路径 | `./data/memory_buffer.sqlite3` | 否 |
| `MEM_CHROMADB_PATH` | ChromaDB 数据目录 | `./data/chromadb/` | 否 |
| `MEM_SEED_SAMPLE_PATH` | 种子数据路径 (memories.sample.json) | `./samples/memories.sample.json` | 否 |

### 9.5 既有 v2 配置

全部沿用，参见 v2 架构文档第 9 节。

配置校验规则：
- `MEMORY_ENABLED=true` 时 8 个模型配置的 `_BASE_URL` / `_API_KEY` / `_MODEL` 必填
- `MEM_EMBED_TYPE=local` 时 `MEM_EMBED_MODEL` 指定的模型需可下载
- `MEM_FEEL_THRESHOLD` 范围 [0.0, 1.0]
- `MEM_RECALL_TIMEOUT_SECONDS` 范围 [5, 60]
- `MEM_SURFACE_INTERVAL_SECONDS` 不小于 300
- `MEM_NEW_WINDOW_RECALL_COUNT` 范围 [1, 100]
- `MEM_INTENT_CONFIDENCE_THRESHOLD` 范围 [0.0, 1.0]

---

## 10. 本地启动与部署边界

### 10.1 本地阶段

- 在现有 v2 项目中增量修改，不重新搭建仓库
- 启动命令沿用 v2：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 单进程运行，禁止 `--workers > 1`
- ChromaDB 嵌入式模式，无需独立服务
- sentence-transformers 首次运行时下载模型（~120MB）
- 测试工具只在测试配置下注册
- `MEMORY_ENABLED=false` 时系统行为与 v2 完全一致

### 10.2 部署阶段

- 继续单进程部署，端口 5000
- SQLite、ChromaDB、Sample 目录必须持久化
- `MEMORY_ENABLED=true` 时需配置 8 个模型
- VPS 内存预算：FastAPI(~200MB) + ChromaDB(~300MB) + sentence-transformers(~400MB) + codex CLI(~500MB-1GB) + OS(~400MB) ≈ 2-2.5GB，剩余 ~1.5-2GB 余量
- 本地与 VPS 只更换环境变量和持久化路径
- sentence-transformers 模型文件持久化到 `MEM_CHROMADB_PATH` 同级目录，避免每次重启重新下载

---

## 11. 可观测数据

| 指标/日志 | 记录时机 | 用途 |
|---|---|---|
| `memory_recall_started` | recall() 开始 | 追踪记忆注入延迟 |
| `memory_recall_completed` | recall() 完成 | mode/text 长度/耗时 |
| `memory_recall_timeout` | @4 超时降级 | 验证降级频率 |
| `memory_recall_degraded` | 降级转发 | 验证降级行为 |
| `intent_classified` | 每次意图分类 | label/confidence/source |
| `intent_calibration_mismatch` | 校准发现偏差 | 规则系统健康度 |
| `retrieval_track_completed` | 每个检索轨道完成 | 轨道耗时/候选数 |
| `scorer_completed` | HybridScorer 完成 | 候选数/Top-K 分数 |
| `surface_generated` | @e 生成 | surface_type/来源条数 |
| `surface_skipped` | @e 生成跳过 (无候选) | 验证 @d 缓冲充裕度 |
| `consolidation_started/completed/failed` | 沉淀管线 | 各步骤耗时/事件数 |
| `buffer_cleared` | @a/@d 清空 | 验证 2am 清理 |
| `memory_recall_tool_called` | memory_recall 工具调用 | 主动回合记忆探索频率 |
| `seed_data_imported` | 种子数据导入 | 初始化验证 |
| 既有 v2 指标 | 不变 | 参见 v2 第 11 节 |

所有记忆日志必须带 `turn_id`；沉淀日志带 `consolidation_id`。

---

## 12. 验收基线

### 12.1 app.py v2 接线验收

| # | 验收项 | 可观测判据 |
|---|---|---|
| 1 | app.py 启动后 ToolRegistry 非空 | 工具 schemas 日志输出 |
| 2 | app.py 启动后 WakeController 运行 | wake_controller_started 日志 |
| 3 | app.py 启动后 WakePlanner 运行 | wake_planner_started 日志 |
| 4 | 主动回合经过 ModelToolLoop | tool_loop_started 日志 |
| 5 | MEMORY_ENABLED=false 时行为与 v2 一致 | v2 全量回归通过 |

### 12.2 意图分类验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | "查一下上次聊的" | label=query, confidence>=0.8, source=rule |
| 2 | "今天天气真好" | label=no_query, confidence>=0.8, source=rule |
| 3 | "那天我们聊的那个东西" | 触发 LLM 兜底, source=llm_fallback |
| 4 | 规则和 LLM 均失败 | label=no_query (默认降级) |
| 5 | 第 N 条消息触发校准 | intent_calibration 日志 (不阻塞路由) |
| 6 | 校准发现偏差 | intent_calibration_mismatch 日志 |

### 12.3 @4 查询路径验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | 正常查询完成 (<20s) | mode=query, text 非空, @d 写入 |
| 2 | 查询超时 (>20s) | mode=degraded, text="", 后台任务继续 |
| 3 | 超时后 @d 最终写入 | 后续回合 read_recall_latest 有内容 |
| 4 | MEM_GEN 失败 | mode=degraded, text="" |
| 5 | 向量化失败 | 检索继续 (跳过向量轨道), 日志告警 |
| 6 | 图谱查询失败 | 检索继续 (跳过图谱轨道), 日志告警 |
| 7 | HybridScorer 排序正确 | 候选按分数降序, intent_type 路由正确 |
| 8 | 润色失败 | 使用 raw_content, 日志告警 |

### 12.4 @6 无查询路径验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | @e 有内容 | mode=no_query, text=@e 内容 |
| 2 | @e 为空 | mode=no_query, text="" (空值可读) |
| 3 | @e 读取后删除 | read_surface 第二次返回 None |
| 4 | @4 和 @6 不跨区 | 同一回合只读一个区域 |

### 12.5 @e 周期生成验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | @d 缓冲有内容 | surface_generated 日志, @e 写入 |
| 2 | @d 缓冲为空 | surface_skipped 日志 |
| 3 | FEEL_THRESHOLD=0.0 | 全部生成 association |
| 4 | FEEL_THRESHOLD=1.0 | 全部生成 impression |
| 5 | 生成周期含随机感 | 两次生成间隔不完全相同 |
| 6 | 生成器异常不崩溃 | 日志记录, 下一周期正常 |

### 12.6 新窗口衔接验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | X-Memory-Mode: new_window | mode=new_window, text=最近15条@d拼接 |
| 2 | @d 不足 15 条 | 拼接全部已有条目 |
| 3 | @d 为空 | text="" (空值) |
| 4 | 新窗口后正常回合 | 恢复 @4/@6 正常路径 |

### 12.7 memory_recall 工具验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | 主动回合工具暴露 | ToolRegistry schemas 包含 memory_recall |
| 2 | 用户回合不暴露 | ToolRegistry schemas 不包含 memory_recall |
| 3 | 主 LLM 调用 memory_recall | tool_call 执行, @d 更新, 结果回灌 |
| 4 | 工具超时 | tool_timeout 回灌 (15s) |
| 5 | 主 LLM 不调用工具 | 正常生成回复 |

### 12.8 2am 沉淀验收

| # | 场景 | 可观测判据 |
|---|---|---|
| 1 | 正常沉淀 | events 写入图谱, persona 更新, saga 归并 |
| 2 | @a/@d 清空 | clear_raw + clear_recall 日志 |
| 3 | W1 失败 | 管线中断, @a/@d 不清空 |
| 4 | W4 失败 | W1-W3 已持久化, @a/@d 清空 |
| 5 | 沉淀后检索 | 新事件可在后续 @4 检索到 |
| 6 | 向量化写入 | ChromaDB 可在后续向量检索到 |

### 12.9 集成与回归验收

| # | 验收项 | 可观测判据 |
|---|---|---|
| 1 | v2 全量回归 | v2 基线 (380 passed + 3 skipped) 不下降 |
| 2 | MEMORY_ENABLED=false | 系统行为与 v2 一致 |
| 3 | 种子数据导入 | 首次启动图谱非空 |
| 4 | ContextBuilder 向后兼容 | memory_recall=None 时从 samples.memories 渲染 |
| 5 | 操作性冒烟 | 从零启动、健康检查、重启后数据持久 |
| 6 | 凭据清理 | 代码与日志无真实 Key |

### 12.10 可迁移性验收

| # | 验收项 | 可观测判据 |
|---|---|---|
| 1 | 业务代码不含硬编码绝对路径 | grep 搜索无绝对路径 |
| 2 | 配置均来自环境变量 | 无裸字面量 |
| 3 | 本地与部署环境不改应用层接口 | 同一套代码 |
| 4 | ebbingflow vendored 组件不引用 vps-gateway config | import 搜索无 `from config import` |

---

## 13. 建议实现顺序

| 里程碑 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| M0 | app.py v2 组件接线补全 | v2 代码 | 12.1 |
| M1 | 记忆基础设施：GraphStore/PersonaStore/BufferStore 端口 + SQLite 适配器 + 表结构 + ChromaDB 初始化 | M0 | 单元测试：表创建/CRUD/递归 CTE 查询 |
| M2 | ebbingflow 组件 vendoring：15 个直接复用模块导入 + 3 个适配模块改写 + LLMBridge 解耦 | M1 | 单元测试：各模块可导入、基本功能可用 |
| M3 | MemoryPort + MemoryEngine 骨架 + BufferManager | M1, M2 | 单元测试：缓冲 CRUD、recall/after_turn 签名 |
| M4 | IntentClassifier（规则层 + LLM 兜底 + 周期校准） | M2, M3 | 12.2 |
| M5 | @4 查询路径完整链路（R2-R7 + 超时降级 + 润色） | M2, M3, M4 | 12.3 |
| M6 | @6 无查询路径 + SurfaceGenerator + RandomSurfaceSelector | M3, M5 | 12.4, 12.5 |
| M7 | memory_recall 工具 + 主动回合工具暴露 + 新窗口衔接 | M5, M6 | 12.6, 12.7 |
| M8 | ConsolidationPipeline（W1-W6 + 清理） | M2, M3 | 12.8 |
| M9 | ContextBuilder 适配 + TurnRunner 适配 + AppFactory v3 接线 + 种子数据导入 | M3-M8 | 12.9 |
| M10 | 全量回归 + 真实 API 冒烟 + 操作性冒烟 | M0-M9 | 12.9, 12.10 |

每个里程碑结束后运行新增测试和全量回归。

---

## 14. 扩展方向（必填）

### 14.1 Neo4j 升级

**当前状态：** `GraphStore` 端口已定义，`SqliteGraphStore` 是第一个适配器。

**扩展路径：**
```text
VPS 内存升级 (≥8GB)
  → Neo4jGraphStore (implements GraphStore)
  → 替换 SqliteGraphStore 注入
  → 上层不变
```

**需要实现的接口：** `Neo4jGraphStore`（实现 `GraphStore` ABC）。

**涉及代码变更：**
- 新增 `app/adapters/memory/neo4j_graph_store.py`
- `app/application/app.py` 注入替换
- 无上层变更

### 14.2 @e 选材算法升级

**当前状态：** `RandomSurfaceSelector` 纯随机挑选，`SurfaceSelector` 接口已预留。

**扩展路径：**
```text
ImpactSurfaceSelector
  → 基于 impact_score / confidence / 时间衰减 计算 surfaced 值
  → 加权随机选取
  → 替换 RandomSurfaceSelector 注入
```

**需要实现的接口：** `ImpactSurfaceSelector`（实现 `SurfaceSelector` ABC）。

### 14.3 "手表数据"和"身体"状态组件

**当前状态：** 预留 Sample 数据模型和注入占位，不实现。

**扩展路径：**
```text
手表 API → watch_data.sample.json → ContextBuilder 新增 <watch_data> 块
身体传感器 → body_state.sample.json → ContextBuilder 新增 <body_state> 块
```

**涉及代码变更：**
- 新增 Sample 数据模型
- ContextBuilder 渲染扩展
- SampleReader 扩展（降级策略：缺失则降级为空值）

### 14.4 状态自动演化（SamplePublisher）

**当前状态：** 沿用 v2，`SamplePublisher` 端口预留，不实现。

**扩展路径：** 沉淀管线中 PersonaManager 的输出可通过 SamplePublisher 回写 working_state.sample.json，实现情绪/焦点的自动更新。

### 14.5 "随心小屋"与"做梦"

**当前状态：** 不实现。V3 为其提供记忆基础设施。

**扩展路径：**
```text
3am 独立触发 (不受 WakeJob 08:00-24:00 约束)
  → 自由活动模式 (VPS 工具 + memory_recall 工具)
  → 留言生成 → Outbox 推送
  → 做梦 (基于记忆生成梦境) → 梦境存储
  → 留言+梦境同步前端
```

**涉及代码变更：**
- 新增"小屋"触发机制（独立于 WakeController）
- 新增梦境数据模型和存储
- 新增留言板数据模型和 API

### 14.6 既有 v2 扩展方向

可靠 ACK 与重新投递 / 多设备优先级 / 前端工具 / 多实例部署——不变，参见 v2 架构文档第 14 节。

---

## 15. 架构不变量

1. 前端不能上传、覆盖或执行工具；所有工具来自 VPS ToolRegistry。（v2 #1）
2. 同一组 tool_calls 必须按模型返回顺序执行。（v2 #2）
3. 工具失败不自动重试，必须作为 ToolResult 回灌模型。（v2 #3）
4. 工具循环最多 5 轮、每回合最多 10 次、单工具最多 15 秒。（v2 #4）
5. 达到工具上限后不再调用模型。（v2 #5）
6. 用户回合与一个主动回合允许并行。（v2 #6）
7. 两个主动回合永远不能并行；冲突任务直接 expired。（v2 #7）
8. 已过执行时间的主动任务不排队、不补跑。（v2 #8）
9. 唤醒只允许 Asia/Shanghai 的 08:00—24:00，最短间隔 20 分钟，每天最多实际启动 10 次。（v2 #9）
10. 未来唤醒没有最远期限，重启不得改变计划时间。（v2 #10）
11. 每个回合必须拥有独立 TurnContext、messages 和工具计数。（v2 #11）
12. Outbox 必须先 commit，后 notify。（v2 #12）
13. 长轮询等待期间不得持有 SQLite connection 或 transaction。（v2 #13）
14. 同时只能有一个活动长轮询读取者，每次最多领取一条。（v2 #14）
15. SQLite 不得把同一个 connection 长期共享给 HTTP 与 Scheduler。（v2 #15）
16. Scheduler 只能由 FastAPI lifespan 启动和停止，构造函数不得创建后台任务。（v2 #16）
17. 主 LLM 不感知记忆引擎存在；记忆注入是 system message 层的文本替换，不消耗主 LLM 的工具调用或额外 token。（v3 新增）
18. @4 和 @6 互斥：同一回合只读取单个区域（@d 或 @e），不跨区。（v3 新增）
19. @4 超时降级采用 γ 策略：后台继续，前台无记忆转发；不允许阻塞主 LLM 请求。（v3 新增）
20. @e 内容已读即删；@d 内容已读不删，仅 2am 清空。（v3 新增）
21. @a 跨平台跨窗口，不分前端不分对话窗口；2am 随沉淀清空。（v3 新增）
22. memory_recall 工具仅在主动唤醒回合暴露，用户回合不暴露。（v3 新增）
23. memories.sample.json 降级为种子数据；MemoryEngine 激活后 `<memories>` 来源由记忆引擎接管。（v3 新增）
24. ebbingflow vendored 组件不引用 vps-gateway 的 config.py；所有配置通过构造注入。（v3 新增）
25. 2am 沉淀是 MemoryEngine 内部定时器，不是 WakeJob，不受不变量 #9 时间窗口约束。（v3 新增）
26. 意图分类规则优先，LLM 兜底仅在不明确时触发；周期校准不参与实时路由。（v3 新增）
27. MEMORY_ENABLED=false 时系统行为与 v2 完全一致。（v3 新增）
28. 新能力默认进入第 14 节扩展方向，不改变已验收的闭环。（v2 #18）

> **v2 #17 继承说明**：v2 #17（"v2 不启用 Sample 自动回写和状态演化"）在 V3 中部分由 #23 和 #27 取代——memories.sample.json 降级为种子数据，`<memories>` 来源由 MemoryEngine 动态管理（但不回写 Sample 文件，MemoryEngine 管理独立的 SQLite/ChromaDB 存储）。working_state 和 preferences 的自动回写仍不启用（见扩展方向 14.4）。状态演化（情绪/焦点自动更新）仍不实现。

---

## 附录 A：8 模型任务表

| 模型 ID | 用途 | 类别 | 强度 | 触发频率 |
|---|---|---|---|---|
| MEM_EMBED | R2 查询向量化 + W5 消息向量化 | Embedding | — | 每查询 1 次 + 每日批量 |
| MEM_INTENT | R1 意图分类 LLM 兜底 + W3 事件校验 | 语义理解-轻 | 每回合最多 1 次 (uncertain 时) | 自适应 |
| MEM_GEN | R5 @d 记忆生成 | 语义理解-强 | 每查询 1 次, token 高 | 仅 @4 触发 |
| MEM_SURF | R6 @e 浮现生成 | 语义理解-中 | 每周期 1 次 | ~15min |
| MEM_EXTRACT | W1 事件/事实抽取 | 语义理解-强 | 批量处理 | 每日 1 次 |
| MEM_PERSONA | W2 人格观察与演化 | 语义理解-中 | 每日 1 次 | 每日 1 次 |
| MEM_SAGA | W4 Saga 聚类归并 | 语义理解-强 | 每日 1 次 | 每日 1 次 |
| MEM_POLISH | R7/W6 文学润色 | 润色-专用 | 跟随 R5/R6/W2/W4 | 跟随各任务 |

MEM_GEN / MEM_EXTRACT / MEM_SAGA 均为"强语义理解"，可指向同一模型实例，但配置保持独立。

## 附录 B：@0—@7 与 @a—@e 完整映射

| 标记 | 含义 | V3 模块 |
|---|---|---|
| @0 | 意图分类入口 | IntentClassifier |
| @1 | 追加历史信息到 @a | BufferManager.append_raw (after_turn) |
| @2 | 用户信息移步到 @b | TurnRunner 传递 raw_messages |
| @3 | 中转站内 LLM 自运行 | MemoryEngine |
| @4 | 需查询路径 | _run_query_path → _execute_retrieval_pipeline |
| @5 | 需查询，等待 @7 完成，读 @d | _run_query_path 中等待 task + read_recall_latest |
| @6 | 无需查询，读 @e | _run_surface_path → read_surface |
| @7 | 依据输入生成 @d；依据 @d 生成 @e | _execute_retrieval_pipeline / SurfaceGenerator |
| @a | 记忆原料区 | buffer_raw 表 |
| @b | 待发送区 | raw_messages (TurnRunner 参数) |
| @d | 记忆缓存区 | buffer_recall 表 |
| @e | 记忆浮现区 | buffer_surface 表 |
