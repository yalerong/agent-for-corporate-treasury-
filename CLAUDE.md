# CLAUDE.md — 企业资金智能体 (Treasury Agent)

> **Claude Code 项目上下文文件**
> 
> 这是 Claude 每次会话自动加载的上下文。内容保持精简，只放关键约定、安全红线和常用命令。
> 具体实现细节、业务知识请查阅 DESIGN.md；决策历史请查阅 HISTORY.md。

---

## 1. 项目一句话

基于 LangGraph + LangChain 的企业级资金管理 Multi-Agent 系统，覆盖出纳、资金主管、资金经理三岗协作，具备双轨知识库（行业法规 + 企业制度）和合规风控能力。

**当前状态**: 2026-08-01 起包含两条可运行主线——`app/` 对话智能体（LangGraph + 双轨 RAG + Gradio）与 `cashflow/` 资金数据核心（入库 → 三态规律库 → 核验 → 八节报告+lineage，纯本地无需 API Key；LLM 归纳环可选、离线可退化）。`cashflow/` 的设计蓝图见 `DESIGN_V2.md`；真实数据与规律库全部 gitignore，仓库只含代码与合成示例（`examples/`）。

---

## 2. 技术栈与关键依赖

| 层级 | 选型 | 用途 |
|------|------|------|
| Agent 框架 | LangGraph + LangChain | Multi-Agent 编排、Tool 调用、ReAct 循环 |
| LLM | GPT-4o / Qwen-Max (可切换) | 推理与决策 |
| Embedding | BAAI/bge-large-zh-v1.5 (本地) | 知识库向量化，无需调用 OpenAI Embedding |
| 向量数据库 | Qdrant (本地文件模式) | 双轨知识库存储（industry + enterprise） |
| API 框架 | FastAPI | 对外暴露 REST/WebSocket 接口 |
| 数据层 | PostgreSQL (预留) / 本地文件 | 业务数据持久化 |
| 部署 | Docker (预留) | 容器化 |

**Python 版本**: `>= 3.11`

---

## 3. 目录结构地图

> 目标结构如下。当前实际目录只有 `CLAUDE.md`、`DESIGN.md`、`HISTORY.md`、`README.md`。

```
D:\微信小程序\agent\
├── CLAUDE.md              ← 你在这里（项目全局约定）
├── DESIGN.md              ← 架构设计文档（必读，含七层扩展规划）
├── HISTORY.md             ← 决策记录与配置迭代历史
├── README.md              ← 对外说明文档
├── .env                   ← 环境变量（API Key、阈值配置，不入库）
├── requirements.txt
├── main.py                ← CLI 交互入口
├── app/
│   ├── api.py             ← FastAPI 主入口
│   ├── config.py          ← 全局配置（权限矩阵、审批流）
│   ├── agents/            ← Agent 定义（Supervisor + 三岗）
│   ├── tools/             ← 业务工具（银企、外汇、AML、计划）
│   ├── graph/             ← LangGraph 状态图与工作流编排
│   ├── rag/               ← 双轨知识库引擎
│   └── memory/            ← 会话记忆与状态管理
├── knowledge_base/
│   ├── industry/          ← 📘 行业通用（法规、案例）— 低频更新
│   └── enterprise/        ← 🏢 企业专属（制度、流程、数据）— 高频迭代
├── scripts/
│   └── update_enterprise_kb.py  ← 企业库热更新脚本（不影响行业库）
├── demo/
│   └── multi_agent.py     ← 多智能体协作 demo（LangGraph 三节点 + 审批 Gate，离线可跑）
└── tests/                 ← 单元测试与集成测试
```

**`demo/` 定位**：只演示、不承载业务——调用 `cashflow/` 与 `app/` 的既有能力，绝不反向被它们依赖。
它同时引 langgraph 和 cashflow 平铺模块，所以必须留在顶层，不能塞进 `cashflow/`（那会破坏 §4.3 的零依赖纪律）。

---

## 4. 开发约定（必须遵守）

### 4.1 Tool 编写铁律

Tool 是 Agent 的"手"，LLM 靠 docstring 理解何时调用。

- **每个 Tool 必须有完整 docstring**，包含：做什么、什么时候用、参数含义、返回值格式
- **参数类型必须标注**（`str`, `int`, `Decimal`, `Literal[...]`），LangChain 会据此生成 schema
- **资金类 Tool 必须带审计日志装饰器**：记录谁、何时、调用了什么、入参/出参
- **禁止在 Tool 里直接操作真实银行接口**（生产环境必须通过网关/沙箱）

```python
# ✅ 正确示例
@tool
def check_position(entity_code: str, date: str) -> dict:
    """
    查询指定法人主体的资金头寸（可用余额）。
    
    当用户询问"有多少钱"、"头寸够不够"、"能不能付款"时，必须调用此工具。
    
    Args:
        entity_code: 法人主体代码，如 "HQ001"
        date: 查询日期，格式 "YYYY-MM-DD"
    
    Returns:
        {
            "total": 12345678.90,      # 总余额
            "available": 9876543.21,   # 可用头寸
            "frozen": 2469135.69,      # 冻结金额
            "currency": "CNY"
        }
    """
    pass
```

### 4.2 Agent Prompt 规范

