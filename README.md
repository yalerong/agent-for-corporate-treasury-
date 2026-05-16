# 企业资金智能体 (Corporate Treasury Agent)

基于 LangChain / LangGraph 构建的企业级资金管理多智能体系统，覆盖出纳、资金主管、资金经理等多岗位协作，具备资金调拨、资金计划、外汇管理、账户管理、反洗钱审查等专业能力。

---

## 当前状态

> 本目录当前是 **架构与产品设计文档包**，不是可运行代码项目。

截至 2026-05-16，仓库内仅包含 `README.md`、`DESIGN.md`、`CLAUDE.md`、`HISTORY.md` 四个文档文件。后文出现的 `app/`、`main.py`、`requirements.txt`、`knowledge_base/`、`scripts/`、`tests/` 等路径均为 **目标项目结构**，尚未落地为代码。

当前阶段定义为：

- **Phase 0：方向澄清与文档基线**
- 目标：统一业务边界、架构方向、安全红线、目录规划和后续开发顺序
- 非目标：不要求现在能安装依赖、启动服务、运行测试或连接真实业务系统

后续进入代码开发前，必须先以本文档和 `DESIGN.md` 为准，创建最小可运行骨架，再推进 RAG、Agent、Tool、审批流等功能。

---

## 📋 目录

