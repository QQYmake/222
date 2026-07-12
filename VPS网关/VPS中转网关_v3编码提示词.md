# 沉的 VPS 中转网关 v3——编码提示词

> 用途：在 v2 基线（M0—M7 完成，380+3 测试通过）上增量引入认知记忆引擎。
> 使用前：确认第一节文件路径、第六节凭据状态、第五节执行节奏。
> 架构文档定义"做什么"；本提示词定义"如何计划、编码和验证"。

---

## 一、参考文件

请先完整读取以下文件，再执行任何修改：

| 文件 | 路径 | 用途 |
|---|---|---|
| V3 架构文档 | `VPS网关/VPS中转网关_v3架构文档.md` | 唯一功能范围、数据合同、模块接口、数据流、验收基线和实现顺序 |
| V2 架构文档 | `VPS网关/VPS中转网关_v2架构文档.md` | v2 基线参照——18 条不变量、v2 模块定义、v2 验收基线 |
| 现有项目 | `vps-gateway/` | 增量修改对象；不得另建替代项目 |
| 项目级 AGENTS.md | `vps-gateway/AGENTS.md` | 项目级约束、依赖方向、目录结构、关键入口 |
| 编码经验文档 | `README.md` | 工程控制论协作经验、反馈点和验证层级 |
| 技能目录 | `superpowers-main/` | 加载本任务要求的技能 |
| ebbingflow 源码 | 已克隆至 `/tmp/ebbingflow/` | 组件复用来源；vendoring 时从此处提取 |

路径规则：

1. 路径未填写或文件不可读时立即停止，明确指出缺失项。
2. 不猜测文件位置。
3. 不重新创建 v2 已有模块；先检查现有代码、测试、配置和依赖。
4. 若 v2 代码与 V3 架构文档描述不一致，以真实代码和测试结果为现状证据，以 V3 架构文档为目标合同。
5. 计划文件由 Agent 创建在 `vps-gateway/docs/superpowers/plans/YYYY-MM-DD-vps-gateway-v3.md`。
6. 若参考路径缺失，一次性列出全部缺项；不要每轮只问一个路径。
7. 架构文档若仍含 `[待配置]` 或状态不是"待审阅"，停止并一次性报告全部未冻结项。
8. ebbingflow 源码若不在 `/tmp/ebbingflow/`，需从 GitHub 重新克隆 `MMX920/ebbingflow`。

---

## 二、技能加载与流程

按以下顺序加载并执行技能：

### 阶段 1——启动与取证

```text
using-superpowers
  → 读取 V3 架构文档、V2 架构文档、现有项目、项目级 AGENTS.md 和编码经验文档
  → 检查 pyproject.toml、uv.lock、测试目录和 Git 状态
  → 保存初始 git status --short、git diff --stat 和相关文件 diff 作为用户变更基线
  → 运行 v2 全量测试，记录真实测试基线 (预期 380 passed + 3 skipped)
  → 确认 ebbingflow 源码位置 (/tmp/ebbingflow/)
  → 检查 v2 app.py 组件接线缺失（ToolRegistry/WakeController/WakePlanner/ModelToolLoop 未注入）
  → 检查现有 memories.sample.json 内容（V3 降级为种子数据）
```

本阶段必须产出：

- 当前测试通过数与失败数；
- 当前模块—文件映射；
- v2 与 v3 的差异清单（含 app.py 接线缺失）；
- 环境基线：Python、FastAPI、httpx、SQLite、uv、pytest 版本；chromadb、rank-bm25 是否已安装；
- ebbingflow 组件清单（15 直接复用 + 3 适配 + 新开发模块，详见架构文档 3.3 节和 4 节）；
- 用户既有未提交变更基线；
- 仅列会阻塞实现的决策点。

### 阶段 2——实现计划

