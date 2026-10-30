"""构建上下文必须是白名单：本机目录里没进仓库的私有文件，不能被 `COPY . .` 打进镜像。

这里不起 Docker，只钉住 .dockerignore 的结构：第一条规则是 `*`（先排除一切），
之后放行的只有镜像运行真正需要的几项。想往镜像里多放东西，就得改这条测试并说明理由。
"""
from __future__ import annotations

ALLOWED_REINCLUDES = {"app", "requirements.txt", "eval", "examples"}


def _rules(project_root):
    text = (project_root / ".dockerignore").read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_dockerignore_is_an_allowlist(project_root):
    rules = _rules(project_root)
    assert rules[0] == "*", "第一条必须是 `*`：先排除一切，再放行"
    reincluded = {r[1:] for r in rules if r.startswith("!")}
    assert reincluded == ALLOWED_REINCLUDES, f"放行项变了: {sorted(reincluded)}"


def test_user_knowledge_and_private_dirs_never_reincluded(project_root):
    joined = "\n".join(_rules(project_root))
    for private in ("knowledge_base", "cashflow", "业务知识", "调研", ".wrangler", ".env", "user_token"):
        assert f"!{private}" not in joined


def test_dockerfile_only_copies_what_the_allowlist_provides(project_root):
    copies = [
        ln.split() for ln in (project_root / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if ln.strip().upper().startswith("COPY ")
    ]
    sources = {c[1] for c in copies}
    assert sources <= {"requirements.txt", "."}, sources


def test_compose_images_are_pinned_not_latest(project_root):
    """向量库服务端版本和 Python 客户端要对得上；浮动的 latest 会在某天悄悄升级后不兼容。"""
    text = (project_root / "docker-compose.yml").read_text(encoding="utf-8")
    images = [
        ln.split("image:", 1)[1].split("#")[0].strip()
        for ln in text.splitlines() if ln.strip().startswith("image:")
    ]
    qdrant = [i for i in images if i.startswith("qdrant/qdrant")]
    assert qdrant and all(":" in i and not i.endswith(":latest") for i in qdrant), qdrant
