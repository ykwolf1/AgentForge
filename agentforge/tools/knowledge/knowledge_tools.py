# knowledge_tools.py 核心流程：知识库导入 + 检索工具
#
#   ingest_document: 文档 → 解析 → 切片 → embedding → Milvus 存储
#   knowledge_search: 混合检索（RRF）+ 元数据过滤 + chunk_id/doc_id 查询
import json
import uuid
from typing import Any, List, Mapping

from agentforge.llm.llm_basics import ToolCallResult
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.tools.tool_manager import register_tool


def _get_kb():
    """获取全局知识库客户端（由 InfraManager 启动时注入）。"""
    from agentforge.knowledge import _kb_instance
    return _kb_instance


@register_tool(name="ingest_document", providers="*")
class IngestDocumentTool(BaseTool):
    """导入文档到知识库（自动解析 + 切片 + 向量化 + 存储）。"""

    name = "ingest_document"
    display_name = "Ingest Document"
    description = (
        "Ingest a document (md/pdf/docx/html) into the knowledge base for future retrieval. "
        "The document will be parsed, chunked, embedded, and stored in the vector database. "
        "Images URLs in the document will be preserved in metadata."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the document file"
            },
            "chunk_strategy": {
                "type": "string",
                "enum": ["fixed", "section", "recursive", "custom"],
                "default": "recursive",
                "description": "Chunking strategy"
            },
            "chunk_size": {
                "type": "integer",
                "default": 500,
                "description": "Chunk size in characters"
            },
            "overlap": {
                "type": "integer",
                "default": 100,
                "description": "Overlap between chunks (for fixed/recursive)"
            },
            "separators": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Custom separators for 'custom' strategy (default: ['\\n\\n'])"
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Custom tags for the document"
            }
        },
        "required": ["file_path"]
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        file_path = kwargs.get("file_path", "")
        strategy = kwargs.get("chunk_strategy", "recursive")
        chunk_size = kwargs.get("chunk_size", 500)
        overlap = kwargs.get("overlap", 100)
        separators = kwargs.get("separators")
        tags = kwargs.get("tags")
        call_id = kwargs.get("call_id", "")

        if not file_path:
            return ToolCallResult(call_id=call_id, error="file_path is required")

        kb = _get_kb()
        if kb is None or not kb.milvus or not kb.milvus.available:
            return ToolCallResult(
                call_id=call_id,
                error="知识库不可用（Milvus 未初始化或不可连接）"
            )

        try:
            # 1. 解析文档
            from agentforge.knowledge.loader import DocumentLoader
            loader = DocumentLoader()
            doc = loader.load(file_path)

            # 2. 切片
            from agentforge.knowledge.chunker import TextChunker
            doc_id = str(uuid.uuid4())[:16]
            chunker = TextChunker()
            chunks = chunker.chunk(
                document=doc,
                doc_id=doc_id,
                strategy=strategy,
                chunk_size=chunk_size,
                overlap=overlap,
                separators=separators,
                tags=tags,
            )

            if not chunks:
                return ToolCallResult(
                    call_id=call_id,
                    error=f"文档解析后无内容: {file_path}"
                )

            # 3. 向量化 + 存储
            count = kb.milvus.ingest(chunks)

            return ToolCallResult(
                call_id=call_id,
                result=(
                    f"✅ 文档已导入知识库\n"
                    f"  文件: {doc.source}\n"
                    f"  标题: {doc.doc_title}\n"
                    f"  doc_id: {doc_id}\n"
                    f"  切片数: {count}（策略: {strategy}）\n"
                    f"  图片 URL: {len(doc.images)} 个\n"
                    f"  标签: {tags or '无'}\n"
                    f"用 knowledge_search 工具检索，或用 doc_id={doc_id} 取整篇文档。"
                ),
            )
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"导入失败: {e}")


