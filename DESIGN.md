# DESIGN.md — 企业资金智能体架构设计

> **项目架构蓝图**
>
> 本文档描述 Treasury Agent 的整体架构、核心模块设计、数据流和扩展规划。
> 对应 Anthropic 推荐的七层 harness 扩展体系，明确每层在项目中的落地方式。
> 开发前必读。具体编码约定见 CLAUDE.md，决策历史见 HISTORY.md。

---

## 1. 设计目标与非目标

### 1.0 当前阶段边界

当前目录处于 **Phase 0：方向澄清与文档基线**。本文件描述目标架构和实现约束，不表示代码已经存在。

在进入 Phase 1 之前，必须先完成最小代码骨架：

- `requirements.txt` 与 `.env.example`
- `main.py` 或等价 CLI 入口
- `app/config.py`
- `app/rag/` 最小检索模块
- `knowledge_base/industry/` 与 `knowledge_base/enterprise/` 示例文档目录
- `tests/` 中至少覆盖配置、权限矩阵和最小检索链路

未完成上述骨架前，不应推进银企直连、付款执行、复杂审批流、MCP Server、Hooks、Skills 或插件化建设。

### 1.1 目标
- **专业准确**: 资金领域的法规引用、金额阈值、审批流程必须 100% 准确，可溯源
- **角色隔离**: 出纳、主管、经理三岗权限物理隔离，符合企业内控 SOD 原则
- **合规可追溯**: 所有决策、工具调用、LLM 输出留痕，支持审计
- **渐进式采用**: 从知识问答（Phase 1）到流程自动化（Phase 3），平滑升级

### 1.2 非目标（明确排除）
- **不替代核心银行系统**: Agent 是决策辅助层，不直接替代银企直连接口或 ERP
- **不做自动付款**: 任何资金划出操作必须经人工确认（HITL），Agent 只生成指令和预审
- **不做投资建议**: 外汇套保、理财建议仅提供方案试算，最终决策权在人类

---

## 2. 系统架构

### 2.1 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│  交互层 (Presentation)                                        │
│  CLI / FastAPI / 企业微信 / 钉钉 / Web                         │
└───────────────────────┬──────────────────────────────────────┘
                        │ HTTP / WebSocket
┌───────────────────────▼──────────────────────────────────────┐
│  编排层 (Orchestration)                                       │
│  Supervisor Agent —— 意图识别 · 权限校验 · 流程编排 · HITL      │
└──┬──────────────┬──────────────┬──────────────────┬──────────┘
   │              │              │                  │
┌──▼──┐    ┌────▼────┐   ┌─────▼──────┐   ┌──────▼──────┐
│出纳  │    │ 资金主管 │   │  资金经理   │   │  知识库检索  │
│Agent │    │  Agent  │   │   Agent    │   │   Agent     │
└──┬──┘    └────┬────┘   └─────┬──────┘   └──────┬──────┘
   │            │              │                  │
   └────────────┴──────────────┴──────────────────┘
                        │
         ┌──────────────▼──────────────┐
         │        工具层 (Tools)        │
         │ 银企直连 │ 汇率API │ 风控引擎 │
         │  ERP   │ AML名单 │ 估值模型 │
         └─────────────────────────────┘
                        │
         ┌──────────────▼──────────────┐
         │      知识层 (Knowledge)      │
         │ Industry KB │ Enterprise KB  │
         └─────────────────────────────┘
                        │
         ┌──────────────▼──────────────┐
         │    基础设施 (Infrastructure)  │
         │ Qdrant │ PostgreSQL │ Redis  │
         └─────────────────────────────┘