```text
writing-plans
  → 严格按 V3 架构文档第 13 节 M0—M10 顺序
  → 每个 Task 只写伪代码
  → 每个 Task 标注输入、处理指令、输出、接口和测试
  → 标明 @4/@6/new_window/主动唤醒/@e生成/2am沉淀的数据流
  → 标注 ebbingflow 组件复用来源（直接复用/适配/新开发）
  → 不在计划中写完整实现代码
```

计划必须设置以下反馈点：

1. M0 完成：证明 app.py v2 组件接线补全，ToolRegistry/WakeController/WakePlanner 运行。
2. M2 完成：证明 ebbingflow 组件 vendoring 完成，各模块可导入、基本功能可用。
3. M5 完成：证明 @4 查询路径完整闭环（含超时降级 γ）。
4. M6 完成：证明 @6 无查询路径 + @e 周期生成器运行。
5. M8 完成：证明 2am 沉淀管线完整执行并清空缓冲。
6. M9 完成：证明 ContextBuilder 替换 `<memories>` + TurnRunner 注入 MemoryPort + app.py 完整接线。
7. M10 完成：全量回归不下降、MEMORY_ENABLED=false 向后兼容、真实 API 冒烟、操作性冒烟。

### 阶段 3——执行

每个 Task 都执行：

```text
test-driven-development
  → 先写失败测试
  → 运行并确认按预期失败
  → 写最小实现
  → 运行新增测试直到通过

executing-plans
  → 按 M0—M10 顺序推进
  → 不跳过依赖里程碑
  → 每个里程碑结束运行全量回归
  → 记录测试数量变化

systematic-debugging（出现失败时）
  → 先读取错误、堆栈和相关数据流
  → 找到根因后做局部修复
  → 禁止为了让测试变绿而削弱架构不变量
  → ebbingflow 适配问题时：先确认原模块行为，再确认适配点，最后确认 vps-gateway 端口契约
```

### 阶段 4——完成前验证

```text
verification-before-completion
  → 运行全量回归
  → 逐项对照 V3 架构文档第 12 节（12.1—12.10）
  → 执行真实 API 冒烟（MEMORY_ENABLED=true，8 模型配置真实端点）
  → 执行 MEMORY_ENABLED=false 向后兼容验证
  → 执行服务启动、关闭、重启恢复冒烟
  → 执行种子数据导入冒烟
  → 检查凭据和日志无泄漏
  → 检查 ebbingflow vendored 组件不引用 vps-gateway config.py

requesting-code-review
  → 审查模块边界、接口一致性、并发隔离、记忆注入隔离和测试遗漏
  → 审查 ebbingflow 适配正确性（Neo4j→SQLite 递归 CTE）
  → 审查缓冲区读写规则（@e 已读即删 / @d 已读不删 / @a 2am 清空）
```

只有存在 Git 分支且用户要求集成时，才执行 `finishing-a-development-branch`。不要自行提交、推送或创建 PR。

### 本任务对技能默认流程的显式覆盖

以下是用户对本任务的直接要求，优先于技能中的通用默认流程：

- 不创建 git worktree；直接在现有 vps-gateway 项目中增量修改。
- 不自动 commit、push 或创建 PR。
- `writing-plans` 只采用任务拆分与依赖分析；计划正文只写伪代码，不写完整实现代码。
- `executing-plans` 在当前会话连续执行 M0—M10，不要求切换独立会话。
- 计划保存后进行一次自动一致性检查；无 Critical/Important 问题即进入 M0，不等待人工批准。
- 有子代理/reviewer 时进行独立计划审查；没有时由当前 Agent 按架构第 12、13、15 节逐项自检。
- 代码审查针对当前工作树 diff 与实际文件，不依赖提交 SHA。
- **基于开源项目修改，不重复造轮子**：ebbingflow 组件优先直接复用或适配，仅编排和新功能模块从零开发。
- 后续必须使用 superpowers 展开任务。

---

## 三、技术栈与固定选择

