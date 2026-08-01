# 企业资金智能体 (Corporate Treasury Agent)

一个**会自己获取数据、自己总结规律**的资金管理智能体：把付款流水喂给它，它自动提炼出发薪日、固定付款、部门预算偏差等业务规律，产出**预算执行差异、当月异动归因、滚动资金预测、头寸与调拨建议、外汇交易管控、关联方合规视图、审批流程画像、规律核验人审清单**八份决策材料。

核心设计哲学一句话：**LLM 总结规律，代码执行规律**——报告里每一个数字都来自确定性引擎，按指标登记（metrics.yaml）+ 数值血缘（lineage.json）可追溯、可重放。LLM 归纳环已落地且被三重闸约束：只见聚合摘要（绝不见单笔流水）、产出只进候选池等人工批准、claim 里的数字必须能被代码复算——**没有 API Key 时全流程照常跑**，LLM 是锦上添花不是依赖。

---

## 30 秒看结果

合成数据跑出的完整示例：**[examples/report-sample.md](examples/report-sample.md)**

系统从 245 笔原始付款流水中**自动学到**了这些（无任何人工标注）：

```text
[high] ADP Payroll/USD     :: 月度固定付款, 约每月15日, 均额 118,670（7笔/7月）   ← 发薪日
[high] Landlord Ltd/USD    :: 月度固定付款, 约每月5日,  均额 18,000（7笔/7月）    ← 房租
[high] 深圳子公司(关联方)   :: 月度固定付款, 约每月10日, 均额 151,880（7笔/7月）  ← 关联方节奏
[prov] IRAS/SGD            :: 月度固定付款, 约每月20日, 均额 88,226（3笔/3月）    ← 季度税期
[prov] HK Co/运营/USD      :: 周度付款基准 23,035（31周样本, CV=0.54）           ← 日常水平
```

然后据此生成下月预测、逐条标注固定付款日提醒、给出不超预算额度的购汇区间建议。

## 快速开始（纯本地，无需任何 API Key）

```bash
pip install pandas numpy openpyxl PyYAML tabulate
cd cashflow
python make_sample.py       # 生成合成付款数据+预算+余额快照（或把真实导出放进 data/raw/）
python ingest.py            # 付款流水标准化入库 SQLite（幂等，带来源与哈希）
python ingest_balances.py   # 余额快照入库（finweb 总览导出格式；接口直取用 ingest_balances_api.py）
python patterns.py          # 学习环：提炼规律 → patterns/patterns.yaml（三态 schema v2）
python validate.py          # 核验环：hit/violated/uncertain 写回 evidence，连续失败自动降级
python approve.py stats     # 审批 CLI：list / approve / refute——批准(approved)才进计算
python engine.py            # 行动环：八节报告+forecast.csv+lineage.json → runs/<date>/
python llm_patterns.py      # 可选 LLM 归纳环：聚合摘要→候选规律（无 LLM_API_KEY 自动跳过）
python ui.py                # 本地审批台：报告/规律审批/预测明细 → http://127.0.0.1:8787
```

接入真实数据只需两步：复制 `column_map.example.yaml` 为 `column_map.yaml` 改成你的导出列名；把 xlsx/csv 丢进 `data/raw/`。

针对具体系统导出另有专用适配器（用法见各脚本 docstring）：`ingest_liushui.py`（流水查询导出）、`ingest_approvals.py`（审批单导出）、`ingest_budget.py`（预算汇总）、`ingest_history.py`（历史管报日记账合并）；余额双路径——`ingest_balances.py`（finweb 余额总览 Excel 导出）与 `ingest_balances_api.py`（finweb 接口直取，`FINWEB_BASE_URL`/`FINWEB_TOKEN`）。

---

## 架构：三个自主环

```
定时唤起(cron) ─┐                ┌─ 对话入口（app/ LangGraph 智能体）
                ▼                ▼
┌──────────────────────────────────────────────┐
│              Treasury Agent                   │
│   日常巡检 / 周期规划 / 复盘学习 / 例外处置     │
└──────┬────────────────┬──────────────────────┘
       │ 工具调用         │ 读写（变更须人工批准）
┌──────▼────────┐  ┌────▼─────────────────────┐
│ 确定性引擎      │  │ 规律库 Pattern Store      │
│ engine.py      │  │ 每条规律带证据链+置信度     │
│ 差异/预测/管控  │  │ provisional 不参与计算     │
└──────┬────────┘  └──────────────────────────┘
┌──────▼───────────────────────────────────────┐
│ 数据层  ingest.py → SQLite（来源文件+行哈希）   │
└──────────────────────────────────────────────┘
```

**感知环**（自己获取信息）：付款/预算导出标准化入库，幂等可重跑；余额 API 直连与定时巡检在路线图中。

**学习环**（自己总结规律）：`patterns.py` 每次复盘从全量历史重新提炼三类规律——周度付款基准（缺失周补零，防稀疏高估）、固定节奏付款（自动识别发薪/房租/税期/关联方，并从周度基线中剔除防重复计数）、月内集中付款日。规律库是三态 schema（每条带 `pattern_id`+审计字段）：