```

### 2.2 核心设计哲学

**"决策辅助，而非决策替代"**

Agent 系统的定位是"增强型 Treasury 工作台"：
- 把法规查询从 30 分钟人工检索压缩到 10 秒
- 把资金计划编制从 Excel 手工汇总升级为智能预测
- 把 AML 审查从抽样检查升级为全量筛查 + 可疑模式识别

但**最终盖章、最终付款、最终投资**的按钮，必须握在人类手里。

---

## 3. 七层 Harness 扩展规划

对应 Anthropic 提出的 Claude Code 七层扩展体系，以下是在 Treasury Agent 项目中的落地映射：

### Layer 1: CLAUDE.md ✅ 已落地
**用途**: 项目全局上下文，每次 Claude 会话自动加载  
**位置**: `CLAUDE.md`（根目录）+ 子目录局部 `CLAUDE.md`  
**内容**: 技术栈、目录地图、开发铁律、安全红线、常用命令  
**维护频率**: 每 3 个月审查一次，或模型升级后检查

### Layer 2: Hooks 🔄 预留架构
**用途**: 会话生命周期自动化、自我进化  
**规划位置**: `.claude/hooks/`  
**候选 Hooks**:
| Hook 类型 | 触发时机 | 功能 |
|-----------|---------|------|
| `start` | Claude Code 启动时 | 根据当前目录自动加载对应岗位上下文（如在 `app/agents/cashier/` 下自动注入出纳权限矩阵） |
| `pre-tool` | Tool 调用前 | 拦截高风险 Tool（如 execute_transfer），检查 HITL 状态 |
| `post-write` | 文件写入后 | 自动执行 `pytest` 和 `ruff check`，失败则阻止提交 |
| `stop` | 会话结束时 | 反思本次改动，提议更新 CLAUDE.md 或追加 HISTORY.md |

**实施优先级**: P2（Phase 2 引入，先手动跑通流程）

### Layer 3: Skills 🔄 预留架构
**用途**: 按需加载的专业知识包，避免每个会话塞满所有知识  
**规划位置**: `skills/` 目录  
**设计原则**:
- 按岗位拆分：`cashier_skill/`、`fx_skill/`、`aml_skill/`
- 按任务拆分：`planning_skill/`、`reporting_skill/`、`compliance_review_skill/`
- 可绑定到目录：在 `app/agents/cashier/` 下自动加载 cashier_skill

**Skill 内容示例** (`skills/aml_skill/SKILL.md`):
```markdown
# AML 审查 Skill

## 检查清单
1. 交易对手是否在 OFAC/联合国/中国红通名单？
2. 交易金额是否超过大额报告标准（个人 5 万美金/日，企业 20 万人民币/日）？
3. 交易模式是否符合可疑交易特征（如：分散转入集中转出、快进快出）？
4. 资金用途是否与经营范围一致？

