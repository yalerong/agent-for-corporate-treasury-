# 企业资金智能体 (Corporate Treasury Agent)

一个**会自己获取数据、自己总结规律**的资金管理智能体：把付款流水喂给它，它自动提炼出发薪日、固定付款、部门预算偏差等业务规律，产出**预算执行差异、当月异动归因、滚动资金预测、头寸与调拨建议、外汇交易管控、关联方合规视图、审批流程画像、规律核验人审清单**八份决策材料。

核心设计哲学一句话：**LLM 总结规律，代码执行规律**——报告里每一个数字都来自确定性引擎，按指标登记（metrics.yaml）+ 数值血缘（lineage.json）可追溯、可重放。LLM 归纳环已落地且被三重闸约束：只见聚合摘要（绝不见单笔流水）、产出只进候选池等人工批准、claim 里的数字必须能被代码复算——**没有 API Key 时全流程照常跑**，LLM 是锦上添花不是依赖。

---

## 30 秒看结果

合成数据跑出的完整示例：**[examples/report-sample.md](examples/report-sample.md)**；
网页版展示（单文件静态页，可开 GitHub Pages）：**[docs/index.html](docs/index.html)**——
`python cashflow/export_demo.py` 重新生成 `docs/data.json` 后，`python -m http.server -d docs` 本地预览。

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

## 多智能体协作 demo（一条命令，无需任何 Key）

把 DESIGN.md 的多智能体设计跑成看得见的东西：三个角色节点串成一条链，**每一条"会动钱"的建议都必须过审批 Gate**。

> **可点的网页版**：[docs/multi-agent.html](docs/multi-agent.html)——自包含单文件，浏览器直接打开即可。
> 同一轮协作的重放，审批处真的停下来等你点批准/驳回，还能拖动审批硬限看红线实时重算
> （裁决逻辑与 `policy.review_tier` 对拍一致；无外部依赖，可直接托管到任意静态站点）。
>
> **在线版**：<https://treasury-demo.pages.dev/> —— 就是这张页面。
> **这个链接是公开的，没有任何访问控制**，拿到就能打开；页里数据全部合成，不含真实主体与流水。
> `robots.txt` 与 `X-Robots-Tag` 只让它不进搜索引擎，那不是访问控制。

### 重新部署演示站

```bash
python scripts/build_site.py          # 拼出 _site/
npx wrangler pages deploy _site --project-name treasury-demo --branch main
```

`scripts/build_site.py` 存在的理由不是"构建"（页面是自包含单文件），是两件手工做不可靠的事：

1. **线上首页是 `docs/multi-agent.html`，不是 `docs/index.html`**——部署时要改名，靠记的话迟早传错一张页面上去；
2. **别整目录传**——`docs/data.json` 是给另一张（当前没上线的）页用的，不该出现在公网上；
   `_headers` 反过来必须传上去，漏掉它安全头就静默消失、页面照常打开，不看响应头发现不了。

`docs/_headers` 是给 Cloudflare Pages 读的响应头。这个站纯静态、无后端、数据全合成，
所以这几条挡的不是数据泄露，是三件具体的事：`frame-ancestors 'none'`（别人把演示站嵌进
自己页面里做点击劫持）、`connect-src 'self'`（页面即便被注入也发不出外部请求）、
`X-Robots-Tag: noindex`（`robots.txt` 只管爬虫抓不抓，这个头把 noindex 标到每个响应上）。
`script-src`/`style-src` 保留 `'unsafe-inline'`——这是自包含单文件页，脚本样式全是内联的。

`tests/test_docs_site.py` 钉住这些：产物文件清单、首页确实取自 `multi-agent.html`、
`data.json` 不上线、`_headers` 规则行是 `/*`、页面没引 CSP 放行不了的东西。
**最后一条是关键**：本地双击打开 HTML 不发 CSP，加个 CDN 脚本在本地一切正常，线上才发现被挡。

**部署前先验证的做法**：先 `--branch <临时名>` 部一个 preview，`curl -D -` 确认头和内容，
验完 `npx wrangler pages deployment delete <id> --project-name treasury-demo -f` 删掉，
再部生产。另外每次部署都会留一个永久公开的 `<hash>.pages.dev`，**部完顺手把上一版删掉**，
只保留当前线上那一个。

```bash
pip install -r requirements.txt
python demo/multi_agent.py            # 交互：暂停处敲 y/n 决定批不批
python demo/multi_agent.py --auto     # 非交互：预置应答，CI/录屏用
```

首次运行会在临时目录自举一遍合成流水线（固定种子，约 5 秒），跑完自动清理——不碰真实数据，也不需要任何凭据。

**关于"不需要 Key"的准确说法**：不是这套系统不用 LLM，而是**判定不靠 LLM**。金额、缺口、档位、红线全部由代码算出（`metrics` 指标层 + `policy.review_tier`），无 Key 时缺的只是角色那句话——它退化成确定性模板。配上 Key 后，LLM 负责把聚合摘要讲成人话、从流水里归纳新规律进候选池等人批，**判定链路一行都不变**。这正是 `llm_patterns.py` 那套三重闸的同款分工：LLM 总结规律，代码执行规律。

**预期输出**（节选）：

