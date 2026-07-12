"""ChromaVectorStore：向量存储适配器（ChromaDB 嵌入式模式）。

数据合同来源：V3 架构文档 5.9 + 6.11。

设计：
  1. 使用 lazy import，chromadb 在运行时才导入
  2. 嵌入式模式，无独立服务进程
  3. collection: memory_vectors
  4. 元数据: {source, turn_id, created_at, track}
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional


class ChromaVectorStore:
    """ChromaDB 向量存储适配器。

    chromadb 在运行时 lazy import，测试时可通过注入 mock client。
    """

    def __init__(self, persist_path: str, collection_name: str = "memory_vectors"):
        self._persist_path = persist_path
        self._collection_name = collection_name
        self._client = None
        self._collection = None
        self._ensure_dir()

    def _ensure_dir(self):
        d = os.path.dirname(self._persist_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _get_client(self):
        """Lazy import chromadb 并创建客户端。"""
        if self._client is not None:
            return self._client
        import chromadb  # noqa: lazy import
        self._client = chromadb.PersistentClient(path=self._persist_path)
        return self._client

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        client = self._get_client()
        self._collection = client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    def upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """写入/更新向量。"""
        col = self._get_collection()
        col.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    def query(
        self,
        query_embedding: list[float],
        n_results: int = 10,
        where: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """向量检索。返回 {ids, documents, metadatas, distances}。"""
        col = self._get_collection()
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
        }
        if where is not None:
            kwargs["where"] = where
        return col.query(**kwargs)

    def count(self) -> int:
        """返回 collection 中的向量数量。"""
        col = self._get_collection()
        return col.count()

    def delete_all(self) -> None:
        """删除所有向量（测试用）。"""
        client = self._get_client()
        try:
            client.delete_collection(self._collection_name)
        except Exception:
            pass
        self._collection = None
        self._get_collection()