## 输出格式
- 风险等级：低/中/高/极高
- 命中名单：[是/否]，详情...
- 建议措施：放行/加强审核/拒绝交易/上报可疑交易报告（STR）
```

**实施优先级**: P2（Phase 2 按岗位逐步引入）

### Layer 4: Plugins ⏳ 远期规划
**用途**: 打包 Skills + Hooks + MCP 配置，团队内部分发，避免部落知识  
**场景**: 新员工第一天安装 Plugin，即拥有和老手相同的 Claude Code 环境  
**实施优先级**: P3（Phase 3 团队推广时建设）

### Layer 5: LSP ✅ 已具备
**用途**: 代码符号级导航，让 Claude 精确找到函数定义和引用  
**状态**: Python 项目天然支持 Pyright/Pylance  
**增强建议**: 对 `app/tools/` 中的 Tool 函数，确保类型注解完整，LSP 能精准跳转  
**实施优先级**: P0（保持现状，无需额外工作）

### Layer 6: MCP Servers 🔄 预留架构
**用途**: 把外部系统封装为 Claude 可直接调用的标准化工具  
**规划封装**:
| MCP Server | 连接系统 | 暴露能力 |
|-----------|---------|---------|
| `mcp-bank-host` | 银企直连/网银 | 余额查询、转账、回单获取 |
| `mcp-fx-market` | 路透/万得/Bloomberg | 实时汇率、远期点、波动率 |
| `mcp-aml-screen` | 内部风控/外部名单库 | OFAC、联合国、PEP 名单筛查 |
| `mcp-erp` | SAP/Oracle/用友 | 科目余额、凭证、资金计划 |
| `mcp-report` | 内部 BI/报表系统 | 现金流报表、融资台账、外汇敞口表 |

**架构原则**: 
- MCP Server 是**只读优先**，写操作（转账、审批）必须通过 HITL
- 每个 MCP Server 独立进程，LangChain Tool 做薄封装层
- 不急于 Phase 1 建设，先完成最小代码骨架和 Mock Tool 边界

**实施优先级**: P2（Phase 2 逐步接入，先 Mock 后真实）

### Layer 7: Sub-agents ⏳ 架构预留
**用途**: 把探索和编辑分离，避免单个会话 context 撑爆  
**当前使用**:
- 架构上已经定义各角色 Agent（出纳、主管、经理）由 Supervisor 调度
- 代码尚未实现 `app/agents/`，因此当前没有可运行的子 Agent
- 未来扩展：复杂任务（如"做一份季度外汇套保方案"）可派生子 Agent 先调研，再汇总

**实施优先级**: P0（Phase 1 代码骨架中优先落地）

---

## 4. 核心模块详细设计

### 4.1 双轨知识库 (Dual-Track KB)

#### 4.1.1 数据模型

```
Collection: treasury_industry
├── Documents (chunked)
│   ├── metadata.source: "knowledge_base/industry/regulations/外汇管理条例.txt"
│   ├── metadata.category: "industry"
│   ├── metadata.subcategory: "regulations"
│   └── metadata.effective_date: "2008-08-01" (法规生效日期)
│
Collection: treasury_enterprise
├── Documents (chunked)
│   ├── metadata.source: "knowledge_base/enterprise/policies/资金调拨管理办法.txt"
│   ├── metadata.category: "enterprise"
│   ├── metadata.subcategory: "policies"
│   └── metadata.version: "v2.1" (制度版本号)
```

#### 4.1.2 检索策略

**默认策略**: `both`（双库并行检索，合并排序）
- 优点：跨库关联查询（如"法规要求 + 我们公司怎么做的"）
- 缺点：延迟 ×2，Token 消耗 ×2

**优化策略**（Phase 2）:
- Supervisor Agent 先做一次"意图分类"，判断问题纯法规/纯内部/混合
- 纯法规 → 只查 industry
- 纯内部 → 只查 enterprise
- 混合 → 双库并行

#### 4.1.3 更新机制

| 知识库 | 触发条件 | 执行脚本 | 影响范围 |
|--------|---------|---------|---------|
| Industry | 季度更新（或法规发布后） | 手动执行 `init_industry_kb(force_rebuild=True)` | 全局，需重启服务确认 |
| Enterprise | 制度修订后立即 | `scripts/update_enterprise_kb.py` | 局部，热更新，零停机 |

---

### 4.2 Multi-Agent 编排 (LangGraph + LangChain)

#### 4.2.0 框架职责分工

本项目同时依赖 **LangGraph** 和 **LangChain**，两者承担不同职责，写代码前必须分清。

| 维度 | LangGraph | LangChain |
|------|-----------|-----------|
| **定位** | 流程"骨架" | 通用"零件" |
| **核心抽象** | `StateGraph`、`Node`、`Edge`、`interrupt` | `@tool`、`BaseMessage`、`ChatModel`、`Embeddings`、`VectorStore` |
| **在本项目负责** | Supervisor 编排、条件路由、HITL 暂停/恢复、状态持久化 | Tool 定义与 schema 生成、LLM 客户端、消息类型、RAG 检索组件 |
| **代码位置** | `app/graph/` | `app/tools/`、`app/rag/`、`app/agents/`（LLM 客户端） |

**具体落地约定**：

1. **Tool 用 LangChain 的 `@tool` 装饰器**，靠 Python 类型注解自动生成 JSON schema 给 LLM；不要手写 schema，也不要绕过 `@tool` 直接给 LangGraph 节点传裸函数
2. **状态机用 LangGraph 的 `StateGraph`**；节点之间只通过 `TreasuryState` 传消息，不要让节点直接 import 其他节点
3. **HITL 走 LangGraph 的 `interrupt` 节点**，不要在 LangChain Tool 内部阻塞等待用户输入
4. **LLM 客户端走 LangChain 抽象**（`ChatOpenAI` / `ChatTongyi` / `ChatDeepSeek` 等），便于 §7.3 提到的多模型热切换；不要在节点里直接调用 OpenAI SDK
5. **RAG 组件走 LangChain 抽象**（`QdrantVectorStore` + `HuggingFaceEmbeddings`），不要在业务层直连 Qdrant 客户端
6. **MCP Tool 封装** = LangChain `@tool` 薄壳 + 内部转发到 MCP Server（见 §3 Layer 6）

> **不要混用**：不要试图用 LangGraph 取代 LangChain Tool 抽象，也不要用 LangChain 的 `AgentExecutor` 取代 LangGraph（已在 HISTORY.md ADR-001 否决）。

#### 4.2.1 状态定义

```python
class TreasuryState(TypedDict):
    # 对话与任务
    messages: List[BaseMessage]          # 完整对话历史
    current_task: str                    # 当前任务类型：inquiry / transfer / plan / fx / aml
    
    # 路由与权限
    current_role: Literal["supervisor", "cashier", "treasury_supervisor", "treasury_manager", "knowledge"]
                                         # 当前处理角色，命名必须与 user_role 及 route_by_intent 返回值一致
    user_role: Literal["cashier", "treasury_supervisor", "treasury_manager", "admin"]
                                         # 操作者身份
    requires_approval: bool              # 是否触发审批流（任何 Agent 都可置位，由 HITL 节点统一拦截）
    approval_status: Literal["none", "pending", "approved", "rejected"]
    approved_instruction_id: Optional[str]  # 已审批指令编号，出纳执行类 Tool 前必须校验此字段
    
    # 业务数据
    entity_code: Optional[str]           # 法人主体代码
    amount: Optional[Decimal]            # 涉及金额
    currency: Optional[str]              # 币种
    counterparty: Optional[str]          # 交易对手（脱敏后）
    
    # 知识检索
    industry_context: str                # 行业法规检索结果
    enterprise_context: str              # 企业制度检索结果
    
    # 执行与输出
    tool_calls: List[dict]               # 已执行的工具调用记录
    execution_result: dict               # 工具执行结果
    final_output: str                    # 最终输出给用户的内容
