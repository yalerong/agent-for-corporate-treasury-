# HISTORY.md — 项目演进与决策记录

> **Claude Code 自我进化上下文**
>
> 记录项目的重大决策、配置变更和迭代历史。每次会话结束时，Claude 应反思本次改动，
> 并在此文件中追加记录。这帮助后续会话快速恢复上下文，也对应 Anthropic 推荐的
> "配置随模型进化，3-6 个月审查一次"原则。

---

## 项目元信息

- **项目名称**: Corporate Treasury Agent（企业资金智能体）
- **启动日期**: 2026-05-15
- **当前阶段**: Phase 1 — 最小可运行骨架（自 2026-05-16）
- **上一阶段**: Phase 0（方向澄清与文档基线）完成于 2026-05-16
- **技术负责人**: [待填写]
- **业务负责人**: [待填写]
- **下次配置审查**: 2026-08-15

**当前实际文件状态（2026-05-16，Phase 2 完整版落地后）**:
- 配置基线：`requirements.txt`、`.env.example`、`.gitignore`、`pyproject.toml`、`app/config.py`、`app/__init__.py`
- LLM 客户端：`app/llm.py`（lru_cache 单例，OpenAI 协议兼容层）
- Agent 节点：`app/agents/{nodes,__init__}.py`（supervisor LLM 意图分类、knowledge 真实接 Tool + LLM 合成、HITL 横切）
- 状态图：`app/graph/{state,routing,__init__}.py`（MemorySaver checkpointer + 条件边）
- CLI：`main.py`（thread_id + 中断检测）
- 最小 RAG：`app/rag/{store,knowledge_base,__init__}.py` + `knowledge_base/{industry,enterprise}/*.md` 占位 stub
- 业务 Tool 层：`app/tools/{audit,masking,knowledge,__init__}.py`
- API 服务：`app/api.py`（chat / approvals / knowledge / audit/logs / healthz 五端点 + graph 单例）
- 记忆模块：`app/memory/__init__.py`（Phase 3 持久化 checkpointer 接入点占位）
- 热更新脚本：`scripts/update_enterprise_kb.py`
- 测试：10 个测试文件，共 **136 个测试** 2.50s 全绿
- **占位/未做**：Tool 仅 search_*_knowledge 两个真实业务（其他 Tool 类如 check_position / execute_transfer 待真实业务系统对接）；cashier / treasury_supervisor 节点仍 placeholder；脱敏映射无真实数据；持久化 checkpointer / Docker / MCP / 银企对接为 Phase 3

---

## 决策记录（Architecture Decision Records）

### ADR-001: Agent 框架选型 — LangGraph vs AgentExecutor

**日期**: 2026-05-15
**状态**: 已采纳

**背景**: 需要支持多角色（出纳、资金主管、资金经理）协作，且资金领域对流程控制、审批流、人工介入要求严格。

**决策**: 采用 **LangGraph** 作为核心编排框架，而非传统的 `AgentExecutor`。

**理由**:
1. LangGraph 基于状态机的图编排，能精确控制"审批 → 执行 → 反馈"的循环逻辑
2. 原生支持 Human-in-the-Loop（HITL），在关键节点插入人工确认
3. 支持子图（Sub-graph），便于未来拆分跨境资金池、外汇套保等独立模块
4. 内置记忆管理（checkpoint），适合资金流程的长会话状态保持

**代价**: 学习曲线比 AgentExecutor 陡峭，需要理解 StateGraph、Nodes、Edges 概念。

**替代方案**: `AgentExecutor`（简单但无法控制复杂审批流）、AutoGen（微软，但社区生态和 Python 类型支持不如 LangChain）

---

### ADR-002: 双轨知识库架构 — Industry + Enterprise

**日期**: 2026-05-15
**状态**: 已采纳

**背景**: 资金 Agent 需要同时回答"法规要求是什么"和"我们公司怎么规定的"。两类知识更新频率、负责人、敏感度完全不同。

**决策**: 物理隔离为两个 Qdrant Collection：
- `treasury_industry`: 行业通用法规、监管政策、通用案例（低频更新）
- `treasury_enterprise`: 企业内部制度、操作流程、业务数据（高频迭代，可热更新）

**理由**:
1. 企业库可以独立重建（`scripts/update_enterprise_kb.py`），不影响行业库
2. 权限控制更灵活：未来可对企业库按角色做细粒度访问控制
3. 避免大文件行业库拖慢企业库的检索速度

**代价**: 查询时需要两次向量检索（或合并检索），延迟增加约 50-100ms（可接受）。

**替代方案**: 单 Collection + metadata filter（实现简单，但无法独立重建企业库）

---

### ADR-003: Embedding 模型选型 — 本地 BGE vs OpenAI Embedding