| 维度 | 选择 |
|---|---|
| 开发方式 | 在现有 vps-gateway v2 中增量修改 |
| 语言 | Python 3.12+；沿用 `pyproject.toml` |
| HTTP 框架 | FastAPI |
| 上游协议 | OpenAI Chat Completions |
| HTTP 客户端 | httpx；异步 |
| 主数据库 | SQLite；每次操作短连接，不长期共享 connection |
| 向量数据库 | ChromaDB 嵌入式模式（无独立服务进程） |
| 图谱存储 | SQLite（递归 CTE 替代 Neo4j Cypher） |
| 嵌入模型 | sentence-transformers（本地）或 OpenAI 兼容 API（可选） |
| 关键词检索 | rank-bm25（pip 依赖） |
| 记忆引擎基础 | ebbingflow（vendored 至 `app/adapters/memory/ebbingflow/`） |
| 包管理器 | uv |
| 测试框架 | pytest + pytest-asyncio |
| 方法 | TDD |
| 运行形态 | 单进程、单实例；禁止多 worker |
| 工具来源 | 仅 VPS ToolRegistry；memory_recall 工具仅主动唤醒回合暴露 |
| 状态组件 | v2 Sample 只读保留；memories.sample.json 降级为种子数据 |
| 记忆开关 | MEMORY_ENABLED 环境变量控制；false 时行为与 v2 完全一致 |

不得未经批准更换框架、数据库、包管理器、协议或目录体系。

新增依赖（需加入 `pyproject.toml`）：
- `chromadb`——向量存储
- `rank-bm25`——BM25 关键词检索
- `sentence-transformers`——本地嵌入模型（MEM_EMBED_TYPE=local 时）

---

## 四、计划格式

实现计划只写伪代码。每个 Task 必须使用以下格式：

```text
Task [编号]：[名称]

ebbingflow 来源：
- [直接复用: 模块名 → 目标路径]
- [适配: 模块名 → 适配说明]
- [新开发: 模块名]

修改位置：
- [现有文件]
- [新增文件]

数据输入：
- 来源
- 类型
- 约束

处理指令：
1. 一个原子步骤
2. 一个原子步骤
3. 错误/冲突处理

数据输出：
- 类型
- 去向
- 可观察结果

接口关系：
[上游模块] → [本 Task] → [下游模块]

测试：
- 先失败的测试
- 通过判据
```

计划中必须单独画出以下数据流：

1. 用户回合 @6 无查询路径（意图分类 → 读 @e → 替换 `<memories>` → 主 LLM）。
2. 用户回合 @4 查询路径（意图分类 → 检索管线 → @d 生成 → 润色 → 超时降级 γ）。
3. 新窗口衔接（X-Memory-Mode → 读最近 15 条 @d → 拼接转发）。
4. 主动唤醒 + memory_recall 工具（伪用户输入 → @6 → 工具暴露 → 主 LLM 调用 → @4 流程）。
5. @e 周期生成（独立后台任务 → 扫描 @d → 选材 → LLM → 润色 → 写 @e）。
6. 2am 沉淀管线（W1→W2→W3→W4→W5→W6 → 持久化 → 清空 @a/@d）。
7. ebbingflow 组件复用映射（15 直接复用 / 3 适配 / 新开发模块的依赖关系，详见架构文档 3.3 节）。

---

## 五、执行节奏

采用以下节奏：

- 连续执行 M0—M10。
- 每个里程碑完成后运行新增测试和全量回归，记录测试数量变化。
- 不为普通可逆实现细节暂停询问，采用架构文档默认值。
- 只有以下情况才停止并请求用户：
  1. 架构文档存在互相矛盾的合同；
  2. 真实 v2 代码使目标无法局部实现；
  3. ebbingflow 组件行为与 V3 架构文档描述矛盾且无法适配；
  4. 需要数据删除、凭据变更或不可逆外部操作；
  5. 必须更换固定技术栈。