```

#### 4.2.2 状态图 (StateGraph)

```
[用户输入]
    │
    ▼
┌─────────────┐
│  Supervisor │ ← 意图识别 + 权限校验（唯一调度中心）
│   (Entry)   │
└──────┬──────┘
       │
       ├─→ 纯知识问答 ──────→ Knowledge Agent ─────────┐
       │                  （Supervisor 直调双轨检索）   │
       │                                              │
       ├─→ 日常查询/对账 ───→ Cashier Agent ───────────┤
       │                                              │
       ├─→ 头寸/计划/账户 ──→ Treasury Supervisor ─────┤
       │                                              │
       ├─→ 外汇/投资/AML ───→ Treasury Manager ────────┤
       │                                              │
       └─→ 资金调拨 transfer（多阶段编排）              │
            │                                         │
            ├─ 小额 → Treasury Supervisor              │
            │     └─ 子任务：头寸检查 → 合规检索        │
            │                                         │
            ├─ 大额(>阈值) → Treasury Manager          │
            │     └─ 子任务：合规审查 → AML 筛查        │
            │                                         │
            └─ 已审批指令 → Cashier 执行               │
                                                      │
                            ┌─────────────────────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │ requires_     │ ← 任何 Agent 都可置位
                    │ approval ?    │
                    └───┬───────┬───┘
                       是│      否│
                        ▼        ▼
              ┌──────────────┐  [End]
              │ HITL Node    │  返回结果
              │ 等待人工确认  │
              └──────┬───────┘
                     │ approved
                     ▼
                  [End] 返回结果