**日期**: 2026-05-15
**状态**: 已采纳

**背景**: 资金文档包含大量专业术语（头寸、套期保值、跨境资金池、全口径宏观审慎），需要高质量中文语义理解。

**决策**: 使用 **BAAI/bge-large-zh-v1.5**（本地 CPU 推理），不调用 OpenAI Embedding API。

**理由**:
1. 节省 API 费用：知识库索引阶段 embedding 调用量极大，本地模型零边际成本
2. 数据隐私：资金制度文档不出境、不上传第三方
3. BGE 在中文语义匹配（特别是法规条文类长文本）上表现优于 Ada-002
4. 离线可用，不受网络抖动影响

**代价**: 首次下载模型约 1.2GB；CPU 推理速度比 GPU 慢 5-10 倍（但索引是离线批处理，不影响在线查询）。

**未来可切换**: 如后期性能瓶颈，可无缝替换为 `BAAI/bge-m3`（多语言更强）或 `text-embedding-3-large`（OpenAI）。

---

### ADR-004: 角色权限隔离模型

**日期**: 2026-05-15
**状态**: 已采纳

**背景**: 资金领域对岗位职责分离（SOD）要求极高。出纳不能审批，经理不能直接付款。

**决策**: 采用 **三岗 Agent + Supervisor 路由** 架构：
- Cashier Agent: 仅执行权
- Treasury Supervisor Agent: 计划权 + 日常审批权（≤阈值）
- Treasury Manager Agent: 投资决策权 + 大额审批权 + 合规认定权
- Supervisor Agent: 意图识别、权限校验、流程编排

**理由**:
1. 每个 Agent 的 System Prompt 明确锁定其 Tool 权限，物理隔离越权操作
2. Supervisor 在每次路由前检查 `user_role` + `AUTHORIZATION_MATRIX`
3. 符合企业内控审计要求：每个操作可追溯至具体 Agent 角色

**代价**: 跨角色协作需要多次 LLM 调用（Supervisor → 角色 Agent → Supervisor），Token 成本增加。

**风险缓解**: 简单查询（纯知识问答）可跳过 Supervisor，直接由单一 Agent 处理。

---

### ADR-005: 敏感数据脱敏策略

**日期**: 2026-05-15
**状态**: 已采纳

**背景**: 银行账户、客户名称、交易对手信息属于核心商密，不能进入 LLM 上下文。

**决策**: **脱敏代号映射表**（Masking Map）
- LLM 和 Agent 只操作脱敏代号：`POOL-001`、`ACC-工行-001`
- 真实账号在 Tool 层（或下游网关）通过映射表翻译
- 企业知识库中的真实账号在入库前批量脱敏

**理由**:
1. 比"数据加密后送入 LLM"更可靠（LLM 可能从上下文中推断出真实信息）
2. 审计日志中也可记录脱敏代号，避免泄露
3. 实现成本低，在 `app/config.py` 中维护一个字典即可

**代价**: 需要维护映射表；Tool 层需要额外的翻译逻辑。

---

### ADR-006: LangGraph 与 LangChain 职责分工

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: 项目同时依赖 LangGraph 和 LangChain，但两者抽象有重叠（如 Tool 概念在两边都存在），容易写出"LangGraph 节点直接调原始函数"或"用 AgentExecutor 取代 StateGraph"的混乱代码。

**决策**: 在 `DESIGN.md §4.2.0` 明确职责分工：
- **LangGraph 负责骨架**：`StateGraph`、`Node`、`Edge`、`interrupt` — 流程控制层
- **LangChain 负责零件**：`@tool`、`BaseMessage`、`ChatModel`、`Embeddings`、`VectorStore` — 工具与模型抽象层
- 6 条具体落地约定写入文档，含禁忌清单（不许用 `AgentExecutor` 取代 StateGraph、不许在 Tool 内阻塞等 HITL 等）

**理由**:
1. 两个框架版本节奏不同，明确边界后升级一边不会拖另一边
2. Tool 用 `@tool` 装饰器 + 类型注解自动生成 schema，比手写更可靠
3. LLM 走 LangChain 抽象层后，多模型切换（§7.3）实现成本低
4. 防止退化为 `AgentExecutor`（已在 ADR-001 否决）

**代价**: 文档边界需要持续维护；新人入项目要先读 §4.2.0。

---

