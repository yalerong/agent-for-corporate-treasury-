"""自主权滑块：金额/关键词分档，engine 只对 forecast 行标注 review 档，**只标注不拦截**。

档位: auto_report（自动进报告）< flag_review（建议复核）< require_human（必须人工确认）。
真实阈值复制 policy.example.yaml 为 policy.yaml（已 gitignore）调整。
"""
import yaml
from constants import CODE_DIR, get_root

ROOT = get_root()
TIERS = ("auto_report", "flag_review", "require_human")


def load_policy() -> dict | None:
    for base in (ROOT, CODE_DIR):
        for name in ("policy.yaml", "policy.example.yaml"):
            p = base / name
            if p.exists():
                return yaml.safe_load(p.read_text(encoding="utf-8"))
    return None


def review_tier(amount: float, currency: str, text: str, policy: dict) -> str:
    """金额按币种阈值分档；text（主体/项目/收款方拼串）命中关键词直接 require_human。"""
    for kw in policy.get("keywords_require_human") or []:
        if kw in text:
            return "require_human"
    th = (policy.get("by_currency") or {}).get(currency) or policy["default"]
    if amount >= th["require_human"]:
        return "require_human"
    if amount >= th["flag_review"]:
        return "flag_review"
    return "auto_report"