- `candidate` → 重算自动产生，等人工审批；`provisional` 置信度的只在报告提示，**不进任何计算**
- `approved` → 人工批准（`approve.py`，非交互 ssh 友好）才进引擎计算——**strict 门控默认开**，approved=0 时自动回退置信度口径并标注"过渡模式"防空报告
- `refuted` → 只能人为否决，重算永不复活（防重提）
- `validate.py` 核验环用最近付款回测每条规律（hit/violated/uncertain 三态证据），违反明细进报告人审清单；连续 2 次失败的 approved **自动降级回 candidate**（永不自动 refuted）
- 人工状态按 `pattern_id` 在重算时继承；每份报告钉规律库版本，八节各标 metric_id，数值血缘落 `lineage.json`，审计可重放
- LLM 归纳环（`llm_patterns.py`）：`profiles.py` 先把流水压成聚合摘要（周统计量/日历直方图/Top收款方聚合额，**绝不含单笔**，payee 可代号化），LLM 归纳出的候选必须带 checks，代码三重闸（schema 校验 → 数字复算偏差>5% 丢弃 → pattern_id 去重）通过后以 `candidate/source=llm` 入库，与统计规律走同一条人工审批通道，**LLM 永不置 approved、永不影响引擎数字**

**行动环**（分级自主权）：L0 观察报告 → L1 生成提案人工批 → L2 人批后执行到审批门。自主权滑块（`policy.example.yaml`）按金额/关键词把每条预测行标成 `auto_report / flag_review / require_human` 三档——**只标注不拦截**；**付款执行永远不给 Agent 工具——安全靠工具不存在，不靠 Prompt。**

---

## 八份决策材料（engine.py 输出，各节标 metric_id，血缘见 lineage.json）

| 输出 | 回答的问题 | 机制 |
| --- | --- | --- |
| 预算执行差异 | 本月哪里超支/结余，根因是谁 | 预算 vs 可比口径实际，差异>10% 用 contrib 环比贡献定位根因 |
| 当月异动归因 | 这个月钱为什么多了/少了 | 环比贡献分解 Top 变动 + 日历对齐（发薪/固定日/月末）打"预期内"砍误报 |
| 滚动资金预测 | 未来 4 周每个主体/币种要付多少 | 周度基准 + 固定付款日逐笔排期，双轨不重复计数；行级标注人审档 |
| 头寸与调拨建议 | 缺口在哪、先从哪里调 | 币种余额−预测流出→主体级余缺贪心互补（preview-only，不生成指令） |
| 外汇交易管控 | 该换多少汇、什么期限 | 只对余额覆盖不了的缺口给区间，**上限钉预算额度**，期限匹配，风险中性 |
| 关联方合规 | 关联方资金往来是否透明可追溯 | 名单匹配逐笔追溯 + 近月趋势，异常节奏可见 |
| 审批流程画像 | 各类单据流转多快、瓶颈在哪 | approvals 画像：同意率、耗时中位数、性质分布、耗时 Top |
| 规律核验 | 哪些规律过期了、该信谁 | 三态证据核验，违反明细=人审清单，连续失败自动降级出计算口径 |

## 对话智能体（app/）

LangGraph 多智能体框架：意图分类路由、双轨知识库 RAG（行业法规 + 企业制度，Qdrant + bge 本地 embedding）、HITL 人工确认节点、审计日志与敏感信息脱敏工具、Gradio 本地 UI。详见 [DESIGN.md](DESIGN.md)。

## 安全与合规红线

- 真实数据、账户映射、规律库、真实阈值配置全部 gitignore，仓库只含代码与合成示例（`*.example` 模板模式）
- 敏感字段进 LLM 前脱敏：对话侧 `app/tools/masking.py`；数据侧 LLM 只见聚合 profile（绝不见单笔流水，payee 可代号化）
- 数值一律代码精算，禁止 LLM 心算——LLM 候选规律的每个数字都要过复算 verifier（偏差>5% 拒收）
- 规律进入计算的唯一通道是人工批准（strict 门控默认开）；核验连续失败自动降级出计算口径，否决（refuted）只能人为

## 路线图

- [x] Phase A：数据核心闭环（入库 → 规律 → 核验 → 报告），确定性由回归网守护——`tests/cashflow` 用固定种子合成数据对全链路数字做精确断言（golden 快照），CI 每次变更必跑
- [x] Phase A+：工程化五连——三态规律库+审批 CLI（批准才生效）→ 指标层+lineage 血缘 → 证据核验+自动降级 → 归因两函数 → LLM 归纳环（三重闸，离线可退化）
- [ ] Phase B：cashflow 工具封装为 MCP Server，接入对话智能体与 Claude Code
- [ ] Phase C：银行余额 API 直连 + 每日巡检告警（cron）+ 预测 vs 实际准确率曲线
- [ ] Phase D：审批系统直通（preview 成功才允许 create）+ 例外案例库

完整架构蓝图见 [DESIGN_V2.md](DESIGN_V2.md)（三环设计、Pattern Store 规范、分级自主权）。