### ADR-007: route_by_intent 路由修复与状态图重画

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: DESIGN.md 早期版本的 `route_by_intent` 伪代码有多处 bug：
- transfer 且金额 ≤ 500 万时落入默认分支 `"knowledge"`（**路由缺口**，安全风险）
- `current_role` 字面量值 `supervisor_treasury` 与路由返回值 `treasury_supervisor` 不一致
- 代码引用了未在 `TreasuryState` 中定义的 `approved_instruction_id` 字段
- 状态图与 §5.1 调拨流程示例对路由 vs 编排的描述自相矛盾
- HITL 仅绑定在 manager 节点下方，但 §5.1 显示 transfer 也需要 HITL

**决策**:
- 补全 transfer 中等金额分支（→ `treasury_supervisor`）和出纳已审批分支（→ `cashier`）
- `current_role` / `user_role` 全部统一为 `treasury_*` 命名，用 `Literal` 类型锁定
- `TreasuryState` 补 `approved_instruction_id: Optional[str]` 字段
- 状态图改画为多阶段编排，HITL 提为**横切节点**（任何 Agent 通过 `requires_approval` 置位触发）
- Knowledge Agent 明确为 Supervisor 内联调用双轨检索的逻辑分支，非独立 Agent 进程

**理由**:
1. 路由缺口若进入生产，5M 以下调拨会被误送到知识库 Agent，是确定性 bug
2. 命名不一致是路由代码的高频 bug 源；Literal 类型让 mypy / IDE 静态发现
3. HITL 横切设计避免每个 Agent 重复实现审批等待逻辑

**代价**: 状态图比原版略复杂；测试需要覆盖 transfer 阈值边界（5,000,000 含等于走主管、5,000,000.01 走经理）。

---

### ADR-008: 配置层单一权威 + Settings 单例

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: 资金阈值、角色权限、沙箱开关一旦在多处重复定义（Prompt 里写死、路由代码里硬编码、测试里复制），会出现"改一处忘改另一处"的事故。

**决策**: `app/config.py` 集中维护：
- `AUTHORIZATION_MATRIX: dict[UserRole, frozenset[Intent]]` — 唯一权威，所有权限判断走 `role_can()`
- `Thresholds` 类 — 所有金额阈值的唯一来源，强制 `Decimal` 类型禁用 `float`
- `Settings(BaseSettings)` + `@lru_cache` 单例 — 环境变量驱动，测试可 `cache_clear()` 重置
- `SecretStr` 包裹 API Key，repr/str 自动掩码防日志泄露

**理由**:
1. 测试反向锁定：`Thresholds.LARGE_TRANSFER == Decimal("5000000")` 改任何一边都炸
2. `frozenset` 让权限矩阵不可变，运行时防御
3. `Decimal` 规避 `0.1 + 0.2 = 0.30000000000000004` 这类金融场景禁忌
4. 单例避免重复构造与重复读 `.env`

**代价**: 测试需要在 fixture 里 `cache_clear()` + 清空 `LLM_/QDRANT_/...` 前缀环境变量避免污染。

---

### ADR-009: RAG 重依赖延迟导入 + 可注入设计

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: `langchain_huggingface.HuggingFaceEmbeddings` 间接依赖 `torch + transformers + sentence-transformers`，安装包合计约 1.5GB。若 `app/rag/store.py` 在模块顶层 import，则跑任何 RAG 测试都强制装这一堆。

**决策**:
- `get_embeddings()` 内部 `from langchain_huggingface import HuggingFaceEmbeddings`（延迟导入）
- `get_store(track, embeddings, client=None, collection_name=None)` 全部依赖可注入
- 测试用自实现的 `DeterministicFakeEmbeddings`（词袋哈希 → 归一化向量）+ Qdrant `:memory:` 模式
- `index_to_store(store, path, category)` 与 `index_track(track)` 拆为可测纯函数与便利包装

**理由**:
1. CI 跑测试不需要下载 torch；本地开发想用 HF 时再 `pip install langchain-huggingface sentence-transformers`
2. Fake embeddings 是确定性的（同 token 落同桶），不需要随机种子，结果可重复
3. 可注入设计让真实 Qdrant 文件模式和测试 `:memory:` 模式共用代码

**代价**: `DeterministicFakeEmbeddings` 是约 25 行测试专属代码；读者需要知道测试用 fake 而非真实模型。

---

### ADR-010: HITL 实现 — interrupt() + MemorySaver + thread_id

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: DESIGN.md §4.2.2 状态图将 HITL 设计为横切节点，由 `requires_approval` 触发。LangGraph 提供两套 HITL 机制：`interrupt_before/_after` 的编译期配置 vs `interrupt(payload)` 的运行时函数调用。