@register_tool(name="knowledge_search", providers="*")
class KnowledgeSearchTool(BaseTool):
    """语义检索知识库（混合检索 + 元数据过滤）。"""

    name = "knowledge_search"
    display_name = "Knowledge Search"
    description = (
        "Search the knowledge base using hybrid retrieval (dense + sparse with RRF). "
        "Supports metadata filtering and reranking. "
        "Returns text chunks with source info and image URLs."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Semantic search query"
            },
            "top_k": {
                "type": "integer",
                "default": 5,
                "description": "Max results to return"
            },
            "filter_source": {
                "type": "string",
                "description": "Filter by source filename (e.g. 'report.pdf')"
            },
            "filter_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by tags (e.g. ['市场','2024'])"
            },
            "strategy": {
                "type": "string",
                "enum": ["hybrid", "dense", "sparse"],
                "default": "hybrid",
                "description": "Retrieval strategy"
            },
            "doc_id": {
                "type": "string",
                "description": "Search within a specific document (by doc_id)"
            },
            "get_full_doc": {
                "type": "boolean",
                "default": False,
                "description": "Return entire document instead of chunks (requires doc_id)"
            }
        },
        "required": ["query"]
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 5)
        filter_source = kwargs.get("filter_source")
        filter_tags = kwargs.get("filter_tags")
        strategy = kwargs.get("strategy", "hybrid")
        doc_id = kwargs.get("doc_id")
        get_full_doc = kwargs.get("get_full_doc", False)
        call_id = kwargs.get("call_id", "")

        if not query and not (get_full_doc and doc_id):
            return ToolCallResult(call_id=call_id, error="query 或 (get_full_doc + doc_id) 至少需要一项")

        kb = _get_kb()
        if kb is None or not kb.milvus or not kb.milvus.available:
            return ToolCallResult(
                call_id=call_id,
                error="知识库不可用（Milvus 未初始化或不可连接）"
            )

        try:
            # 整文档检索
            if get_full_doc and doc_id:
                chunks = kb.milvus.get_by_doc_id(doc_id)
                if not chunks:
                    return ToolCallResult(call_id=call_id, result=f"文档 {doc_id} 不存在或无内容")
                parts = [f"文档完整内容（{len(chunks)} 个切片）:\n"]
                for c in chunks:
                    section = c.get("section", "")
                    parts.append(f"\n--- [{c.get('chunk_index', 0)}] {section} ---\n{c.get('text', '')}")
                if chunks[0].get("images"):
                    parts.append(f"\n\n图片 URL: {chunks[0]['images']}")
                return ToolCallResult(call_id=call_id, result="".join(parts))

            # 构造过滤表达式
            filter_parts = []
            if filter_source:
                filter_parts.append(f'source == "{filter_source}"')
            if doc_id:
                filter_parts.append(f'doc_id == "{doc_id}"')
            filter_expr = " AND ".join(filter_parts) if filter_parts else None

            # 标签过滤
            # Milvus 3.x 的 JSON_CONTAINS 对字符串数组元素匹配不生效，用 array_contains
            if filter_tags:
                tag_filters = []
                for tag in filter_tags:
                    tag_filters.append(f'array_contains(tags, "{tag}")')
                tag_expr = " AND ".join(tag_filters)
                filter_expr = f"{filter_expr} AND {tag_expr}" if filter_expr else tag_expr

            # 检索
            results = kb.milvus.search(
                query=query,
                top_k=top_k,
                filter_expr=filter_expr,
                strategy=strategy,
            )

            if not results:
                return ToolCallResult(call_id=call_id, result=f"未找到与 '{query}' 相关的内容")

            # 格式化结果
            parts = [f"找到 {len(results)} 条结果:\n"]
            for i, r in enumerate(results, 1):
                parts.append(f"\n--- 结果 {i} (score={r.get('score', 0):.3f}) ---")
                parts.append(f"来源: {r.get('source', '')} | 章节: {r.get('section', '')}")
                if r.get("doc_id"):
                    parts.append(f"doc_id: {r['doc_id']} | chunk_id: {r.get('chunk_id', '')}")
                parts.append(f"\n{r.get('text', '')}")
                if r.get("images"):
                    parts.append(f"\n📎 相关图片: {', '.join(r['images'][:3])}")

            return ToolCallResult(call_id=call_id, result="".join(parts))

        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"检索失败: {e}")