```text
[编排器] LLM 通道: 未配置（无 ANTHROPIC_API_KEY / LLM_API_KEY），角色叙述走确定性模板，流程照常
[现金流分析 Agent] 调用只读工具（聚合口径，无单笔流水）: balances_summary, query_forecast, validation_findings, payments_summary
[现金流分析 Agent]   余额 HK Co CNY: 80,000.00
[现金流分析 Agent] 截至 2026-07-30，未来 4 周待付 CNY 151,880、SGD 156,425、USD 240,627；核验 4 条规律中 1 条失效。
[调拨建议 Agent] 产出 5 条草案（尚未生效，全部待批）:
[调拨建议 Agent]   草案1 [transfer] HK Co → SG Co 调拨 CNY 80,000.00 —— SG Co 该币种缺口 91,879.68，HK Co 可用 80,000.00
[调拨建议 Agent]   草案3 [prefund] 为 深圳子公司(关联方) 备付 CNY 151,879.68 —— 预测来源 recurring@10日（high/approved）

[人工审批 Gate] ⏸ 需要人工确认: HK Co → SG Co 调拨 CNY 80,000.00
[人工审批 Gate]   档位 require_human｜理由: 金额 ≥ CNY flag_review 阈值 50,000；跨主体调拨：动其他主体的钱，一律人工确认
[人工审批 Gate]   批准吗? (y/n): y

[人工审批 Gate] 草案2/5 购汇 CNY 11,879.68 → 档位 auto_report｜✅ 放行（仅标注，不改动任何账务）
[人工审批 Gate] 草案3/5 为 深圳子公司(关联方) 备付 CNY 151,879.68 → 档位 require_human｜❌ 拒绝
[人工审批 Gate]   触发红线: policy 关键词命中「关联方」
[人工审批 Gate]   触发红线: 金额 151,879.68 CNY ≥ 硬限 120,000.00（demo 红线：超此额度不走 Agent 通道，退线下双签）
[编排器] 汇总: 共 5 条草案 → 放行 3、人工批准 1、人工驳回 0、红线拒绝 1
[编排器] 本 demo 全程只读：没有任何付款执行工具，Agent 手上没有这把枪
```

**三档判定**（金额/关键词阈值复用 `cashflow/policy.yaml`；硬限用 `--hard-limit` 或 `DEMO_GATE_HARD_LIMIT` 调）：

| 判定 | 条件 | 处置 |
| --- | --- | --- |
| ✅ 放行 | policy 档位 auto_report / flag_review | 进汇总并标注档位，不生成任何指令 |
| ⏸ 待人工 | 档位 require_human（大额、关键词命中，或跨主体调拨这类固有红线） | LangGraph `interrupt` 暂停，等人 y/n 后 `Command(resume)` 恢复 |
| ❌ 拒绝 | require_human 且金额 ≥ 硬限 | 连人工通道都不开，退线下双签，并逐条打印触发了哪些红线 |

**与设计文档的对应关系**：

| demo 里的东西 | 对应设计章节 | 说明 |
| --- | --- | --- |
| 三节点 StateGraph + 条件边 | DESIGN.md §4.2.2 状态图 / §4.2.3 条件边 | HITL 是横切节点，任何 Agent 都能触发，不绑定角色 |
| 分析 → 建议 → 审批的多阶段编排 | DESIGN.md §5.1 资金调拨流程 | 同一条"头寸检查 → 合规判定 → 人工确认"链路的最小实现 |
| Gate 三档判定 | DESIGN_V2.md §1.3 分级自主权 | demo 停在 **L1 提议**：生成方案、人逐笔批，不越到 L2 执行 |
| 只调 `mcp_server` 聚合工具 | CLAUDE.md §4.3 三重闸 / DESIGN.md Layer 6 | Agent 拿不到单笔流水，只见聚合口径 |
| 无 Key 走确定性模板 | 开篇「LLM 是锦上添花不是依赖」 | 与 `llm_patterns.py` 同款降级：`get_client()` 返回 None 即跳过 |

编排直接用 LangGraph（已在 `requirements.txt`，`app/` 在用），没有自研状态机——DESIGN.md §4.2.0 已定死"状态机用 LangGraph、HITL 走 `interrupt`"，另造一个反而与设计脱节。

## 安全与合规红线

- 真实数据、账户映射、规律库、真实阈值配置全部 gitignore，仓库只含代码与合成示例（`*.example` 模板模式）
- 敏感字段进 LLM 前脱敏：对话侧 `app/tools/masking.py`；数据侧 LLM 只见聚合 profile（绝不见单笔流水，payee 可代号化）
- 数值一律代码精算，禁止 LLM 心算——LLM 候选规律的每个数字都要过复算 verifier（偏差>5% 拒收）
- 规律进入计算的唯一通道是人工批准（strict 门控默认开）；核验连续失败自动降级出计算口径，否决（refuted）只能人为

## 路线图

- [x] Phase A：数据核心闭环（入库 → 规律 → 核验 → 报告），确定性由回归网守护——`tests/cashflow` 用固定种子合成数据对全链路数字做精确断言（golden 快照），CI 每次变更必跑
- [x] Phase A+：工程化五连——三态规律库+审批 CLI（批准才生效）→ 指标层+lineage 血缘 → 证据核验+自动降级 → 归因两函数 → LLM 归纳环（三重闸，离线可退化）
- [x] Phase B（Claude Code 侧）：cashflow 封装为只读 MCP Server（`mcp_server.py`，10 工具：报告/单节/lineage/预测/规律/核验/付款聚合/余额），仓库根 `.mcp.json` 注册即用；本地审批台 UI（`ui.py`）同步上线。对话智能体（app/）接线待做
- [ ] Phase C：银行余额 API 直连 + 每日巡检告警（cron）+ 预测 vs 实际准确率曲线
- [ ] Phase D：审批系统直通（preview 成功才允许 create）+ 例外案例库

完整架构蓝图见 [DESIGN_V2.md](DESIGN_V2.md)（三环设计、Pattern Store 规范、分级自主权）。
