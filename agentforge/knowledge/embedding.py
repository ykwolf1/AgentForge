# embedding.py 核心流程：bge-m3 embedding + bge-reranker 精排
#
#   两种模式：
#     HTTP 模式（推荐）：通过 HTTP 调 embedding_server（不在进程内加载模型，省 2GB 内存）
#     本地模式（降级）：进程内加载 bge-m3（首次 encode 时加载）
#
#   优先级：配置了 server_url → HTTP 模式；否则用 model_path → 本地模式
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class EmbeddingEngine:
    """Embedding 引擎：HTTP 优先，降级本地。"""

    def __init__(
        self,
        model_path: Optional[str] = None,
        reranker_path: Optional[str] = None,
        server_url: Optional[str] = None,
    ):
        self._model_path = model_path
        self._reranker_path = reranker_path
        self._server_url = server_url.rstrip("/") if server_url else None
        self._dim = 1024

        # HTTP 模式
        self._use_http = self._server_url is not None

        # 本地模式（延迟加载）
        self._model = None
        self._reranker = None

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def mode(self) -> str:
        return "http" if self._use_http else "local"

    async def health_check(self) -> bool:
        """检查 embedding 服务是否可用（仅 HTTP 模式）。"""
        if not self._use_http:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._server_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        logger.info(f"[Embedding] HTTP 服务可用: {self._server_url}")
                        return True
        except Exception as e:
            logger.warning(f"[Embedding] HTTP 服务不可用（降级到本地）: {e}")
            self._use_http = False
        return False

    def encode(self, texts: List[str], batch_size: int = 12) -> Dict[str, Any]:
        """编码文本 → {dense_vecs, lexical_weights}。

        HTTP 模式用同步 requests（简单可靠）；本地模式用 FlagEmbedding。
        """
        if self._use_http:
            return self._encode_http(texts)
        return self._encode_local(texts, batch_size)

    def encode_query(self, query: str) -> Dict[str, Any]:
        """编码查询文本（单个）。"""
        result = self.encode([query])
        return {
            "dense_vec": result["dense_vecs"][0],
            "sparse_weights": result["lexical_weights"][0],
        }

    def rerank(self, query: str, documents: List[str], top_k: int = 5) -> List[tuple]:
        """精排。返回 [(index, score), ...]。"""
        if self._use_http and self._server_url:
            return self._rerank_http(query, documents, top_k)
        return self._rerank_local(query, documents, top_k)

    # ===== HTTP 模式 =====
    def _encode_http(self, texts: List[str]) -> Dict[str, Any]:
        """分批编码，避免大批量请求超时。"""
        import numpy as np
        BATCH = 32  # 每批 32 条（bge-m3 在 M 系列上约 5-8 秒/批）
        all_dense, all_sparse = [], []
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            part = self._encode_http_batch(batch)
            all_dense.extend(part["dense_vecs"])
            all_sparse.extend(part["lexical_weights"])
        return {
            "dense_vecs": np.array(all_dense),
            "lexical_weights": all_sparse,
        }

    def _encode_http_batch(self, texts: List[str]) -> Dict[str, Any]:
        import requests
        resp = requests.post(
            f"{self._server_url}/encode",
            json={"texts": texts, "return_dense": True, "return_sparse": True},
            timeout=120,
        )
        data = resp.json()
        # sparse_weights: List[{str(token_id): weight}] → List[{int(token_id): weight}]
        import numpy as np
        sparse = []
        for w in data.get("sparse_weights", []):
            sparse.append({int(k): float(v) for k, v in w.items()})
        return {
            "dense_vecs": np.array(data.get("dense_vecs", [])),
            "lexical_weights": sparse,
        }

    def _rerank_http(self, query: str, documents: List[str], top_k: int) -> List[tuple]:
        import requests
        resp = requests.post(
            f"{self._server_url}/rerank",
            json={"query": query, "documents": documents, "top_k": top_k},
            timeout=60,
        )
        data = resp.json()
        return list(zip(data["indices"], data["scores"]))

    # ===== 本地模式（降级）=====
    def _ensure_model(self):
        if self._model is not None:
            return
        if not self._model_path:
            raise RuntimeError("Embedding 本地模式需要 model_path")
        from FlagEmbedding import BGEM3FlagModel
        self._model = BGEM3FlagModel(self._model_path, use_fp16=True)
        logger.info(f"[Embedding] 本地 bge-m3 已加载: {self._model_path}")

    def _ensure_reranker(self):
        if self._reranker is not None or self._reranker_path is None:
            return
        # FlagEmbedding 的 FlagReranker 在 transformers 5.x 下报错
        # (prepare_for_model 被移除)，改用 sentence-transformers 的 CrossEncoder
        from sentence_transformers import CrossEncoder
        self._reranker = CrossEncoder(self._reranker_path, max_length=512)
        logger.info(f"[Embedding] 本地 reranker 已加载 (CrossEncoder): {self._reranker_path}")

    def _encode_local(self, texts: List[str], batch_size: int) -> Dict[str, Any]:
        self._ensure_model()
        result = self._model.encode(
            texts,
            batch_size=batch_size,
            max_length=8192,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        return {
            "dense_vecs": result["dense_vecs"],
            "lexical_weights": result["lexical_weights"],
        }

    def _rerank_local(self, query: str, documents: List[str], top_k: int) -> List[tuple]:
        self._ensure_reranker()
        if self._reranker is None:
            return [(i, 0.0) for i in range(min(top_k, len(documents)))]
        pairs = [[query, doc] for doc in documents]
        import numpy as np
        logits = self._reranker.predict(pairs)
        logits = np.atleast_1d(logits)
        scores = 1.0 / (1.0 + np.exp(-logits))  # sigmoid 归一化
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        return ranked
