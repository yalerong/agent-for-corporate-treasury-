"""规律审批 CLI（非交互子命令，ssh 友好）。三态：candidate / approved / refuted。

用法:
    python approve.py list [--status candidate] [--confidence high] [--type recurring]
    python approve.py approve --all --confidence high --by 张三
    python approve.py approve --ids ab12cd34ef56,0f9e8d7c6b5a --by 张三
    python approve.py refute --ids ab12cd34ef56 --reason "口径已变" --by 张三
    python approve.py stats

approve --all 只动 candidate（refuted 不会被批量复活，复活须 --ids 点名）；
refute 只能人为发起且必须给 --reason（自动路径永不置 refuted）。
写盘前自动备份 patterns.yaml.bak。
"""
import argparse
from collections import Counter

import pattern_store as ps
from constants import get_root

PAT = get_root() / "patterns" / "patterns.yaml"


def load_v2() -> dict:
    if not PAT.exists():
        raise SystemExit(f"{PAT} 不存在，先跑 patterns.py")
    doc = ps.load(PAT)
    if doc["meta"].get("schema_version", 1) < ps.SCHEMA_VERSION:
        raise SystemExit("patterns.yaml 还是 schema v1，先跑 python migrate_patterns.py")
    return doc


def match(p: dict, args) -> bool:
    return ((not getattr(args, "status", None) or p["status"] == args.status)
            and (not args.confidence or p["confidence"] == args.confidence)
            and (not args.type or p["type"] == args.type))


def pick_by_ids(doc: dict, ids: list[str]) -> list[dict]:
    idx = {p["id"]: p for p in doc["patterns"]}
    missing = [i for i in ids if i not in idx]
    if missing:
        raise SystemExit(f"找不到 id: {', '.join(missing)}（用 list 子命令查）")
    return [idx[i] for i in ids]


def cmd_list(doc: dict, args) -> None:
    n = 0
    for p in doc["patterns"]:
        if match(p, args):
            n += 1
            print(f"{p['id']}  [{p['status']:<9}] [{p['confidence']:<11}] "
                  f"{p['type']:<12} {p['key']} :: {p['claim']}")
    print(f"-- 共 {n} 条")


def cmd_approve(doc: dict, args) -> None:
    if args.ids:
        targets = pick_by_ids(doc, args.ids)
    elif args.all:
        targets = [p for p in doc["patterns"] if p["status"] == "candidate" and match(p, args)]
    else:
        raise SystemExit("approve 需要 --ids 或 --all")
    now = ps.now_iso()
    for p in targets:
        p["status"] = "approved"
        p["approved_by"] = args.by
        p["approved_at"] = now
        p["refuted_reason"] = None
    ps.save(PAT, doc, backup=True)
    print(f"已批准 {len(targets)} 条（by {args.by}）→ {PAT}")


def cmd_refute(doc: dict, args) -> None:
    targets = pick_by_ids(doc, args.ids)
    now = ps.now_iso()
    for p in targets:
        p["status"] = "refuted"
        p["refuted_reason"] = f"[{args.by} {now}] {args.reason}"
        p["approved_by"] = None
        p["approved_at"] = None
    ps.save(PAT, doc, backup=True)
    print(f"已否决 {len(targets)} 条（重算不会复活）→ {PAT}")


def cmd_stats(doc: dict, _args) -> None:
    pats = doc["patterns"]
    print(f"共 {len(pats)} 条（schema v{doc['meta']['schema_version']}）")
    for name, keyfn in (("status", lambda p: p["status"]),
                        ("confidence", lambda p: p["confidence"]),
                        ("type", lambda p: p["type"]),
                        ("status·confidence", lambda p: f"{p['status']}·{p['confidence']}")):
        c = Counter(keyfn(p) for p in pats)
        print(f"  {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())))


def csv_ids(s: str) -> list[str]:
    return [i.strip() for i in s.split(",") if i.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出规律（可按状态/置信度/类型过滤）")
    p.add_argument("--status", choices=ps.STATUSES)
    p.add_argument("--confidence", choices=ps.CONFIDENCES)
    p.add_argument("--type")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("approve", help="批准规律（--ids 点名，或 --all 批量批 candidate）")
    p.add_argument("--ids", type=csv_ids, default=None, help="逗号分隔的 pattern_id")
    p.add_argument("--all", action="store_true")
    p.add_argument("--confidence", choices=ps.CONFIDENCES, help="--all 时的过滤")
    p.add_argument("--type", help="--all 时的过滤")
    p.add_argument("--by", required=True, help="批准人")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("refute", help="否决规律（必须点名 --ids 且给 --reason）")
    p.add_argument("--ids", type=csv_ids, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", required=True, help="否决人")
    p.set_defaults(fn=cmd_refute)

    p = sub.add_parser("stats", help="状态/置信度/类型分布")
    p.set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    args.fn(load_v2(), args)


if __name__ == "__main__":
    main()