**决策**:
- 使用运行时 `interrupt(payload)` 函数：在 `hitl_node` 内调用，可携带任意 payload 给外部审批方
- 图编译时挂 `MemorySaver` checkpointer（LangGraph 强制要求；没有 checkpointer 时 interrupt 无法暂停/恢复）
- 所有 `invoke` 调用必须带 `config={"configurable": {"thread_id": "..."}}`；CLI 自动 UUID 生成
- 恢复执行用 `Command(resume=payload)`；payload 形如 `{"approved": bool, "instruction_id": str, "reason": str}`
- 只在 `treasury_manager` 之后挂条件边（Phase 2 最小实现）；其余 Agent 通过 `requires_approval=True` 触发的扩展留待 Phase 3

**理由**:
1. `interrupt()` 比 `interrupt_before` 更灵活：可在节点内根据条件决定是否中断，且 payload 可定制
2. MemorySaver 适合开发期；生产用 Postgres/Redis checkpointer 时只需替换实例，节点代码不变
3. thread_id 由 CLI 生成并打印，外部审批系统按 thread_id 恢复，符合"审计可追溯"红线

**代价**:
- 所有 `invoke` 测试必须传 thread_config，conftest 加了 fixture
- 单机内存 checkpointer 重启会丢失暂停态；上线前必须切换持久化后端

**遗留扩展**: Phase 3 时把 HITL 条件边扩展到 supervisor / cashier（按 fund_transfer.md 的内部审批矩阵）。

---

### ADR-011: Tool 层装饰顺序 — @tool 外、@audit 内

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: LangChain 的 `@tool` 装饰器把函数包装为 `Tool` 对象（带 `.invoke()`、`.name`、`.description`、`.args_schema`），而 CLAUDE.md §4.1 要求资金类 Tool 必须带审计日志装饰器。两者叠加时顺序敏感。

**决策**: 强制装饰器顺序：
```python
@tool         # 外层（应用顺序最后）
@audit()      # 内层（应用顺序最先，离原函数最近）
def my_tool(...): ...
```

**理由**:
1. `@audit()` 用 `functools.wraps` 保留原函数的 `__doc__` / `__name__` / `__wrapped__`，`@tool` 通过 `inspect.signature` + `__wrapped__` 仍能拿到正确签名生成 schema
2. 调用顺序：`Tool.invoke(...)` → audit_wrapper → 原函数；审计日志在每次 Tool 调用前后都执行
3. 反向顺序（`@audit` 外、`@tool` 内）会把 Tool 对象传给 audit，破坏审计语义

**代价**: 项目内所有业务 Tool 必须遵守此顺序；新 Tool 上线前在 code review 检查。

---

### ADR-012: Tool 模块级 store 缓存（非 lru_cache）

**日期**: 2026-05-16
**状态**: 已采纳

**背景**: `search_industry_knowledge` / `search_enterprise_knowledge` 需要 QdrantVectorStore 实例。每次 Tool 调用重建 store 浪费 embedding 加载时间，所以要缓存。但 `lru_cache` 不利于测试注入 fake store。

**决策**: 使用模块级 `_stores: dict[str, object] = {}` 作为缓存，`reset_stores()` 公开供测试清空。测试通过 `knowledge_module._stores["industry"] = fake_store` 直接注入。

**理由**:
1. `dict.__setitem__` 可由测试 monkeypatch 或直接赋值，比覆盖 lru_cache 函数更直观
2. 单例语义不变（首次调用建实例，后续复用）
3. 与 `app/rag/store.py` 的 `lru_cache` 单例（QdrantClient）形成互补：底层资源用 lru_cache，业务层 store 用可注入 dict

**代价**: 比 lru_cache 多 5 行代码；测试代码要记得 `reset_stores()` 收尾。

---

## 迭代记录

### Iteration 2026-05-15: 文档基线初始化

**改动范围**: README、DESIGN、CLAUDE、HISTORY 四份文档
**触发原因**: 项目启动，先统一业务方向、架构边界和安全红线

**本次新增**:
- `CLAUDE.md`: 项目级 Claude Code 上下文
- `DESIGN.md`: 架构设计文档
- `HISTORY.md`: 本文件
- `README.md`: 对外说明文档与目标结构

**遇到的问题**:
- 初始文档一度把目标代码结构写成已落地状态，容易误导后续开发。
- 已在 2026-05-16 修正为 Phase 0 文档基线，明确当前没有可运行代码。

**下次迭代目标**:
1. 创建最小代码骨架：`requirements.txt`、`.env.example`、`main.py`、`app/`、`tests/`
2. 实现最小 RAG 查询链路，先使用本地示例文档和 Mock 数据
3. 验证 Agent 能正确区分行业知识库与企业知识库查询

---

### Iteration 2026-05-16: 文档状态校准

