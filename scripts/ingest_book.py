#!/usr/bin/env python
# ingest_book.py —— 把《深入理解 AI Agent》导入知识库
#
# 直接调底层组件（不经过 LLM / agent loop）：
#   DocumentLoader.load() → TextChunker.chunk() → MilvusStore.ingest()
#
# 用法：
#   python scripts/ingest_book.py <file_path> [--strategy recursive] [--size 800] [--overlap 150]
#
# 元数据：doc_title 自动从 Markdown 一级标题提取；tags 由 --tags 指定。
import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本能 import agentforge
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    parser = argparse.ArgumentParser(description="导入文档到 AgentForge 知识库")
    parser.add_argument("file_path", help="文档路径（md/pdf/docx/html）")
    parser.add_argument("--strategy", default="recursive",
                        choices=["fixed", "section", "recursive", "custom"],
                        help="切片策略（默认 recursive）")
    parser.add_argument("--size", type=int, default=800, help="切片大小（字符，默认 800）")
    parser.add_argument("--overlap", type=int, default=150, help="重叠（字符，默认 150）")
    parser.add_argument("--tags", default="", help="标签，逗号分隔（如 'AI Agent,学习资料'）")
    args = parser.parse_args()

    file_path = args.file_path
    if not Path(file_path).exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    # 1. 初始化知识库（Milvus + Embedding）
    from agentforge.knowledge import get_kb

    kb = get_kb()
    kb.setup({
        "milvus": {
            "enabled": True,
            "host": "localhost",
            "port": 19530,
            "collection": "agentforge_knowledge",
        },
        "embedding": {
            "server_url": "http://localhost:8002",
            "model_path": "/Users/yangkun/Desktop/Projects/models/bge-m3",
            "reranker_path": "/Users/yangkun/Desktop/Projects/models/bge-reranker-v2-m3",
            "dim": 1024,
        },
    })
    await kb.init()

    if not kb.milvus or not kb.milvus.available:
        print("❌ 知识库不可用（Milvus 未连上）")
        sys.exit(1)

    print(f"✅ 知识库就绪 | embedding 模式: {kb.embedding.mode}")
    print(f"   Milvus collection: {kb.milvus._collection}")

    # 2. 解析文档
    from agentforge.knowledge.loader import DocumentLoader
    loader = DocumentLoader()
    doc = loader.load(file_path)
    print(f"\n📄 文档解析完成:")
    print(f"   来源: {doc.source}")
    print(f"   标题: {doc.doc_title}")
    print(f"   全文: {len(doc.text)} 字符")
    print(f"   图片 URL: {len(doc.images)} 个")
    print(f"   章节: {len(doc.sections)} 个")

    # 3. 切片
    import uuid
    from agentforge.knowledge.chunker import TextChunker
    doc_id = str(uuid.uuid4())[:16]
    chunker = TextChunker()
    chunks = chunker.chunk(
        document=doc,
        doc_id=doc_id,
        strategy=args.strategy,
        chunk_size=args.size,
        overlap=args.overlap,
        tags=tags,
    )
    print(f"\n✂️  切片完成: {len(chunks)} 个（策略={args.strategy}, size={args.size}, overlap={args.overlap}）")
    if chunks:
        avg = sum(len(c.text) for c in chunks) / len(chunks)
        print(f"   平均切片长度: {avg:.0f} 字符")
        print(f"   样例 chunk[0]: {chunks[0].text[:80]}...")

    # 4. 存储前先查重（同 source 已导入则先删）
    existing = kb.milvus.get_doc_list()
    for e in existing:
        if e.get("source") == doc.source:
            print(f"\n⚠️  检测到同名文档已存在（doc_id={e['doc_id']}），先删除再重新导入...")
            kb.milvus.delete_doc(e["doc_id"])
            break

    # 5. 向量化 + 存储
    print(f"\n🔢 开始向量化 + 存储（{len(chunks)} 个切片）...")
    count = kb.milvus.ingest(chunks)
    print(f"\n{'='*50}")
    print(f"✅ 导入完成！")
    print(f"   doc_id: {doc_id}")
    print(f"   标题: {doc.doc_title}")
    print(f"   来源: {doc.source}")
    print(f"   切片数: {count}")
    print(f"   标签: {tags or '无'}")
    print(f"   图片 URL: {len(doc.images)} 个")
    print(f"{'='*50}")
    print(f"\n💡 检索命令：")
    print(f"   knowledge_search query='...' filter_source='{doc.source}'")


if __name__ == "__main__":
    asyncio.run(main())