- "手表数据"和"身体"状态组件不进入本轮，即使实现过程中发现可顺手加入。
- 计划文件保存并完成自动一致性检查后立即进入 M0，不等待人工确认。
- 保留用户已有未提交变更：不得撤销、覆盖、暂存或归因给本轮；无关变更绕开。只有目标文件存在无法安全合并的重叠时才停止。

---

## 六、凭据管理

### 6.1 主 LLM 凭据

沿用 v2 配置（`.env` 中已有上游 API Key）。

### 6.2 记忆引擎 8 模型凭据

V3 新增 8 个独立模型配置。以下为可用端点（与主 LLM 可共用或独立）：

```text
# 记忆引擎模型配置（每个模型独立配置 base_url / api_key / model）
# 以下为推荐配置，用户可在 .env 中覆盖

MEM_EMBED_TYPE=local
MEM_EMBED_MODEL=paraphrase-multilingual-MiniLM-L12-v2

MEM_INTENT_BASE_URL=https://api.deepseek.com
MEM_INTENT_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_INTENT_MODEL=deepseek-chat

MEM_GEN_BASE_URL=https://api.deepseek.com
MEM_GEN_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_GEN_MODEL=deepseek-chat

MEM_SURF_BASE_URL=https://api.deepseek.com
MEM_SURF_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_SURF_MODEL=deepseek-chat

MEM_EXTRACT_BASE_URL=https://api.deepseek.com
MEM_EXTRACT_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_EXTRACT_MODEL=deepseek-chat

MEM_PERSONA_BASE_URL=https://api.deepseek.com
MEM_PERSONA_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_PERSONA_MODEL=deepseek-chat

MEM_SAGA_BASE_URL=https://api.deepseek.com
MEM_SAGA_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_SAGA_MODEL=deepseek-chat

MEM_POLISH_BASE_URL=https://api.deepseek.com
MEM_POLISH_API_KEY=sk-d9cee49798ce480788a463169406a58b
MEM_POLISH_MODEL=deepseek-chat
```

凭据安全规则：

- `.env` 必须被 `.gitignore` 排除。
- `.env.example` 只能包含空占位符。
- 交付前搜索代码、测试输出和日志，确认无真实 Key。
- 不修改或删除用户已有凭据，除非用户明确要求。
- 冒烟测试中只允许读取 `.env` 中的凭据；禁止输出或写入日志。
- 测试完成后由用户决定是否删除 key。

---

## 七、架构执行约束

以下约束优先级高于局部实现便利：

### 7.1 并发边界

- 沿用 v2 全部并发边界（不变量 #6—#16）。
- 用户回合与一个主动回合允许并行。
- 两个主动回合永远串行；冲突任务直接 expired。
- @4 检索管线超时后后台 asyncio.Task 继续运行，不阻塞主 LLM 请求（γ 降级）。
- @e 周期生成器是独立后台任务，不持有回合级锁。
- 2am 沉淀管线是独立后台任务；@a/@d 清空时用短事务，不影响正在进行的 recall。
- memory_recall 工具调用在 ModelToolLoop 内顺序执行，沿用 v2 工具循环约束。

### 7.2 记忆引擎边界

- 主 LLM 不感知记忆引擎存在；记忆注入是 system message 层的文本替换。
- @4 和 @6 互斥：同一回合只读取单个区域，不跨区。
- @e 内容已读即删；@d 内容已读不删，仅 2am 清空。
- @a 跨平台跨窗口，不分前端不分对话窗口；2am 随沉淀清空。
- memory_recall 工具仅在主动唤醒回合暴露，用户回合不暴露。
- memories.sample.json 降级为种子数据；MemoryEngine 激活后 `<memories>` 来源由记忆引擎接管。
- 意图分类规则优先，LLM 兜底仅在不明确时触发；周期校准不参与实时路由。
- MEMORY_ENABLED=false 时系统行为与 v2 完全一致。

### 7.3 ebbingflow 复用边界

