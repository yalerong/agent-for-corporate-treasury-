"""真实 BGE 检索冒烟。"""
from app.rag.store import get_embeddings, get_store

emb = get_embeddings()
s_ind = get_store("industry", emb)
s_ent = get_store("enterprise", emb)

for query in ["跨境资金池监管要求", "公司内部调拨审批流程"]:
    print(f"\n=== query: {query} ===")
    print("[行业]")
    for d in s_ind.similarity_search(query, k=2):
        print(f"  - {d.metadata.get('source')}")
        print(f"    {d.page_content[:120]}...")
    print("[企业]")
    for d in s_ent.similarity_search(query, k=2):
        print(f"  - {d.metadata.get('source')}")
        print(f"    {d.page_content[:120]}...")
