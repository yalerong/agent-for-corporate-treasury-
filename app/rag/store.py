"""双轨知识库的 Embedding / VectorStore / 检索器实现。

设计要点：
- 所有"重依赖"（HuggingFaceEmbeddings 间接依赖 torch + transformers）延迟到函数内导入，
  让测试可以跳过 HF 直接注入 fake embeddings
- QdrantClient 用 lru_cache 单例化，本地文件模式同一进程内不能开多个 client
- 加载/切块/索引拆为可注入的纯函数，便于测试和未来替换
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_qdrant import QdrantVectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams

from app.config import get_settings

Track = Literal["industry", "enterprise"]
SearchTrack = Literal["industry", "enterprise", "both"]


def get_embeddings() -> Embeddings:
    """返回 Embeddings 实例。延迟导入 langchain_huggingface 避免测试时强依赖。"""
    from langchain_huggingface import HuggingFaceEmbeddings

    s = get_settings()
    return HuggingFaceEmbeddings(
        model_name=s.embedding_model,
        model_kwargs={"device": s.embedding_device},
        encode_kwargs={"normalize_embeddings": True},
    )


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    """Qdrant 本地文件模式客户端单例。同一 path 在同一进程内不能开多个。"""
    s = get_settings()
    path_str = str(s.qdrant_path)
    if path_str in (":memory:", "memory"):
        return QdrantClient(location=":memory:")
    s.qdrant_path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=path_str)


def _collection_name(track: Track) -> str:
    s = get_settings()
    return s.industry_collection if track == "industry" else s.enterprise_collection


def _ensure_collection(client: QdrantClient, name: str, vector_size: int) -> None:
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


def get_store(
    track: Track,
    embeddings: Embeddings,
    client: QdrantClient | None = None,
    collection_name: str | None = None,
) -> QdrantVectorStore:
    """返回 track 对应的 QdrantVectorStore。集合不存在时自动建。

    embeddings / client / collection_name 都可注入，方便测试。
    """
    client = client or get_qdrant_client()
    name = collection_name or _collection_name(track)
    probe = embeddings.embed_query("__vector_size_probe__")
    _ensure_collection(client, name, len(probe))
    return QdrantVectorStore(client=client, collection_name=name, embedding=embeddings)


def load_directory(path: Path, category: str) -> list[Document]:
    """递归加载 path 下所有 .md / .txt 文件，构造 Document 并标注元数据。

    metadata:
      source: 相对 path.parent 的路径（便于回溯到原文件）
      category: "industry" 或 "enterprise"
      subcategory: 直接父目录名（如 "regulations"、"policies"）
    """
    docs: list[Document] = []
    if not path.exists():
        return docs
    for file in sorted(path.rglob("*")):
        if file.is_file() and file.suffix.lower() in (".md", ".txt"):
            text = file.read_text(encoding="utf-8")
            docs.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": str(file.relative_to(path.parent)).replace("\\", "/"),
                        "category": category,
                        "subcategory": file.parent.name,
                    },
                )
            )
    return docs


def chunk_documents(
    docs: Iterable[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> list[Document]:
    """中文友好的递归切分。优先按段落 / 句号 / 分号切，再按字符兜底。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    return splitter.split_documents(list(docs))


def index_to_store(
    store: QdrantVectorStore,
    path: Path,
    category: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> int:
    """加载 + 切块 + 写入 store。返回写入的 chunk 数量。"""
    raw = load_directory(path, category=category)
    chunks = chunk_documents(raw, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if not chunks:
        return 0
    store.add_documents(chunks)
    return len(chunks)


def index_track(track: Track) -> int:
    """便捷入口：用默认配置（HF embeddings + Qdrant 本地）索引一整个 track。"""
    s = get_settings()
    path = s.industry_kb_path if track == "industry" else s.enterprise_kb_path
    store = get_store(track, embeddings=get_embeddings())
    return index_to_store(store, path, category=track)


class DualTrackRetriever:
    """双轨检索器。strategy="both" 时并行查两个集合并分别返回。"""

    def __init__(
        self,
        industry_store: QdrantVectorStore,
        enterprise_store: QdrantVectorStore,
        k: int = 4,
    ) -> None:
        self.industry = industry_store
        self.enterprise = enterprise_store
        self.k = k

    def search(self, query: str, track: SearchTrack = "both") -> dict[str, list[Document]]:
        result: dict[str, list[Document]] = {}
        if track in ("industry", "both"):
            result["industry"] = self.industry.similarity_search(query, k=self.k)
        if track in ("enterprise", "both"):
            result["enterprise"] = self.enterprise.similarity_search(query, k=self.k)
        return result