**改动范围**: README、CLAUDE、HISTORY、DESIGN
**触发原因**: 明确当前目录只有文档，避免后续开发误以为代码已存在
**新增/修改**:
- 将当前阶段修正为 Phase 0：方向澄清与文档基线
- 在 README 中加入当前状态说明，标注快速开始和项目结构均为目标状态
- 在 CLAUDE.md 中标注目标目录和常用命令尚不可执行
- 在 HISTORY.md 中修正旧迭代标题，避免把目标结构误写成已实现代码
- 在 DESIGN.md 中补充阶段边界，明确 Phase 1 前必须先完成最小代码骨架
**遗留问题**:
- 代码骨架尚未创建
- 依赖版本尚未锁定
- 知识库样例、测试策略和验收用例仍待实现阶段补齐

---

### Iteration 2026-05-16: Phase 0 → Phase 1 跨越

**改动范围**: 全栈代码骨架 + 4 份文档修订
**触发原因**: Phase 0 文档基线已稳定，进入 §7.0 准入条件中的最小可运行代码骨架阶段

**新增代码（按依赖顺序）**:
1. **配置基线**：`requirements.txt`、`.env.example`、`.gitignore`、`pyproject.toml`、`app/config.py`、`app/__init__.py`
2. **测试基础设施**：`tests/{conftest.py, test_config.py}`（18 测试）— 含 `tmp_dir` fixture 绕 Windows tmp_path 坑
3. **最小 RAG**：`app/rag/{store.py, knowledge_base.py, __init__.py}` + 4 个占位 KB 文档 + `tests/test_rag.py`（9 测试）
4. **Agent 骨架**：`app/agents/{nodes.py, __init__.py}`、`app/graph/{state.py, routing.py, __init__.py}` + `tests/test_graph.py`（27 测试）
5. **CLI**：`main.py` + `tests/test_main.py`（11 测试）

**文档修订**:
- `DESIGN.md` §4.2：修复 `route_by_intent` 多处 bug；新增 §4.2.0 LangGraph/LangChain 职责分工章节；§4.2.2 状态图重画为多阶段 + HITL 横切；§5.1 调拨流程标注子任务编排语义
- `CLAUDE.md` §8：Sub-agents 优先级与 DESIGN.md 对齐
- 详见 ADR-006 / ADR-007 / ADR-008 / ADR-009

**测试规模**: 65 测试 / 0.82s 全绿（pytest 9.0.3 + Python 3.12.9 + Windows 11）

**遇到的问题**:
1. **Windows pytest tmp_path 失效**：`%TEMP%\pytest-of-46673` 目录权限异常，所有用 `tmp_path` 的测试集体 PermissionError。绕坑：`tmp_dir` fixture（`tempfile.mkdtemp` 自管 + finally 清理）。
2. **HuggingFaceEmbeddings 拖 torch**：1.5GB 安装包不适合测试环境；通过延迟导入 + 可注入 Embeddings 解耦（见 ADR-009）。
3. **CLI 终端中文乱码**：Windows cmd 默认 GBK 解码 UTF-8 字节流出现乱码，环境层问题，`chcp 65001` 可修复，代码正确。

**遗留问题**:
- HITL 节点未实现（Phase 2）
- 真实 LLM 未接入；Agent 节点仅返回标识字符串（Phase 2）
- `app/tools/` 业务工具全部为空（Phase 2）
- `scripts/update_enterprise_kb.py` 未实现（Phase 2）
- `langchain` 主包未实际安装（测试只用了 `langchain-core` + `-text-splitters` + `-qdrant` + `langgraph`）；首次接 LLM 时需要 `pip install -r requirements.txt` 完整安装

**下次迭代目标**（Phase 1 → Phase 2 起点）:
1. HITL 节点：横切节点 + `requires_approval` 触发逻辑 + 测试
2. 接入真实 LLM：先用 Qwen-Max 或 DeepSeek（与 CLAUDE.md DeepSeek 启动脚本复用 API Key 路径）
3. Supervisor 接 LLM 做意图分类，替换"调用方提供 current_task"的占位约定
4. 至少接入一个真实 Tool：建议先 `search_industry_knowledge` + `search_enterprise_knowledge`（双轨检索包装为 `@tool`）

---

### Iteration 2026-05-16: Phase 2 早期 — HITL 横切节点接入

**改动范围**: `app/agents/nodes.py`、`app/graph/__init__.py`、`main.py`、测试基础设施
**触发原因**: HISTORY.md 上轮迭代标记的 Phase 2 起点 #1；完成 DESIGN.md §4.2.2 状态图设计但未落地的横切节点

