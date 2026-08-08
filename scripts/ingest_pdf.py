#!/usr/bin/env python
# ingest_pdf.py —— 导入 PDF/MD 到指定知识库（collection）
#
# 直接调底层组件（不经过 LLM / agent loop）：
#   DocumentLoader.load() → TextChunker.chunk() → MilvusStore.ingest()
#
# 用法：
#   python scripts/ingest_pdf.py <file_path> --collection <名称> [--strategy recursive] [--size 800] [--overlap 150] [--tags "标签1,标签2"]
#
# 示例：
#   python scripts/ingest_pdf.py "书.pdf" --collection kb_architecture --tags "架构,设计思维"
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    parser = argparse.ArgumentParser(description="导入文档到指定知识库")
    parser.add_argument("file_path", help="文档路径（md/pdf/docx/html）")
    parser.add_argument("--collection", required=True,
                        help="Milvus collection 名（知识库）")
    parser.add_argument("--strategy", default="recursive",
                        choices=["fixed", "section", "recursive", "custom"])
    parser.add_argument("--size", type=int, default=800, help="切片大小（默认 800）")
    parser.add_argument("--overlap", type=int, default=150, help="重叠（默认 150）")
    parser.add_argument("--tags", default="", help="标签，逗号分隔")
    args = parser.parse_args()

    file_path = args.file_path
    if not Path(file_path).exists():
        print(f"❌ 文件不存在: {file_path}")
        sys.exit(1)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    # 1. 初始化知识库（指定 collection）
    from agentforge.knowledge import get_kb
    kb = get_kb()
    kb.setup({
        "milvus": {
            "enabled": True,
            "host": "localhost",
            "port": 19530,
            "collection": args.collection,
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
        print("❌ 知识库不可用")
        sys.exit(1)

    print(f"✅ 知识库就绪 | collection: {kb.milvus._collection}")

    # 2. 解析文档
    from agentforge.knowledge.loader import DocumentLoader
    loader = DocumentLoader()
    doc = loader.load(file_path)
    print(f"\n📄 文档解析完成:")
    print(f"   来源: {doc.source}")
    print(f"   标题: {doc.doc_title}")
    print(f"   全文: {len(doc.text)} 字符")
    print(f"   图片 URL: {len(doc.images)} 个")

    # 空文本检查（扫描版 PDF）
    if len(doc.text.strip()) < 100:
        print(f"\n❌ 文本内容极少（{len(doc.text.strip())} 字符），可能是扫描版 PDF，无法入库")
        sys.exit(1)

    # 3. 切片
    import uuid
    from agentforge.knowledge.chunker import TextChunker
    doc_id = str(uuid.uuid4())[:16]
    chunker = TextChunker()
    chunks = chunker.chunk(
        document=doc, doc_id=doc_id,
        strategy=args.strategy, chunk_size=args.size,
        overlap=args.overlap, tags=tags,
    )
    avg = sum(len(c.text) for c in chunks) / max(len(chunks), 1)
    print(f"\n✂️  切片: {len(chunks)} 个 | 平均 {avg:.0f} 字符/片")

    # 4. 查重（同 source 先删）
    for e in kb.milvus.get_doc_list():
        if e.get("source") == doc.source:
            print(f"⚠️  同名文档已存在，先删除旧版...")
            kb.milvus.delete_doc(e["doc_id"])
            break

    # 5. 入库
    print(f"\n🔢 向量化 + 存储中...")
    count = kb.milvus.ingest(chunks)
    print(f"\n{'='*50}")
    print(f"✅ 导入完成 | collection: {args.collection}")
    print(f"   doc_id : {doc_id}")
    print(f"   标题   : {doc.doc_title}")
    print(f"   切片数 : {count}")
    print(f"   标签   : {tags or '无'}")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