```

> **设计要点**：
> - HITL 节点是**横切节点**，不绑定特定 Agent；任何 Agent 通过 `state.requires_approval = True` 即可触发
> - Knowledge Agent 不是独立 Agent 进程，而是 Supervisor 内联调用双轨检索 Tool 的逻辑分支
> - Transfer 是**多阶段编排**而非单一路由分支，由 Supervisor 拆分子任务依次调度

#### 4.2.3 条件边 (Conditional Edges)

```python
def route_by_intent(state: TreasuryState) -> str:
    """Supervisor 根据意图和权限决定路由。
    返回值必须是 TreasuryState.current_role 中定义的枚举值之一，
    或控制流标记 "reject_unauthorized"。
    """
    intent = state["current_task"]
    user = state["user_role"]
    amount = state.get("amount", Decimal("0"))

    # 权限矩阵校验：出纳无权发起调拨，只能执行已审批指令
    if intent == "transfer" and user == "cashier":
        if not state.get("approved_instruction_id"):
            return "reject_unauthorized"
        return "cashier"  # 已审批 → 出纳执行

    # 调拨路由：大额走经理审批，小额由主管编排子任务（头寸→合规→执行）
    if intent == "transfer":
        if amount > Decimal("5000000"):
            state["requires_approval"] = True
            return "treasury_manager"
        return "treasury_supervisor"

    if intent in ["fx", "aml", "investment"]:
        return "treasury_manager"

    if intent in ["planning", "position", "account"]:
        return "treasury_supervisor"

    if intent in ["inquiry", "reconciliation"]:
        return "cashier"

    return "knowledge"  # 默认走知识库问答
```

---

### 4.3 Tool 层设计

#### 4.3.1 Tool 分类与权限绑定

| Tool 类别 | 代表 Tool | 可用 Agent | 风险等级 |
|-----------|----------|-----------|---------|
| **查询类** | `get_balance`, `get_fx_rate`, `query_treasury_knowledge` | 全部 | 🟢 低 |
| **计算类** | `calculate_fx_exposure`, `forecast_cash_flow` | 主管、经理 | 🟢 低 |
| **审查类** | `aml_screening`, `compliance_check` | 经理 | 🟡 中 |
| **计划类** | `create_fund_plan`, `update_pooling_rule` | 主管 | 🟡 中 |
| **执行类** | `execute_transfer`, `confirm_hedging_trade` | 出纳（仅执行已审批） | 🔴 高 |

#### 4.3.2 Tool 调用拦截链（安全设计）

```
用户请求
    │
    ▼
Agent 决定调用 Tool
    │
    ▼
┌─────────────────┐
│ Pre-tool Hook   │ ← 检查：1) 用户是否有权 2) 是否已审批 3) 金额是否超限
│ （权限拦截层）   │
└────────┬────────┘
         │
    ├─ 通过 ──→ 调用真实 Tool / Mock Tool
    │
    └─ 拒绝 ──→ 返回错误："您无权执行此操作，需 [角色] 审批"
         │
         ▼
    ├─ 触发 HITL ──→ 等待人工在 Web UI 点击"确认"
    │
    └─ 直接拒绝 ──→ 结束
```

---

## 5. 数据流设计

### 5.1 资金调拨流程（完整数据流）

> **说明**：Transfer 不是一次 `route_by_intent` 调用就结束的简单路由，而是 Supervisor 拆分为
> 多个子任务后，**依次**调度不同 Agent。下例中 `route_by_intent` 因 amount>500 万返回
> `treasury_manager` 仅决定**审批主责 Agent**，头寸/执行等子任务由 Supervisor 单独调度。

```
用户: "从总部调拨 1000 万到香港公司"
    │
    ▼
