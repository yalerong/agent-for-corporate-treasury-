"""验证：知识类问题是否被误分到 aml/fx/transfer 等业务意图。"""
from app.agents.nodes import _classify_intent

CASES = [
    # 应该全部为 knowledge
    ("大额交易报告标准是什么？", "knowledge"),
    ("跨境资金调拨需要办什么手续？", "knowledge"),
    ("公司内部调拨的审批权限是怎么分的？", "knowledge"),
    ("可疑交易的识别要点有哪些？", "knowledge"),
    ("公司内部的留痕标准和反洗钱法的报告标准有什么区别？", "knowledge"),
    ("我们公司的调拨审批和外汇管理条例的跨境登记是什么关系？", "knowledge"),
    ("跨境调拨在国家法规和公司制度上分别要做什么？", "knowledge"),
    ("可疑交易识别上我们公司比国家要求严在哪？", "knowledge"),
    ("一笔 150 万的单位调拨需要上报反洗钱中心吗？", "knowledge"),
    ("800 万的境内付款审批流程是什么？", "knowledge"),
    ("5 万元跨境调拨需要什么手续？", "knowledge"),
    ("内部制度和法规冲突时怎么办？", "knowledge"),
    # 对照组：真业务请求
    ("帮我调拨 500 万到 ACC-工行-001", "transfer"),
    ("我要做一笔 1 亿美元的远期套保", "fx"),
    ("发现一笔可疑交易需要上报", "aml"),
    ("查工行账户今天的余额", "inquiry"),
]

ok = 0
for q, expected in CASES:
    actual = _classify_intent(q)
    status = "OK " if actual == expected else "MISS"
    print(f"[{status}] expected={expected:<10} actual={actual:<10} | {q}")
    if actual == expected:
        ok += 1
print(f"\n{ok}/{len(CASES)} 通过")
