# vector_store.py 核心流程：ChromaDB 向量记忆客户端
#
#   长期向量记忆（embedding + 语义检索），记忆系统第 6 层。
#   故障降级：ChromaDB 不可用时，recall 工具走 FTS5 关键词检索。
import hashlib
from typing import List, Optional

try:
    import chromadb
except Exception:
    chromadb = None

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class VectorStore:
    """ChromaDB 向量记忆：embedding + 语义检索（RAG）。

    存储时机：context_compact 前把原始 history 存入（自动 embedding）。
    检索时机：recall 工具优先查 ChromaDB（语义），fallback FTS5（关键词）。

    连不上时 available=False，调用方走 FTS5。
    """

    def __init__(self, config: dict):
        self._enabled = config.get("enabled", False)
        self._url = config.get("url", "http://localhost:8001")
        self._collection_name = config.get("collection", "agentforge_memory")
        self._healthy = False
        self._client = None
        self._collection = None

    async def health_check(self) -> bool:
        """检查 ChromaDB 是否可用。"""
        if not self._enabled or chromadb is None:
            return False
        try:
            self._client = chromadb.HttpClient(
                host=self._url.split("//")[1].split(":")[0] if "://" in self._url else "localhost",
                port=int(self._url.split(":")[-1]) if ":" in self._url else 8001,
            )
            # 建或取 collection（ChromaDB 默认用 all-MiniLM-L6-v2 embedding）
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"description": "AgentForge long-term memory"},
            )
            self._healthy = True
            logger.info(f"[Infra/ChromaDB] 健康检查通过: {self._url}, collection={self._collection_name}")
            return True
        except Exception as e:
            logger.warning(f"[Infra/ChromaDB] 不可用（降级到 FTS5）: {e}")
            self._healthy = False
            return False

    @property
    def available(self) -> bool:
        return self._enabled and self._healthy and self._collection is not None

    def store(self, text: str, metadata: Optional[dict] = None) -> bool:
        """存一段文本到向量库（自动 embedding）。"""
        if not self.available or not text.strip():
            return False
        try:
            doc_id = hashlib.md5(text.encode()).hexdigest()[:16]
            self._collection.add(
                ids=[doc_id],
                documents=[text],
                metadatas=[metadata or {}],
            )
            logger.debug(f"[VectorStore] 存储: {text[:50]}... → id={doc_id}")
            return True
        except Exception as e:
            logger.warning(f"[VectorStore] 存储失败: {e}")
            return False

    def store_batch(self, texts: List[str], metadatas: Optional[List[dict]] = None) -> int:
        """批量存储。"""
        if not self.available or not texts:
            return 0
        try:
            ids = [hashlib.md5(t.encode()).hexdigest()[:16] for t in texts]
            self._collection.add(
                ids=ids,
                documents=texts,
                metadatas=metadatas or [{} for _ in texts],
            )
            return len(ids)
        except Exception as e:
            logger.warning(f"[VectorStore] 批量存储失败: {e}")
            return 0

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """语义检索：按相关性返回最相似的 N 条。"""
        if not self.available or not query.strip():
            return []
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=top_k,
            )
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
            dists = results.get("distances", [[]])[0]
            return [
                {
                    "content": doc[:300],
                    "metadata": meta,
                    "similarity": round(1 - dist, 3),
                }
                for doc, meta, dist in zip(docs, metas, dists)
            ]
        except Exception as e:
            logger.warning(f"[VectorStore] 检索失败: {e}")
            return []