**新增/修改**:
- `app/agents/nodes.py`：新增 `hitl_node`（用 `langgraph.types.interrupt`）；`treasury_manager_node` 对 fx/aml/investment 与大额 transfer 置 `requires_approval=True`
- `app/graph/__init__.py`：`MemorySaver` checkpointer + `_maybe_hitl` 条件边（仅挂在 `treasury_manager` 之后）
- `main.py`：UUID thread_id + 中断检测 + 退出码 4（含 payload 与 thread_id 打印）
- `tests/conftest.py`：`thread_config` fixture
- `tests/test_hitl.py`：11 个测试（触发判定 7 + resume 4）
- `tests/test_graph.py`：把 fx / 大额 transfer 测试搬到 test_hitl，TestGraphExecution 只保留非 HITL 路径
- `tests/test_main.py`：`test_large_transfer_routes_to_manager` 改名 `test_large_transfer_triggers_hitl`，期望退出码 4

**测试规模**: 65 → 74（+9 net）

**遇到的问题**:
- 启用 checkpointer 后，所有 `invoke` 都需要 `config={"configurable": {"thread_id": ...}}`；改前现有 15 个测试集体 ValueError。统一加 `thread_config` fixture 解决。
- 大额 transfer / fx 之前的两个 graph execution 测试在中断处停止，断言失败；分流到 test_hitl.py 后语义更清晰。

**遗留**: HITL 仅挂在 manager 之后；按 fund_transfer.md，supervisor 处理 50-500 万也应有审批，留待 Phase 3。

---

### Iteration 2026-05-16: Phase 2 代码极限 — Tool 层 + 企业库热更新

**改动范围**: 新增 `app/tools/`、`scripts/`；扩展测试覆盖
**触发原因**: 用户目标"完成这项目"框定为"Phase 2 代码极限不接 LLM"；落地所有不依赖 API Key 的工程件

**新增**:
- `app/tools/audit.py`：`@audit(tool_name=None)` 装饰器；JSONL 日志写到 `settings.audit_log_path`；记录 ts/tool/args/kwargs/duration_ms/error/result_preview
- `app/tools/masking.py`：`MaskingMap` 线程安全双向映射 + `get_masking_map()` 单例；按分类（ACC/CUST/CP/ENTITY）独立计数；提供框架，真实业务数据导入留待 Phase 3
- `app/tools/knowledge.py`：`search_industry_knowledge` / `search_enterprise_knowledge` 包装为 LangChain `@tool`，叠加 `@audit()`；模块级 `_stores` dict 缓存（不是 lru_cache，便于测试注入）+ `reset_stores()` 测试钩子
- `app/tools/__init__.py`：再导出公共 API
- `scripts/update_enterprise_kb.py`：企业库热更新 CLI，支持 `--check`（dry-run）和默认（全量重建）；可通过 `python -m scripts.update_enterprise_kb` 调用
- 4 个测试文件：`test_tools_audit.py`（8 测试）、`test_tools_masking.py`（9 测试）、`test_tools_knowledge.py`（12 测试）、`test_scripts.py`（3 测试）

**测试规模**: 74 → 106（+32 net）总 1.04s 全绿

**关键设计决策**: ADR-010 (HITL)、ADR-011 (Tool 装饰顺序)、ADR-012 (Tool store 缓存模式)

**遇到的问题**: 无（一次绿）

**遗留**:
- Tool 已可独立调用，但 Agent 节点仍是占位实现，没有 Agent 真实选择并调用这些 Tool（需要 LLM 做意图分类与 Tool 选择）
- 脱敏框架是空表；真实业务数据接入需等 Phase 3 主数据/ERP 对接

---

### Iteration 2026-05-16: Phase 2 完整版 — LLM 集成 + FastAPI 服务

**改动范围**: 新增 `app/llm.py`、`app/api.py`、`app/memory/`；改造 `app/agents/nodes.py`、`app/graph/__init__.py`；扩展 conftest + 新增 3 个测试文件
**触发原因**: 用户目标 "完成这项目" 在 Stop Hook 触发后被解释为完整 Phase 2，不止"代码极限不接 LLM"；继续推进剩余必做项

**新增**:
- `app/llm.py`：`get_chat_model()` lru_cache 单例（OpenAI 协议兼容层，DeepSeek / Qwen / OpenAI 都用 ChatOpenAI 实例化，仅 base_url 不同）；`reset_chat_model_cache()` 测试钩子
- `app/api.py`：FastAPI 主入口，按 DESIGN.md §8.2 实现 5 端点：
  - `POST /api/v1/chat`（含 status: completed/interrupted/rejected 三态）
  - `POST /api/v1/approvals/{thread_id}`（HITL 恢复）
  - `GET /api/v1/knowledge`（双轨直查，绕 Agent）
  - `GET /api/v1/audit/logs`（含 tool 名筛选）
  - `GET /healthz`
  - **graph 单例**：模块级 `_graph_singleton`，避免每请求新建 MemorySaver 导致 HITL thread 丢失