- ebbingflow vendored 组件位于 `app/adapters/memory/ebbingflow/`，属于适配器层。
- vendored 组件不引用 vps-gateway 的 config.py；所有配置通过构造注入。
- vendored 组件不直接引用应用层或领域层。
- Neo4j 相关代码必须适配为 SQLite 递归 CTE；不保留 Neo4j driver 依赖。
- LLMBridge 解耦 ebbingflow 的 `core.monitoring.token_monitor`；改为可选回调。
- 直接复用模块（15 个）零改动或仅 import 路径调整；适配模块（3 个）只改存储层和配置注入，不改业务逻辑。

### 7.4 工具边界

- 沿用 v2 全部工具边界（不变量 #1—#5）。
- memory_recall 工具遵循 v2 工具循环约束：最多 5 轮、10 次、单工具 15 秒。
- ToolRegistry 增加 `register_for_wake_only(tool)` 方法，标记仅在主动唤醒回合暴露的工具。

### 7.5 唤醒边界

- 沿用 v2 全部唤醒边界（不变量 #9—#10）。
- 2am 沉淀是 MemoryEngine 内部定时器，不是 WakeJob，不受 08:00—24:00 约束。
- 主动唤醒回合走 @6 无查询路径（明确为无需查询状态），同时暴露 memory_recall 工具。

### 7.6 Outbox 边界

- 沿用 v2 全部 Outbox 边界（不变量 #12—#14）。

### 7.7 范围边界

- 不引入 Neo4j、PostgreSQL。
- 不做流式响应（SSE）。
- 不做前端。
- 不做"随心小屋"和"做梦"。
- 不修改 v2 identity/optional Sample 降级规则。
- 不降低现有测试覆盖来适配新代码。
- "手表数据"和"身体"仅预留接口，不实现逻辑。

---

## 八、错误与可观测性要求

所有错误必须符合 V3 架构文档第 8 节，并记录可关联 ID：

- 用户回合使用 `request_id/turn_id`。
- 主动回合使用 `wake_id/turn_id`。
- 工具使用 `tool_call_id/turn_id`。
- Outbox 使用 `event_id/trigger_id`。
- 记忆操作使用 `turn_id`（recall/after_turn）。
- 沉淀管线使用 `consolidation_id`。

记忆引擎特有可观测要求：

- 每次 recall 记录 `memory_recall_started/completed`（mode/text 长度/耗时）。
- 超时降级记录 `memory_recall_timeout` + `memory_recall_degraded`。
- 每次意图分类记录 `intent_classified`（label/confidence/source）。
- 校准偏差记录 `intent_calibration_mismatch`。
- @e 生成记录 `surface_generated/skipped`。
- 沉淀记录 `consolidation_started/completed/failed`。
- 缓冲清空记录 `buffer_cleared`。
- memory_recall 工具调用记录 `memory_recall_tool_called`。

不得：

- 把工具内部堆栈回灌模型或返回前端；
- 把 API Key、完整 Sample 或完整对话写日志；
- 把记忆引擎内部 LLM 的原始输出直接注入主 LLM（必须经过润色）；
- 捕获异常后假装成功；
- 在写入失败时通知长轮询；
- 在工具/唤醒/记忆引擎失败时自动无限重试。

---

## 九、测试与验收要求

以 V3 架构文档第 12 节为唯一验收基线。执行前生成完整"验收追踪矩阵"，不得用本提示词中的摘要替代任何一项：

```text
架构验收编号 (12.1—12.10)
  → 对应测试文件/测试名
  → 执行命令
  → 预期结果
  → 实际证据
```

第 12 节中的每一行都必须出现在矩阵中。

验证分三层：

```text
单元/集成测试：Mock 上游 LLM，验证全部控制分支
MEMORY_ENABLED=false 回归：验证向后兼容
真实冒烟：实际 API 端点，验证记忆检索→生成→注入闭环
```

每个里程碑结束后：

