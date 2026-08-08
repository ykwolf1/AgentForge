# milvus_store.py 核心流程：Milvus 向量库（存储 + RRF 混合检索 + 元数据过滤 + chunk_id/doc_id）
#
#   Schema: dense(1024) + sparse + 15 个元数据字段
#   检索: hybrid (RRF) / dense / sparse 三种策略
#   过滤: source / tags / doc_id / section
#   精排: bge-reranker-v2-m3
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class MilvusStore:
    """Milvus 向量库：存储 + 混合检索（RRF）+ 元数据过滤 + chunk_id/doc_id 检索。"""

    def __init__(self, config: dict, embedding_engine=None):
        self._config = config
        self._host = config.get("host", "localhost")
        self._port = config.get("port", 19530)
        self._collection = config.get("collection", "agentforge_knowledge")
        self._enabled = config.get("enabled", False)
        self._healthy = False
        self._client = None
        self._embedding = embedding_engine

    async def health_check(self) -> bool:
        """检查 Milvus 是否可用。"""
        if not self._enabled:
            return False
        try:
            from pymilvus import MilvusClient
            self._client = MilvusClient(uri=f"http://{self._host}:{self._port}")
            cols = self._client.list_collections()
            self._healthy = True
            logger.info(f"[Infra/Milvus] 健康检查通过: {self._host}:{self._port}, collections={cols}")
            self._ensure_collection()
            return True
        except Exception as e:
            logger.warning(f"[Infra/Milvus] 不可用: {e}")
            self._healthy = False
            return False

    @property
    def available(self) -> bool:
        """实时检查连接是否活着——不只是看初始化时的状态。

        之前的问题：_healthy 只在 health_check 时设 True，之后再也不检查。
        如果 Milvus 服务端断开连接（空闲超时），_healthy 仍然是 True，
        导致后续所有操作都失败。
        """
        if not self._enabled or self._client is None:
            return False
        if not self._healthy:
            return False
        # 实时探测：尝试一个轻量操作，失败则尝试重连
        try:
            self._client.list_collections()
            return True
        except Exception:
            # 连接断了，尝试重连
            return self._reconnect()

    def _reconnect(self) -> bool:
        """自动重连——连接断开时尝试重新建立。"""
        try:
            from pymilvus import MilvusClient
            self._client = MilvusClient(uri=f"http://{self._host}:{self._port}")
            self._client.list_collections()  # 验证连接
            self._healthy = True
            logger.info(f"[Infra/Milvus] 自动重连成功: {self._host}:{self._port}")
            return True
        except Exception as e:
            logger.warning(f"[Infra/Milvus] 重连失败: {e}")
            self._healthy = False
            return False

    def _ensure_collection(self):
        """确保 collection 存在，不存在则创建。"""
        if not self.available:
            return
        if self._collection in self._client.list_collections():
            # collection 已存在，确保已 load
            try:
                self._client.load_collection(self._collection)
            except Exception:
                pass
            return

        # pymilvus 3.x：用 create_schema + prepare_index_params + create_collection 一次完成
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=1024)
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("doc_title", DataType.VARCHAR, max_length=256)
        schema.add_field("section", DataType.VARCHAR, max_length=256)
        schema.add_field("page", DataType.INT64)
        schema.add_field("images", DataType.JSON)
        schema.add_field("tags", DataType.JSON)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("total_chunks", DataType.INT64)
        schema.add_field("chunk_strategy", DataType.VARCHAR, max_length=32)
        schema.add_field("created_at", DataType.VARCHAR, max_length=64)

        # 索引参数（一次定义 dense + sparse）
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="dense_embedding",
            index_type="IVF_FLAT",
            metric_type="COSINE",
            params={"nlist": 128},
        )
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
        )

        self._client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )

        # 加载到内存
        self._client.load_collection(self._collection)
        logger.info(f"[Milvus] Collection '{self._collection}' 已创建（schema + 索引 + load）")

    def ingest(self, chunks: List["Chunk"]) -> int:
        """向量化 + 存储。返回切片数。"""
        if not self.available or not chunks:
            return 0

        # 1. 批量 embedding
        texts = [c.text for c in chunks]
        embed_result = self._embedding.encode(texts)
        dense_vecs = embed_result["dense_vecs"]
        sparse_weights = embed_result["lexical_weights"]

        # 2. 构造 entities
        now = datetime.now().isoformat()
        entities = []
        for i, chunk in enumerate(chunks):
            meta = chunk.metadata
            entities.append({
                "chunk_id": chunk.chunk_id,
                "doc_id": meta.get("doc_id", ""),
                "text": chunk.text[:8000],
                "dense_embedding": dense_vecs[i].tolist(),
                "sparse_embedding": sparse_weights[i],
                "source": meta.get("source", ""),
                "doc_title": meta.get("doc_title", ""),
                "section": meta.get("section", ""),
                "page": meta.get("page", 0),
                "images": meta.get("images", []),
                "tags": meta.get("tags", []),
                "chunk_index": meta.get("chunk_index", i),
                "total_chunks": meta.get("total_chunks", len(chunks)),
                "chunk_strategy": meta.get("chunk_strategy", ""),
                "created_at": now,
            })

        # 3. 插入
        self._client.insert(collection_name=self._collection, data=entities)
        self._client.flush(self._collection)
        # 加载 collection 到内存（搜索前必须 load）
        self._client.load_collection(self._collection)

        logger.info(f"[Milvus] 已导入 {len(entities)} 个切片到 '{self._collection}'")
        return len(entities)

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_expr: Optional[str] = None,
        strategy: str = "hybrid",
    ) -> List[dict]:
        """混合检索。

        Args:
            query: 搜索文本
            top_k: 返回数量
            filter_expr: Milvus 过滤表达式
            strategy: "hybrid"(RRF) / "dense" / "sparse"

        Returns: [{chunk_id, doc_id, text, source, section, images, tags, score}]
        """
        if not self.available:
            return []

        # 1. 编码查询
        query_embed = self._embedding.encode_query(query)
        dense_vec = query_embed["dense_vec"].tolist()
        sparse_weights = query_embed["sparse_weights"]

        # 2. 检索
        if strategy == "hybrid":
            raw_results = self._hybrid_search(dense_vec, sparse_weights, top_k * 4, filter_expr)
        elif strategy == "dense":
            raw_results = self._dense_search(dense_vec, top_k * 4, filter_expr)
        elif strategy == "sparse":
            raw_results = self._sparse_search(sparse_weights, top_k * 4, filter_expr)
        else:
            raw_results = self._hybrid_search(dense_vec, sparse_weights, top_k * 4, filter_expr)

        # 3. 精排（如果 reranker 可用，失败则降级到原始排序）
        if self._embedding and raw_results:
            try:
                documents = [r["text"] for r in raw_results]
                ranked = self._embedding.rerank(query, documents, top_k=top_k)
                results = []
                for idx, score in ranked:
                    r = raw_results[idx].copy()
                    r["rerank_score"] = score
                    results.append(r)
            except Exception as e:
                logger.warning(f"[Milvus] reranker 失败（降级到原始排序）: {e}")
                results = raw_results[:top_k]
        else:
            results = raw_results[:top_k]

        return results

    def _dense_search(self, dense_vec, limit, filter_expr) -> List[dict]:
        """纯 dense 向量检索。"""
        results = self._client.search(
            collection_name=self._collection,
            data=[dense_vec],
            anns_field="dense_embedding",
            search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=limit,
            filter=filter_expr or "",
            output_fields=["chunk_id", "doc_id", "text", "source", "doc_title",
                          "section", "page", "images", "tags", "chunk_index"],
        )
        return self._parse_search_results(results)

    def _sparse_search(self, sparse_weights, limit, filter_expr) -> List[dict]:
        """纯 sparse 向量检索。"""
        results = self._client.search(
            collection_name=self._collection,
            data=[sparse_weights],
            anns_field="sparse_embedding",
            search_params={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=limit,
            filter=filter_expr or "",
            output_fields=["chunk_id", "doc_id", "text", "source", "doc_title",
                          "section", "page", "images", "tags", "chunk_index"],
        )
        return self._parse_search_results(results)

    def _hybrid_search(self, dense_vec, sparse_weights, limit, filter_expr) -> List[dict]:
        """RRF 混合检索：dense + sparse → RRFRanker 融合。"""
        from pymilvus import AnnSearchRequest, RRFRanker

        dense_req = AnnSearchRequest(
            data=[dense_vec],
            anns_field="dense_embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=limit,
            expr=filter_expr or "",
        )
        sparse_req = AnnSearchRequest(
            data=[sparse_weights],
            anns_field="sparse_embedding",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.2}},
            limit=limit,
            expr=filter_expr or "",
        )

        results = self._client.hybrid_search(
            collection_name=self._collection,
            reqs=[dense_req, sparse_req],
            ranker=RRFRanker(k=60),
            limit=limit,
            output_fields=["chunk_id", "doc_id", "text", "source", "doc_title",
                          "section", "page", "images", "tags", "chunk_index"],
        )
        return self._parse_search_results(results)

    def _parse_search_results(self, raw_results) -> List[dict]:
        """解析 Milvus 搜索结果为统一格式。"""
        parsed = []
        if not raw_results:
            return parsed
        for hit_group in raw_results:
            if isinstance(hit_group, list):
                for hit in hit_group:
                    entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
                    distance = hit.get("distance", 0) if isinstance(hit, dict) else 0
                    parsed.append({
                        "chunk_id": entity.get("chunk_id", ""),
                        "doc_id": entity.get("doc_id", ""),
                        "text": entity.get("text", ""),
                        "source": entity.get("source", ""),
                        "doc_title": entity.get("doc_title", ""),
                        "section": entity.get("section", ""),
                        "page": entity.get("page", 0),
                        "images": entity.get("images", []),
                        "tags": entity.get("tags", []),
                        "chunk_index": entity.get("chunk_index", 0),
                        "score": float(distance),
                    })
            elif isinstance(hit_group, dict):
                entity = hit_group.get("entity", {})
                distance = hit_group.get("distance", 0)
                parsed.append({
                    "chunk_id": entity.get("chunk_id", ""),
                    "doc_id": entity.get("doc_id", ""),
                    "text": entity.get("text", ""),
                    "source": entity.get("source", ""),
                    "doc_title": entity.get("doc_title", ""),
                    "section": entity.get("section", ""),
                    "page": entity.get("page", 0),
                    "images": entity.get("images", []),
                    "tags": entity.get("tags", []),
                    "chunk_index": entity.get("chunk_index", 0),
                    "score": float(distance),
                })
        return parsed

    def get_by_chunk_id(self, chunk_id: str) -> Optional[dict]:
        """按 chunk_id 取单个切片。"""
        if not self.available:
            return None
        results = self._client.query(
            collection_name=self._collection,
            filter=f'chunk_id == "{chunk_id}"',
            output_fields=["chunk_id", "doc_id", "text", "source", "doc_title",
                          "section", "page", "images", "tags", "chunk_index"],
        )
        return results[0] if results else None

    def get_by_doc_id(self, doc_id: str) -> List[dict]:
        """取整篇文档的所有切片（按 chunk_index 排序）。"""
        if not self.available:
            return []
        results = self._client.query(
            collection_name=self._collection,
            filter=f'doc_id == "{doc_id}"',
            output_fields=["chunk_id", "doc_id", "text", "source", "doc_title",
                          "section", "page", "images", "tags", "chunk_index"],
            limit=1000,
        )
        # 按 chunk_index 排序
        return sorted(results, key=lambda x: x.get("chunk_index", 0))

    def get_doc_list(self) -> List[dict]:
        """列出所有文档（去重，返回 doc_id + doc_title + total_chunks）。"""
        if not self.available:
            return []
        results = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=["doc_id", "doc_title", "total_chunks", "created_at", "source"],
            limit=10000,
        )
        # 去重（按 doc_id）
        seen = set()
        docs = []
        for r in results:
            did = r.get("doc_id", "")
            if did and did not in seen:
                seen.add(did)
                docs.append({
                    "doc_id": did,
                    "doc_title": r.get("doc_title", ""),
                    "total_chunks": r.get("total_chunks", 0),
                    "source": r.get("source", ""),
                })
        return docs

    def delete_doc(self, doc_id: str) -> int:
        """删除整个文档的所有切片。"""
        if not self.available:
            return 0
        self._client.delete(
            collection_name=self._collection,
            filter=f'doc_id == "{doc_id}"',
        )
        logger.info(f"[Milvus] 已删除文档: {doc_id}")
        return 1