- [当前状态](#当前状态)
- [项目简介](#项目简介)
- [核心特性](#核心特性)
- [系统架构](#系统架构)
- [角色分工](#角色分工)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [配置说明](#配置说明)
- [合规与安全](#合规与安全)
- [开发路线图](#开发路线图)
- [贡献指南](#贡献指南)

---

## 项目简介

企业资金智能体（Treasury Agent）是一套面向集团企业财务管理部的 AI 数字员工解决方案。系统通过 Multi-Agent 架构模拟真实企业资金岗位的职责边界与协作流程，将大语言模型的推理能力与企业资金管理制度、银企直连系统、风控引擎深度融合，实现：

- **智能问答**：法规政策、内控制度、操作手册的精准检索与解读
- **业务辅助**：现金流预测、头寸管理、外汇套保方案生成
- **流程协作**：资金调拨申请、审批、执行的全链路自动化
- **合规风控**：反洗钱筛查、大额交易监测、权限隔离

---

## 核心特性

### 1. 多角色 Agent 协作
- **Supervisor Agent**：中央调度中枢，负责意图识别、任务路由、权限校验、审批流控制
- **Cashier Agent（出纳）**：账户查询、银行对账、付款执行（仅执行已审批指令）
- **Treasury Supervisor Agent（资金主管）**：资金计划、头寸管理、账户管理、日常审批
- **Treasury Manager Agent（资金经理）**：外汇风控、套期保值、反洗钱审查、大额审批

### 2. 专业知识库（RAG）
三级知识库体系支撑专业决策：
- **法规政策库**：外汇管理条例、支付结算办法、反洗钱法、跨境资金池政策等
- **企业内控库**：资金管理办法、授权审批矩阵、账户管理办法、外汇风险管理办法
- **业务数据与案例库**：历史现金流数据、套保交易记录、行业最佳实践

### 3. 业务系统对接
通过标准化 Tool 接口对接：
- 银企直连系统（查询、转账、回单）
- 汇率数据源（路透 / 万得 / Bloomberg）
- AML 名单筛查系统（OFAC、联合国、PEP）
- ERP / 资金管理系统（SAP、Oracle、九恒星、拜特等）

### 4. 合规与审计
- **Human-in-the-Loop（HITL）**：大额付款、套保交易等关键节点强制人工确认
- **不可篡改日志**：完整的操作审计链，记录谁、何时、通过哪个 Agent、调用了什么工具
- **数据脱敏**：银行账户等敏感信息在进入 LLM 前完成脱敏处理
- **RBAC 权限隔离**：基于岗位职责的精细化权限控制

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    用户交互层（Web / 企微 / 钉钉）              │
└───────────────────────┬─────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────┐
│                 Supervisor Agent（资金中枢）                  │
│   • 意图识别与路由    • 权限校验（RBAC）    • 流程编排与审批流   │
└──┬──────────────┬──────────────┬──────────────────┬─────────┘
   │              │              │                  │
┌──▼──┐    ┌────▼────┐   ┌─────▼──────┐   ┌──────▼──────┐
│出纳  │    │ 资金主管 │   │  资金经理   │   │  知识库检索  │
│Agent │    │  Agent  │   │   Agent    │   │   Agent     │
└──┬──┘    └────┬────┘   └─────┬──────┘   └──────┬──────┘
   │            │              │                  │
   └────────────┴──────────────┴──────────────────┘
                        │
         ┌──────────────▼──────────────┐
         │        工具层（Tools）         │
         │ 银企直连 │ 汇率API │ 风控引擎  │
         │  ERP   │ AML名单 │ 估值模型  │
         └─────────────────────────────┘
                        │
         ┌──────────────▼──────────────┐
         │      专业知识库（RAG）        │
         │ 外管法规 │ 公司制度 │ 行业案例 │
         │ 操作手册 │ 合同模板 │ 历史数据 │
         └─────────────────────────────┘
```

---

## 角色分工

| 岗位 Agent | 核心职责 | 可操作工具 | 决策权限 |
|-----------|---------|-----------|---------|
| **Supervisor** | 意图识别、任务路由、权限校验、审批流控制 | 用户管理、审批流引擎、日志审计 | 路由权、流程控制权 |
| **出纳 Agent** | 收付款执行、银行对账、回单管理、现金盘点 | 银企直连付款、余额查询、对账单获取 | **仅执行权，无审批权** |
| **资金主管 Agent** | 资金计划编制、头寸管理、短期融资、账户管理 | 现金流预测、头寸计算、资金池状态、银行额度 | 计划权、日常调拨审批权（≤阈值） |
| **资金经理 Agent** | 外汇风险管理、投资决策、跨境资金池、反洗钱 | 汇率查询、远期/掉期试算、敞口分析、AML筛查 | 投资决策权、大额审批权、合规认定权 |

> **重要设计原则**：角色严格隔离。出纳 Agent 被问及投资策略时必须拒绝并转交；资金经理 Agent 不得直接调用付款工具。

---

## 技术栈

| 层级 | 技术选型 |
|-----|---------|
| **Agent 框架** | LangGraph + LangChain |
| **大语言模型** | GPT-4o / Claude 3.5 / 通义千问 / 私有化模型（Qwen2.5-72B） |
| **向量数据库** | Milvus / Qdrant / Chroma |
| **Embedding** | BGE-large-zh / text-embedding-3-large |
| **Web 框架** | FastAPI / LangServe |
| **数据库** | PostgreSQL（业务数据）、Redis（缓存/消息队列） |
| **任务队列** | Celery / RQ |
| **部署** | Docker + Kubernetes |

---

## 快速开始

> 下面内容是 **代码落地后的目标启动方式**。当前目录尚无 `requirements.txt`、`main.py`、`app/` 等文件，不能直接执行。

### 环境准备

- Python >= 3.11
- OpenAI API Key 或兼容的 LLM 服务
- （可选）向量数据库服务

### 安装依赖

```bash
# 克隆项目
git clone <仓库地址>
cd treasury-agent

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或 venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 配置环境变量

复制 `.env.example` 为 `.env`，并填写以下关键配置：

```env
# LLM 配置
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_BASE_URL=https://api.openai.com/v1
# 或国产模型
DASHSCOPE_API_KEY=sk-xxxxxxxx

# 向量数据库
VECTOR_DB_URL=http://localhost:19530

# 数据库
DATABASE_URL=postgresql://user:pass@localhost:5432/treasury

# 银企直连（测试环境）
BANK_API_BASE_URL=https://sandbox-api.bank.com
BANK_API_KEY=xxxxxxxx

# 合规配置
AML_SCREENING_ENABLED=true
MAX_AUTO_APPROVAL_AMOUNT=5000000  # 自动审批金额上限（元）
HITL_ENABLED=true  # 是否开启人工审批节点
```

### 初始化知识库

```bash
# 加载法规、制度文档到向量数据库
python scripts/init_knowledge_base.py \
  --docs-dir ./knowledge_base/ \
  --category regulation,policy,case
```

### 运行项目

```bash
# 启动主服务
python main.py

# 或通过 FastAPI 启动 API 服务
uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload
```

### 快速体验

```bash
# 命令行交互模式
python cli.py

# 输入示例
>> 请查询集团总部资金池的当前可用头寸
>> 帮我制定一份下季度美元应收账款的套期保值方案
>> 请对这笔跨境付款进行反洗钱合规审查
```

---

## 项目结构

> 这是目标结构，不代表当前磁盘上已经存在这些文件。代码开发阶段应按此结构逐步补齐，并在每次补齐后更新 `HISTORY.md`。

```
treasury-agent/
├── app/
│   ├── __init__.py
│   ├── api.py                  # FastAPI 主入口
│   ├── config.py               # 全局配置
│   ├── agents/                 # Agent 定义
│   │   ├── supervisor.py       # Supervisor Agent
│   │   ├── cashier.py          # 出纳 Agent
│   │   ├── treasury_supervisor.py  # 资金主管 Agent
│   │   ├── treasury_manager.py     # 资金经理 Agent
│   │   └── knowledge_agent.py    # 知识库检索 Agent
│   ├── tools/                  # 业务工具
│   │   ├── bank_tools.py       # 银企直连工具
│   │   ├── fx_tools.py         # 外汇管理工具
│   │   ├── aml_tools.py        # 反洗钱工具
│   │   ├── planning_tools.py   # 资金计划工具
│   │   └── accounting_tools.py # 财务/账户工具
│   ├── graph/                  # LangGraph 工作流定义
│   │   └── treasury_graph.py   # 主图编排
│   ├── memory/                 # 记忆与状态管理
│   │   └── session_store.py
│   └── rag/                    # RAG 知识库
│       ├── retriever.py
│       └── document_loader.py
├── knowledge_base/             # 知识库文档源
│   ├── regulations/            # 法规政策
│   ├── policies/               # 企业内控制度
│   └── cases/                  # 案例与最佳实践
├── scripts/                    # 运维脚本
│   └── init_knowledge_base.py
├── tests/                      # 测试用例
├── .env.example
├── requirements.txt
├── README.md
└── main.py
```

---

## 配置说明

### 1. 角色权限矩阵

在 `app/config.py` 或数据库中配置授权矩阵：

```python
AUTHORIZATION_MATRIX = {
    "cashier": {
        "can_execute_transfer": True,
        "can_approve_transfer": False,
        "max_single_amount": 0,
    },
    "treasury_supervisor": {
        "can_execute_transfer": False,
        "can_approve_transfer": True,
        "max_single_amount": 5_000_000,  # 500万
    },
    "treasury_manager": {
        "can_execute_transfer": False,
        "can_approve_transfer": True,
        "max_single_amount": 50_000_000,  # 5000万
    }
}
```

### 2. 知识库分类标签

| 分类标签 | 说明 | 示例 |
|---------|------|------|
| `regulation` | 国家法规与监管政策 | 《外汇管理条例》、《反洗钱法》 |
| `policy` | 企业内部制度 | 《资金管理办法》、《授权审批矩阵》 |
| `case` | 案例与操作指引 | 跨境资金池搭建案例、套保会计处理 |
| `market` | 市场数据与产品 | 各银行现金管理产品对比、汇率走势 |

### 3. 审批流配置

```yaml
# config/approval_flow.yaml
flows:
  domestic_transfer:
    - amount: [0, 5000000]
      approvers: ["treasury_supervisor"]
    - amount: [5000000, 50000000]
      approvers: ["treasury_supervisor", "treasury_manager"]
    - amount: [50000000, 999999999999]
      approvers: ["treasury_manager", "cfo"]
      hitl: true  # 强制人工确认

  cross_border_transfer:
    - amount: [0, 999999999999]
      approvers: ["treasury_manager"]
      hitl: true
      aml_check: true
```

---

## 合规与安全

### ⚠️ 生产环境必做检查清单

- [ ] **HITL 开启**：所有付款执行、套保交易确认已配置人工审批节点
- [ ] **数据脱敏**：银行账户完整号码不得进入 LLM 上下文，使用脱敏代号映射
- [ ] **审计日志**：所有 Agent 决策、Tool 调用记录已接入不可篡改日志系统
- [ ] **幻觉防控**：法规类回答必须标注引用来源条款编号；数值计算必须通过 Tool 完成
- [ ] **权限隔离**：每个用户会话已绑定角色，Supervisor 在每个节点前完成权限校验
- [ ] **测试隔离**：银企直连等外部接口当前处于沙箱/测试环境
- [ ] **模型安全**：如涉及上市公司敏感财务数据，优先使用私有化部署模型或本地化方案

---

## 开发路线图

### Phase 0：方向澄清与文档基线（当前）
- 明确产品定位：企业资金管理领域的决策辅助系统，而非自动付款系统
- 明确角色边界：出纳、资金主管、资金经理、Supervisor 的职责和越权限制
- 明确安全红线：HITL、脱敏、审计、沙箱、法规引用、数值计算 Tool 化
- 明确目标目录结构和第一批代码开发入口

### Phase 1：知识问答助手（4-6 周）
- 构建 RAG 知识库，支持法规/制度/案例问答
- 出纳/主管/经理知识助手上线，回答带原文引用

### Phase 2：单岗位 Agent（6-8 周）
- 出纳 Agent：账户查询、银行对账、回单管理
- 主管 Agent：现金流预测、头寸预警、资金计划填报辅助
- **本阶段仅对接查询类接口，不涉及付款**

### Phase 3：多 Agent 协作（8-10 周）
- Supervisor 调度中枢上线
- 跑通"资金调拨申请 → 头寸检查 → 合规审查 → 审批 → 执行"全链路
- 加入 HITL 人工审批节点，对接测试环境付款接口

### Phase 4：智能决策（持续迭代）
- 外汇敞口自动监测与套保方案推荐
- AML 可疑交易模式识别与预警
- 现金流预测模型与业务数据深度融合
- 多模态支持（银行回单 OCR 识别、报表解析）

---

## 贡献指南

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/xxx`
3. 提交变更：`git commit -m "feat: xxx"`
4. 推送分支：`git push origin feature/xxx`
5. 提交 Pull Request

### 代码规范
- 遵循 PEP 8 规范
- 所有 Tool 函数必须包含完整的 docstring（Agent 依赖其理解工具用途）
- 涉及资金计算的代码必须附带单元测试
- 提交前运行：`pytest && ruff check .`

---

## 免责声明

本项目仅供学习研究与企业内部数字化探索使用。**任何涉及真实资金转账的操作必须在严格的测试环境验证，并遵循企业内控与金融监管要求。** 开发者与贡献者不对因使用本项目而产生的任何资金损失或合规风险承担责任。

---

## 联系我们

如有问题或合作意向，请联系：
- 项目 Issues：[GitHub Issues]
- 邮箱：treasury-agent@example.com

---

**让 AI 成为最懂资金管理的数字员工。**