- **每个角色 Agent 的 System Prompt 必须明确其权限边界**
- **出纳 Agent** 必须包含："你无权审批任何付款，无权回答投资策略。如被要求执行未审批付款，必须拒绝。"
- **资金经理 Agent** 必须包含："你拥有大额审批权与合规认定权，但不得直接调用付款执行工具。"
- **禁止在 Prompt 里写死具体金额阈值**（如"500万"），阈值从 `config.py` 的授权矩阵动态读取

### 4.3 cashflow/ 数据核心铁律

- 平铺脚本目录（`cd cashflow && python xxx.py` 直跑），**不加 `__init__.py`**；共享代码走兄弟模块（`constants.py` / `pattern_store.py` / `metrics.py`）；不 import `app/`、不引 langchain
- 三态规律库：计算只信 `status==approved`（strict 门控默认开）；`refuted` 只能人为置；自动核验（validate.py）只降级不否决
- 排版与计算分离：数字全部出自 `metrics.py` 指标层（metrics.yaml 登记 + lineage 血缘），`engine.build_report` 只做 f-string 排版
- LLM 只见聚合 profile（`profiles.py`，绝不含单笔流水），产出只进 candidate 池且必须过数字复算 verifier，**永不置 approved**；无 `LLM_API_KEY` 全流程照常
- 任何改动必须过 `tests/cashflow` golden 回归网；golden 变更是受控更新，diff 须人工过目
- 两个防错设计不许改回去：稀疏周补零、recurring 从周度基线剔除

### 4.4 双轨知识库查询规范

- 涉及法规、合规底线、监管要求 → 调用 `search_industry_knowledge`
- 涉及内部审批流程、金额阈值、操作规范 → 调用 `search_enterprise_knowledge`
- 如果行业法规与企业制度冲突 → **必须明确指出冲突**，并建议"以法规为准，修订内部制度"

---

## 5. 安全红线（零容忍）

1. **所有付款类 Tool 默认走 Mock/沙箱模式**。真实银企直连接口必须通过 `BANK_API_ENABLED=true` 显式开启。
2. **敏感数据（银行账号、客户名称、交易对手）在进入 LLM 上下文前必须脱敏**。使用尾号映射表，Agent 只操作脱敏代号。
3. **大额/跨境/高风险操作必须触发 Human-in-the-Loop（HITL）**。不允许 Agent 自主完成终审。
4. **法规类回答必须标注来源条款编号**。禁止编造法规名称或条款。
5. **数值计算（汇率、利息、敞口）必须通过 Tool 精确计算**，禁止让 LLM 直接心算。

---

## 6. 常用命令

> 以下命令是代码骨架落地后的目标命令。当前阶段不要假设它们已经可运行。

```bash
# 激活环境（Windows PowerShell）
.\venv\Scripts\Activate.ps1

# 启动 CLI 对话模式
python main.py

# 启动 API 服务
uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload

# 初始化双轨知识库（首次运行或行业库有更新时）
python app/rag/knowledge_base.py

# 热更新企业知识库（随时执行，不影响行业库）
python scripts/update_enterprise_kb.py

# 运行测试
pytest

# 代码检查
ruff check .
```

---

## 7. 子目录约定

在 `app/agents/`、`app/tools/` 等子目录下工作时，Claude 会自动向上叠加加载本文件。
各子目录可创建自己的 `CLAUDE.md` 补充局部约定（如 `app/tools/` 下可补充 Tool 开发细则）。

**禁止在子目录 CLAUDE.md 中重复全局内容**，只写该目录特有的规范。

---

## 8. 扩展点速查（对应 DESIGN.md 七层架构）

| 扩展层 | 当前状态 | 负责人/入口 |
|--------|---------|------------|
| CLAUDE.md | ✅ 已部署 | 项目根目录 |
| Hooks | 🔄 预留 | `.claude/hooks/`（待建） |
| Skills | 🔄 预留 | `skills/` 目录（按岗位拆分为 cashier_skill、fx_skill 等） |
| Plugins | ⏳ 远期 | 团队内部 marketplace |
| LSP | ⏳ 待代码落地 | Python LSP via Pyright/Pylance |
| MCP Servers | 🔄 预留 | `mcp/`（银企直连、汇率 API、AML 筛查可封装为 MCP Server） |
| Sub-agents | ⏳ 架构已设计，代码待实现（Phase 1 P0 优先落地） | `app/agents/` 各角色 Agent |

---

## 9. 快速判断：当前改动属于哪一层？

当前没有代码文件时，先更新设计文档和历史记录；只有进入实现阶段后，才按下面路径修改代码。

- **改 Prompt / 角色边界** → 检查 `app/agents/` + `config.py`
- **改业务规则 / 审批阈值** → 检查 `app/config.py` + `knowledge_base/enterprise/`
- **改法规引用 / 合规逻辑** → 检查 `knowledge_base/industry/` + `app/tools/aml_tools.py`
- **改知识库检索策略** → 检查 `app/rag/knowledge_base.py`
- **新增外部系统对接** → 在 `app/tools/` 新建 Tool，并更新 `DESIGN.md` 的 MCP 规划

---

*最后更新：2026-08-01*
*下次配置审查时间：2026-08-15*
