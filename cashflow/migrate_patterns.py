"""patterns.yaml schema v1 → v2 一次性迁移（幂等，写前自动备份）。

v1 的 approved:true → status=approved（approved_by 记 v1-migration），false → candidate。
meta.schema_version>=2 时 no-op，连跑两次文件不变。备份写到 patterns.yaml.v1.bak。

用法: python migrate_patterns.py [--dry-run]
"""
import argparse
import shutil

import pattern_store as ps
from constants import get_root

PAT = get_root() / "patterns" / "patterns.yaml"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    args = ap.parse_args()

    if not PAT.exists():
        raise SystemExit(f"{PAT} 不存在，先跑 patterns.py")
    doc = ps.load(PAT)
    ver = doc["meta"].get("schema_version", 1)
    if ver >= ps.SCHEMA_VERSION:
        print(f"已是 schema v{ver}，无需迁移（幂等 no-op）")
        return

    default_from = (doc["meta"].get("data_range") or [None, None])[1]
    now = ps.now_iso()
    pats, n_approved = [], 0
    for p in doc["patterns"]:
        was_approved = bool(p.get("approved"))
        q = ps.upgrade_pattern(p)
        if not q.get("valid_from"):
            q["valid_from"] = default_from
        if was_approved:
            q["approved_by"] = q["approved_by"] or "v1-migration"
            q["approved_at"] = q["approved_at"] or now
            n_approved += 1
        pats.append(q)

    if args.dry_run:
        print(f"[dry-run] 将迁移 {len(pats)} 条（其中 v1 已批准 {n_approved} 条），不写盘")
        return
    bak = PAT.with_name("patterns.yaml.v1.bak")
    shutil.copy2(PAT, bak)
    ps.save(PAT, {"meta": doc["meta"], "patterns": pats})
    print(f"迁移完成: {len(pats)} 条 → schema v2（approved {n_approved}）；v1 备份 → {bak}")


if __name__ == "__main__":
    main()