Supervisor Agent
├── 意图识别: transfer
├── 实体解析: entity=HQ, amount=10,000,000, currency=CNY, cross_border=true
├── 权限校验: user_role=treasury_supervisor → 可发起调拨请求
├── route_by_intent: amount>500万 → 主责 Agent = treasury_manager
│                    并置位 state.requires_approval = True
└── 拆分子任务并依次调度
    │
    ├── 子任务 A: 头寸检查 ──→ Treasury Supervisor Agent
    │   └── Tool: check_position(HQ)
    │       └── 返回: 可用头寸 15,000,000 CNY ✅
    │
    ├── 子任务 B: 合规审查 ──→ Treasury Manager Agent（主责）
    │   ├── Tool: search_industry_knowledge("跨境资金池 1000万 外债登记")
    │   │   └── 返回: 全口径跨境融资宏观审慎额度要求...
    │   ├── Tool: search_enterprise_knowledge("香港公司 调拨审批")
    │   │   └── 返回: 单笔 >500万 需资金经理 + CFO 双签
    │   └── Tool: aml_screening("香港公司")
    │       └── 返回: 无命中 ✅
    │
    ├── HITL Node: 等待人工双签确认
    │   └── 通过后写入 state.approved_instruction_id = "APR-2026-0515-001"
    │
    └── 子任务 C: 执行 ──→ Cashier Agent（凭 approved_instruction_id 放行）
        └── Tool: execute_transfer(...)
            └── 返回: 银行流水号 BK20260515001
    │
    ▼
Supervisor Agent 汇总结果，返回用户
```

### 5.2 知识问答流程（简单数据流）

```
用户: "反洗钱大额交易报告标准是多少？"
    │
    ▼
Supervisor Agent: 意图识别为 inquiry，无需审批
    │
    ▼
Knowledge Agent（或 Supervisor 直接调用）
├── Tool: search_industry_knowledge("反洗钱 大额交易报告 标准")
│   └── 返回: 《金融机构大额交易和可疑交易报告管理办法》...
└── Tool: search_enterprise_knowledge("反洗钱 大额交易 内部标准")
    └── 返回: 我司规定：单笔 >20万人民币 需额外留痕...
    │
    ▼
LLM 综合两段上下文，生成回答（标注法规来源 + 企业制度来源）
```

---

## 6. 安全与合规架构

### 6.1 纵深防御体系

```
Layer 1: 身份与权限 (RBAC)
├── 用户身份校验（对接企业 SSO/AD）
├── 角色绑定（出纳/主管/经理）
└── 权限矩阵实时校验（每次 Tool 调用前检查）

Layer 2: Agent 行为隔离
├── System Prompt 锁死角色边界
├── Tool 白名单（每个 Agent 只能看到自己有权的 Tool）
└── 跨角色操作必须通过 Supervisor 路由

Layer 3: 数据脱敏
├── 知识库入库前批量脱敏（正则匹配银行卡号、身份证号）
├── 运行时脱敏映射表（Agent 只操作代号）
└── 输出层再脱敏（防止 LLM 从上下文中反推）

Layer 4: 审批与 HITL
├── 金额阈值自动触发审批流
├── 跨境/外汇/AML 自动触发审批流
└── 人工确认节点不可跳过