- `app/memory/__init__.py`：Phase 3 持久化 checkpointer 接入点说明（暂无代码）
- `tests/test_llm.py`：6 个测试（工厂 + 缓存 + monkeypatch 注入）
- `tests/test_agents.py`：12 个测试（supervisor 意图分类 6 + knowledge agent 3 + 端到端 LLM 集成 2 + 边界 1）
- `tests/test_api.py`：14 个测试（healthz / chat 6 / approvals 2 / knowledge 3 / audit 3）
- `tests/conftest.py`：抽取 `_FakeEmbeddings` 类 + `fake_embeddings` / `populated_stores` / `fake_llm` 三个新 fixture，供所有测试复用

**改造**:
- `app/agents/nodes.py`：
  - `supervisor_node`：当 `current_task` 未由调用方提供时，调 LLM 把最后一条 HumanMessage 分类到 Intent；LLM 响应非法时 fallback 到 `knowledge`
  - `knowledge_node`：调用双轨 `search_*_knowledge` 拿上下文，把"行业法规 / 企业制度 / 用户问题"塞 prompt 让 LLM 合成带 [来源:] 标注的答案，同时把上下文写入 state.industry_context / state.enterprise_context 便于审计回溯
  - 顶层 `import app.llm as llm_module` 让测试 monkeypatch 在节点内 `llm_module.get_chat_model()` 调用点生效
- `app/graph/__init__.py`：agents 导入推到 `build_graph()` 函数内，破解循环导入（nodes 依赖 `state` 触发 graph 包加载 → 反过来加载 agents → 循环）
- `tests/test_main.py`：`test_knowledge_task` 加上 fake_llm + populated_stores fixtures（否则会撞真实 HF embedding 下载）
- `requirements.txt`：取消 fastapi / uvicorn / httpx 的 Phase 2 注释，正式纳入

**测试规模**: 106 → 136（+30 net）总 2.50s 全绿

**遇到的问题**:
1. **循环导入**：`app.agents.nodes` 导入 `app.graph.state` 触发 `app.graph.__init__` 顶层 import `app.agents` —— 后者仍在初始化中。解法：把 agents 导入推到 `build_graph()` 函数内。
2. **monkeypatch + lru_cache 冲突**：`reset_chat_model_cache()` 在 fixture 拆卸时被调用，但此时 `get_chat_model` 已被 monkeypatch 替换成 lambda，没有 `.cache_clear()` 属性。解法：函数内用 `getattr(..., "cache_clear", None)` 防御性检查。
3. **API graph singleton**：早期实现每次请求都 `build_graph()`，新 MemorySaver 实例找不到上次暂停的 HITL thread。解法：模块级 `_graph_singleton` 单例。
4. **Tool 链跑通需要 langchain-huggingface 或注入**：knowledge_node 调真实 search_industry_knowledge，触发 `get_embeddings()` 强制 `import langchain_huggingface`。生产装；测试用 `populated_stores` fixture 直接注入 `_stores` 缓存绕开。

**关键设计决策**: ADR-013（待补：LLM 单例 + monkeypatch 友好模式）、ADR-014（待补：API graph 单例必要性）。本轮先以代码 + 测试形式落地，后续会话补 ADR。

**Phase 2 完整版达成度**:
- ✅ HITL 横切节点（已通过 `Command(resume=...)` 端到端验证）
- ✅ 双轨知识库（fake embeddings 测试 + 真实 HF embedding 生产路径，stub 文档 4 份）
- ✅ Tool 层（audit + masking + knowledge @tool）
- ✅ LLM 接入（OpenAI/DeepSeek/Qwen 统一抽象，无需 API Key 也能跑测）
- ✅ Supervisor LLM 意图分类（fallback 到 knowledge）
- ✅ Knowledge Agent ↔ Tool 真实集成（双轨检索 + LLM 合成 + 来源标注）
- ✅ FastAPI 5 端点（chat / approvals / knowledge / audit / healthz）
- ✅ 136 测试全绿

**Phase 3 起点候选**（任选）:
- Dockerfile + docker-compose + 持久化 checkpointer（PostgresSaver）
- MCP Server 封装（bank / fx / aml / erp / report 五件套）
- 银企直连真实接口适配（沙箱 → 生产网关）
- 鉴权（FastAPI Depends + JWT / 企业 SSO）
- 审计日志查询的 admin-only 鉴权 + Grafana 看板
- 真实业务数据接入脱敏映射

---

## 配置审查记录

### 审查 2026-05-15: 初始配置审查

**审查人**: Claude Code（初始化）
**模型版本**: GPT-4o / Qwen-Max

