"""双轨知识库 RAG 模块。

公开 API：
- get_embeddings()         返回 LangChain Embeddings 实例（默认 HuggingFace bge）
- get_qdrant_client()      返回 Qdrant 客户端单例
- get_store(track, ...)    返回对应 track 的 QdrantVectorStore
- load_directory(path, category) / chunk_documents(...)  纯函数式加载与切块
- index_to_store(store, ...)                             指定 store 索引（测试可注入 fake）
- index_track(track)                                     默认配置下索引整个 track
- DualTrackRetriever       双轨检索器
"""
from app.rag.store import (
    DualTrackRetriever,
    chunk_documents,
    get_embeddings,
    get_qdrant_client,
    get_store,
    index_to_store,
    index_track,
    load_directory,
)

__all__ = [
    "get_embeddings",
    "get_qdrant_client",
    "get_store",
    "load_directory",
    "chunk_documents",
    "index_to_store",
    "index_track",
    "DualTrackRetriever",
]
