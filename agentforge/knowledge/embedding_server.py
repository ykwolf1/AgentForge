# embedding_server.py 核心流程：bge-m3 独立 HTTP 服务
#
#   把 bge-m3 + bge-reranker 包装成独立 HTTP 服务。
#   Agent 不在进程内加载模型（省 2GB 内存），通过 HTTP 调用。
#
#   启动方式：
#     python -m agentforge.knowledge.embedding_server
#       --model /Users/yangkun/Desktop/Projects/models/bge-m3
#       --reranker /Users/yangkun/Desktop/Projects/models/bge-reranker-v2-m3
#       --port 8002
#
#   或 Docker 部署（推荐生产环境）。
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


# ===== 请求/响应模型 =====
class EncodeRequest(BaseModel):
    texts: List[str]
    return_dense: bool = True
    return_sparse: bool = True

class EncodeResponse(BaseModel):
    dense_vecs: Optional[List[List[float]]] = None
    sparse_weights: Optional[List[dict]] = None

class RerankRequest(BaseModel):
    query: str
    documents: List[str]
    top_k: int = 5

class RerankResponse(BaseModel):
    indices: List[int]
    scores: List[float]

class HealthResponse(BaseModel):
    status: str
    model: str
    reranker: Optional[str] = None


# ===== 模型加载 =====
_model = None
_reranker = None
_model_path = ""
_reranker_path = None

def load_models(model_path: str, reranker_path: Optional[str] = None):
    global _model, _reranker, _model_path, _reranker_path
    _model_path = model_path
    _reranker_path = reranker_path

    from FlagEmbedding import BGEM3FlagModel
    _model = BGEM3FlagModel(model_path, use_fp16=True)
    logger.info(f"[EmbeddingServer] bge-m3 已加载: {model_path}")

    if reranker_path:
        try:
            # FlagEmbedding 的 FlagReranker 在 transformers 5.x 下会报
            # 'XLMRobertaTokenizer has no attribute prepare_for_model'。
            # 改用 sentence-transformers 的 CrossEncoder 加载同一个 bge-reranker，
            # 行为等价（同样的模型权重），且兼容 transformers 5.x。
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder(reranker_path, max_length=512)
            logger.info(f"[EmbeddingServer] reranker 已加载 (CrossEncoder): {reranker_path}")
        except Exception as e:
            logger.warning(f"[EmbeddingServer] reranker 加载失败（降级）: {e}")


# ===== FastAPI =====
app = FastAPI(title="AgentForge Embedding Server", version="1.0.0")


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        model=_model_path,
        reranker=_reranker_path,
    )


@app.post("/encode", response_model=EncodeResponse)
def encode(req: EncodeRequest):
    """批量编码文本 → dense + sparse 向量。"""
    result = _model.encode(
        req.texts,
        batch_size=12,
        max_length=8192,
        return_dense=req.return_dense,
        return_sparse=req.return_sparse,
        return_colbert_vecs=False,
    )
    dense = None
    sparse = None
    if req.return_dense:
        dense = result["dense_vecs"].tolist()
    if req.return_sparse:
        sparse = []
        for w in result["lexical_weights"]:
            # {token_id(int): weight(float)} → {str(key): float(val)}
            sparse.append({str(k): float(v) for k, v in w.items()})
    return EncodeResponse(dense_vecs=dense, sparse_weights=sparse)


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    """精排。返回 top_k 文档的索引和分数（sigmoid 归一化到 0-1）。"""
    if _reranker is None or not req.documents:
        indices = list(range(min(req.top_k, len(req.documents))))
        return RerankResponse(indices=indices, scores=[0.0] * len(indices))

    pairs = [[req.query, doc] for doc in req.documents]
    # CrossEncoder.predict 返回原始 logits，用 sigmoid 归一化（对齐 FlagReranker 的 normalize=True）
    import numpy as np
    logits = _reranker.predict(pairs)
    logits = np.atleast_1d(logits)
    scores = 1.0 / (1.0 + np.exp(-logits))

    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:req.top_k]
    return RerankResponse(
        indices=[idx for idx, _ in ranked],
        scores=[float(score) for _, score in ranked],
    )


def main():
    parser = argparse.ArgumentParser(description="AgentForge Embedding Server")
    parser.add_argument("--model", type=str, required=True, help="bge-m3 模型路径")
    parser.add_argument("--reranker", type=str, default=None, help="bge-reranker 模型路径")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    load_models(args.model, args.reranker)

    import uvicorn
    logger.info(f"[EmbeddingServer] 启动: {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