**检查结果**:
| 检查项 | 状态 | 备注 |
|--------|------|------|
| CLAUDE.md 是否精简 | ✅ 通过 | 约 7.5KB，符合"不塞太多"原则 |
| 是否有冗余 Prompt 规则 | ✅ 通过 | 无重复或矛盾规则 |
| Tool docstring 是否完整 | ⏳ 待实现 | 当前没有 Tool 代码 |
| 安全红线是否可执行 | ⏳ 待实现 | 当前只有规则，尚无 HITL 或权限校验代码 |
| 子目录 CLAUDE.md 是否需要 | ⏳ 待评估 | `app/tools/` 和 `app/agents/` 创建后再评估 |

**下次审查时间**: 2026-08-15

---

## 经验教训

> 每次踩坑后记录，避免重复犯错。

1. **[预留]** LangGraph 的 `interrupt` 节点在 Windows 上的行为是否与 Linux 一致？需验证。
2. **[预留]** Qdrant 本地文件模式在并发写入时的稳定性？目前单用户 CLI 无问题，API 模式需压测。
3. **[预留]** BGE-large-zh 在法规条文上的检索召回率？需要人工标注 50 条查询做评测。
4. **2026-05-16**: Windows 上 pytest 的 `tmp_path` 依赖 `%TEMP%\pytest-of-<user>` 目录的可写权限；本机该目录被锁导致所有用 `tmp_path` 的测试 PermissionError。**教训**：不要假设 pytest 内置 fixture 在所有平台可用，conftest 提供 `tmp_dir` 替代品。
5. **2026-05-16**: HuggingFace 系列模型间接拖 `torch + transformers + sentence-transformers`（合计 ~1.5GB），**绝不能在模块顶层 import**。RAG 模块用延迟导入 + 可注入 Embeddings 才能让测试在无 GPU/无 torch 的环境跑通。
6. **2026-05-16**: Qdrant 本地文件模式 `QdrantClient(path=...)` 同一进程内不能开多个 client，会锁定文件；必须 `lru_cache` 单例化。测试用 `QdrantClient(location=":memory:")` 隔离，且单一 client 内可挂多个 collection。
7. **2026-05-16**: pydantic `SecretStr` 在 `repr/str` 中自动掩码为 `**********`，但 `f"{settings}"` 也走 `str()` — 写日志时无需担心明文 API Key 泄露；但读取真实值必须显式 `.get_secret_value()`。
8. **2026-05-16**: LangGraph `add_messages` reducer 必须用 `Annotated[list[BaseMessage], add_messages]` 标注，否则节点返回 `{"messages": [...]}` 会覆盖而非追加。状态字段的 reducer 注解是 LangGraph 的核心约定。

---

## 外部依赖变更记录

| 日期 | 依赖 | 变更 | 影响 |
|------|------|------|------|
| 2026-05-15 | langchain | 目标候选：0.3.x | 尚无 `requirements.txt`，未实际锁定 |
| 2026-05-15 | qdrant-client | 目标候选：1.12.x | 尚无 `requirements.txt`，未实际锁定 |
| 2026-05-16 | langchain / -core / -community / -text-splitters | 锁定 `>=0.3,<0.4` | `requirements.txt` 已生效；测试仅装 -core/-text-splitters，主包延迟到接 LLM 时再装 |
| 2026-05-16 | langchain-openai | 锁定 `>=0.2,<0.4` | 兼容 OpenAI / DeepSeek / Qwen 兼容模式，单一包覆盖三家 |
| 2026-05-16 | langgraph | 锁定 `>=0.2,<0.5` | StateGraph + add_messages reducer 已使用 |
| 2026-05-16 | qdrant-client | 锁定 `>=1.7,<2.0` | 本地文件模式 + `:memory:` 测试模式均验证 |
| 2026-05-16 | langchain-qdrant | 锁定 `>=0.1,<0.3` | VectorStore 抽象层 |
| 2026-05-16 | langchain-huggingface | 锁定 `>=0.1,<0.3` | **延迟导入**，未实际安装；接 LLM 时再装 |
| 2026-05-16 | sentence-transformers | 锁定 `>=2.5,<4.0` | 同上，延迟到首次 HF embedding 调用 |
| 2026-05-16 | pydantic / pydantic-settings | 锁定 `>=2.0,<3.0` | 配置基线已落地 |
| 2026-05-16 | pytest / ruff | 锁定 `>=8.0,<9.0` / `>=0.6,<1.0` | 测试与 lint 工具 |
| 2026-05-16 | fastapi / uvicorn | 暂未引入（Phase 2） | requirements.txt 已标注待引入位置 |

---

*记录格式参考: Anthropic "Configuration Iteration" 最佳实践*
*每 3 个月或每次大模型发布后，做一次完整审查并在此追加记录*