Layer 5: 审计与追溯
├── 全量操作日志（Append-only）
├── LLM 输出快照（每次推理的 input/output）
└── 定期合规扫描（检查是否有越权操作日志）
```

### 6.2 数据分类与处理策略

| 数据级别 | 示例 | 存储 | LLM 可见性 | 知识库 |
|---------|------|------|-----------|--------|
| **公开** | 外汇管理条例原文 | 明文 | ✅ 全文可见 | industry |
| **内部** | 资金管理办法 | 明文 | ✅ 全文可见 | enterprise |
| **敏感** | 账户余额、头寸数据 | 加密 | ⚠️ 脱敏后可见（代号） | enterprise（脱敏后） |
| **机密** | 银行密钥、交易密码 | HSM/ Vault | ❌ 绝不可见 | ❌ 禁止入库 |

---

## 7. 扩展性与演进路线

### 7.0 Phase 0 → Phase 1 的准入条件

| 准入项 | 要求 |
|--------|------|
| 项目骨架 | `app/`、`tests/`、`knowledge_base/`、`scripts/` 基础目录存在 |
| 依赖锁定 | `requirements.txt` 明确 Python、LangChain、LangGraph、Qdrant 等版本 |
| 配置基线 | `.env.example`、`app/config.py` 包含模型、向量库、审批阈值、沙箱开关 |
| 最小测试 | 可运行 `pytest`，至少覆盖配置加载和权限矩阵 |
| 安全默认值 | 付款、银企直连、外部写操作默认关闭或 Mock |

### 7.1 Phase 1 → Phase 2 的架构变化

| 维度 | Phase 1 | Phase 2 (目标) |
|------|---------------|---------------|
| **Agent** | 单 Agent + 双工具 | Supervisor + 三岗 Agent 独立进程 |
| **知识库** | 本地文件 + CLI 更新 | 企业库接内部 OA API 自动同步 |
| **Tool** | Mock 数据 / 文本返回 | 接入真实查询接口（余额、汇率） |
| **Hooks** | 无 | Pre-tool 拦截、Post-write 自动测试 |
| **Skills** | 无 | cashier_skill、fx_skill、aml_skill |
| **部署** | 本地运行 | FastAPI + Docker 容器化 |

### 7.2 Phase 2 → Phase 3 的架构变化

| 维度 | Phase 2 | Phase 3 (目标) |
|------|---------|---------------|
| **MCP** | 预留接口 | 银企直连 MCP、汇率 MCP、AML MCP 上线 |
| **Plugins** | 无 | 内部 Plugin Marketplace，一键同步团队配置 |
| **多 Agent 协作** | 同机单进程 | LangGraph Cloud / 独立微服务 |
| **记忆** | 会话级 | 跨会话长期记忆（员工偏好、历史审批习惯） |
| **预测** | 基于规则 | 现金流预测模型（Time-Series + LLM 融合） |

### 7.3 性能预留

- **向量检索**: Qdrant 单机可支撑 100万 文档，预留集群模式接口
- **LLM 调用**: 支持多模型热切换（OpenAI 故障时切到 Qwen / DeepSeek）
- **并发**: FastAPI 层无状态设计，可水平扩展
- **缓存**: Redis 缓存频繁查询的余额、汇率（TTL 1-5 分钟）

---

## 8. 接口契约

### 8.1 Agent 间通信协议

各 Agent 通过 `TreasuryState` 传递消息，不直接调用对方的方法。
Supervisor 是唯一的调度中心。

### 8.2 对外 API 预留

```python
# FastAPI 层预留接口（Phase 2 实现）

@app.post("/api/v1/chat")
async def chat(request: ChatRequest):
    """通用对话接口，自动路由到对应 Agent"""
    pass

@app.post("/api/v1/agents/{agent_id}/invoke")
async def invoke_agent(agent_id: str, request: AgentRequest):
    """直接调用指定 Agent（用于调试和特定场景）"""
    pass

@app.get("/api/v1/knowledge/query")
async def query_knowledge(q: str, target: Literal["industry", "enterprise", "both"]):
    """双轨知识库查询接口"""
    pass

@app.post("/api/v1/approvals/{approval_id}/confirm")
async def confirm_approval(approval_id: str, request: ApprovalConfirmRequest):
    """HITL 人工确认接口"""
    pass

@app.get("/api/v1/audit/logs")
async def get_audit_logs(
    start_time: datetime, 
    end_time: datetime, 
    user_id: Optional[str] = None
):
    """审计日志查询（仅 admin 可调）"""
    pass
```

---

## 9. 关键指标 (KPIs)

| 指标 | 目标值 | 测量方式 |
|------|--------|---------|
| 法规引用准确率 | ≥ 95% | 人工抽查 100 条回答，对比原文 |
| 知识库检索召回率 | ≥ 90% | 标注 50 条查询，检查 Top-3 是否命中 |
| 角色权限误判率 | ≤ 1% | 模拟 200 次越权请求，统计拦截率 |
| HITL 触发合规率 | 100% | 所有高风险操作日志检查 |
| 平均响应时间 | ≤ 3s | API 层监控（不含 HITL 等待时间） |

---

*文档版本: v1.0*  
*最后更新: 2026-05-16*  
*对应 CLAUDE.md 版本: v1.0*  
*下次架构评审: 2026-08-15*