- 运行该里程碑新增测试；
- 运行全量回归；
- 记录通过数、失败数和耗时；
- 不以 v2 的"380 passed + 3 skipped"替代当前真实执行结果。

回归基线规则：

- v2 基线：380 passed + 3 skipped。
- 所有新增 v3 测试必须通过。
- 相对 v2 基线不得新增失败或跳过。
- 既有失败只记录，除非直接阻塞 v3，不扩大范围修复。
- 最终报告必须分别列出"既有失败"和"新增回归"。

ebbingflow 适配测试专项：

- 适配模块（knowledge_engine / persona_manager / sql_pool）必须有独立适配测试。
- 适配测试验证：原模块行为不变 + 适配点行为正确（SQLite 递归 CTE 查询结果与预期一致）。
- 直接复用模块的测试：验证导入成功 + 基本功能可用（不重复 ebbingflow 原有测试）。

---

## 十、任务

依据 V3 架构文档第 13 节，从 M0 开始，连续完成 M0—M10：

```text
M0  app.py v2 组件接线补全（ToolRegistry/WakeController/WakePlanner/ModelToolLoop）
M1  记忆基础设施：GraphStore/PersonaStore/BufferStore 端口 + SQLite 适配器 + 表结构 + ChromaDB 初始化
M2  ebbingflow 组件 vendoring：15 个直接复用 + 3 个适配 + LLMBridge 解耦
M3  MemoryPort + MemoryEngine 骨架 + BufferManager
M4  IntentClassifier（规则层 + LLM 兜底 + 周期校准）
M5  @4 查询路径完整链路（R2-R7 + 超时降级 γ + 润色）
M6  @6 无查询路径 + SurfaceGenerator + RandomSurfaceSelector
M7  memory_recall 工具 + 主动回合工具暴露 + 新窗口衔接
M8  ConsolidationPipeline（W1-W6 + 清理）
M9  ContextBuilder 适配 + TurnRunner 适配 + AppFactory v3 接线 + 种子数据导入
M10 全量回归 + 真实 API 冒烟 + 操作性冒烟 + 向后兼容验证
```

开始编码前，先保存实现计划并进行自动一致性检查；计划只能包含伪代码、数据流、接口与验收，不写完整实现。无 Critical/Important 问题即直接进入 M0。

**ebbingflow 复用提醒**：每个 Task 在计划中必须标注 ebbingflow 来源（直接复用/适配/新开发）。直接复用模块不做逻辑改动；适配模块只改存储层；新开发模块从零编写但遵循 ebbingflow 的数据模型和接口风格。

---

## 附录：V3 与 V2 差异速查

| 维度 | V2 | V3 |
|---|---|---|
| app.py 接线 | 遗漏（ToolRegistry 等未注入） | M0 补全 + 记忆引擎注入 |
| `<memories>` 来源 | memories.sample.json 静态读取 | MemoryEngine 动态生成（润色后替换） |
| memories.sample.json | 运行时读取 | 种子数据（首次初始化导入） |
| 意图分类 | 无 | 三层置信度路由 |
| 记忆检索 | 无 | 多轨检索 + HybridScorer 重排 |
| 记忆注入 | 无 | system message 文本替换（主 LLM 无感） |
| 后台任务 | Scheduler + 长轮询 | + @e 周期生成器 + 2am 沉淀管线 |
| LLM 配置 | 单一上游 | + 8 个独立记忆模型配置 |
| 向量存储 | 无 | ChromaDB 嵌入式 |
| 图谱存储 | 无 | SQLite（递归 CTE） |
| BM25 检索 | 无 | rank-bm25 |
| 工具暴露 | 全回合统一 | 用户回合无 memory_recall；主动回合有 |
| 新窗口 | 无 | X-Memory-Mode: new_window |
| 不变量 | 18 条 | 28 条（v2 #1-#16, #18 保留 17 条 + v3 新增 11 条；v2 #17 部分由 v3 #23, #27 取代） |
| 实现顺序 | M0—M7 | M0—M10 |
