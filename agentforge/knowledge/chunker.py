# chunker.py 核心流程：文档切片器（4 种策略）
#
#   固定切分 / 章节切分 / 递归切分 / 自定义分隔符切分
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from .loader import Document

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    """一个文档切片。"""
    chunk_id: str                           # 唯一 ID（UUID）
    text: str                               # 切片文本
    metadata: dict = field(default_factory=dict)
    # metadata: {doc_id, source, doc_title, section, page, images,
    #            chunk_index, total_chunks, chunk_strategy, tags}

    @classmethod
    def create(cls, text: str, **metadata) -> "Chunk":
        return cls(
            chunk_id=str(uuid.uuid4())[:16],
            text=text,
            metadata=metadata,
        )


class TextChunker:
    """文档切片器。4 种策略。"""

    def chunk(
        self,
        document: Document,
        doc_id: str,
        strategy: str = "recursive",
        chunk_size: int = 500,
        overlap: int = 100,
        separators: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> List[Chunk]:
        """统一入口。返回带 metadata 的 Chunk 列表。"""
        if strategy == "fixed":
            chunks = self.chunk_fixed(document.text, chunk_size, overlap)
        elif strategy == "section":
            chunks = self.chunk_by_section(document)
        elif strategy == "recursive":
            chunks = self.chunk_recursive(document.text, chunk_size, overlap)
        elif strategy == "custom":
            seps = separators or ["\n\n"]
            chunks = self.chunk_by_separator(document.text, seps, chunk_size)
        else:
            raise ValueError(f"未知切片策略: {strategy}")

        # 给每个 chunk 补充完整 metadata
        for i, c in enumerate(chunks):
            c.metadata.update({
                "doc_id": doc_id,
                "source": document.source,
                "doc_title": document.doc_title,
                "images": document.images,
                "tags": tags or [],
                "chunk_index": i,
                "total_chunks": len(chunks),
                "chunk_strategy": strategy,
            })
            # 回填 section：recursive/fixed/custom 策略本身不保留标题，
            # 用 chunk 首句去匹配 loader 已解析的 sections，补上所属章节
            if not c.metadata.get("section") and document.sections:
                c.metadata["section"] = self._locate_section(c.text, document.sections)
            # 从 pages 字典推算页码（如果有的话）
            if document.pages and not c.metadata.get("page"):
                c.metadata["page"] = self._guess_page(c.text, document.pages)

        logger.info(f"[Chunker] 切片完成: {len(chunks)} 个 ({strategy}, doc={document.source})")
        return chunks

    # ===== 策略 1：固定字数切分 =====
    def chunk_fixed(self, text: str, size: int = 500, overlap: int = 100) -> List[Chunk]:
        chunks = []
        start = 0
        while start < len(text):
            end = start + size
            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(Chunk.create(chunk_text, section=""))
            start = end - overlap
        return chunks

    # ===== 策略 2：按章节切分 =====
    def chunk_by_section(self, document: Document) -> List[Chunk]:
        if document.sections:
            # 用 loader 已经解析好的 sections
            return [
                Chunk.create(
                    text=s["text"] or s["title"],
                    section=s["title"],
                )
                for s in document.sections
                if s["text"] or s["title"]
            ]
        # 没有结构化 section → 用标题正则解析
        return self._split_by_headings(document.text)

    def _split_by_headings(self, text: str) -> List[Chunk]:
        chunks = []
        current_title = ""
        current_lines = []
        for line in text.split("\n"):
            if re.match(r'^#{1,3}\s+', line):
                if current_title or current_lines:
                    chunks.append(Chunk.create(
                        text="\n".join(current_lines).strip(),
                        section=current_title,
                    ))
                current_title = re.sub(r'^#{1,3}\s+', '', line).strip()
                current_lines = []
            else:
                current_lines.append(line)
        if current_title or current_lines:
            chunks.append(Chunk.create(
                text="\n".join(current_lines).strip(),
                section=current_title,
            ))
        return chunks

    # ===== 策略 3：递归切分 =====
    def chunk_recursive(self, text: str, max_size: int = 500, min_size: int = 100) -> List[Chunk]:
        """先按段落 → 超长按句子 → 超长按固定。"""
        # 分隔符优先级：段落 → 换行 → 句号 → 固定
        result = []
        self._recursive_split(text, max_size, min_size, result, "")
        return result

    def _recursive_split(self, text: str, max_size: int, min_size: int,
                         result: List[Chunk], section: str):
        text = text.strip()
        if not text:
            return
        if len(text) <= max_size:
            result.append(Chunk.create(text, section=section))
            return

        # 尝试按段落分
        paragraphs = text.split("\n\n")
        if len(paragraphs) > 1:
            buf = ""
            for para in paragraphs:
                if len(buf) + len(para) <= max_size:
                    buf += para + "\n\n"
                else:
                    if buf:
                        result.append(Chunk.create(buf.strip(), section=section))
                    if len(para) > max_size:
                        self._recursive_split(para, max_size, min_size, result, section)
                    else:
                        buf = para + "\n\n"
            if buf.strip():
                result.append(Chunk.create(buf.strip(), section=section))
            return

        # 尝试按句子分
        sentences = re.split(r'(?<=[。！？.!?\n])\s+', text)
        if len(sentences) > 1:
            buf = ""
            for sent in sentences:
                if len(buf) + len(sent) <= max_size:
                    buf += sent
                else:
                    if buf:
                        result.append(Chunk.create(buf.strip(), section=section))
                    if len(sent) > max_size:
                        # 句子太长 → 固定切
                        for i in range(0, len(sent), max_size):
                            result.append(Chunk.create(sent[i:i+max_size], section=section))
                        buf = ""
                    else:
                        buf = sent
            if buf.strip():
                result.append(Chunk.create(buf.strip(), section=section))
            return

        # 兜底：固定切
        for i in range(0, len(text), max_size):
            result.append(Chunk.create(text[i:i+max_size], section=section))

    # ===== 策略 4：自定义分隔符切分 =====
    def chunk_by_separator(
        self, text: str, separators: List[str], max_size: int = 500
    ) -> List[Chunk]:
        """按用户指定的分隔符（如 \\n\\n）切分。
        如果某段仍超过 max_size，递归再切。"""
        # 按第一个分隔符切
        chunks = []
        remaining = text
        for sep in separators:
            parts = remaining.split(sep)
            remaining = ""
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if len(part) > max_size:
                    # 超长 → 递归切
                    sub = []
                    self._recursive_split(part, max_size, 100, sub, "")
                    chunks.extend(sub)
                else:
                    chunks.append(Chunk.create(part, section=""))
            break  # 只用第一个匹配的分隔符

        if not chunks:
            # 没切出来 → 固定切
            chunks = self.chunk_fixed(text, max_size, 50)

        return chunks

    # ===== 辅助 =====
    def _locate_section(self, chunk_text: str, sections: List[dict]) -> str:
        """定位 chunk 属于哪个章节。

        sections 是 loader 解析好的 [{title, text}]，按出现顺序排列。
        用 chunk 的前若干句在 section.text 中查找，返回命中的最后一个 section 的 title
        （因为 chunk 可能横跨边界，取它开头所属的章节）。
        """
        if not sections or not chunk_text:
            return ""
        # 取 chunk 前 40 个字符做指纹（足够区分，又不会因为切片边界失配）
        fingerprint = chunk_text.strip()[:40]
        for s in sections:
            stext = s.get("text", "")
            if not stext:
                continue
            # chunk 开头出现在该 section 正文中 → 属于这个 section（取最早命中）
            if fingerprint and fingerprint in stext:
                return s.get("title", "")
        # 没精确命中 → 返回空（比误归到错误的最后一节更诚实）
        return ""

    def _guess_page(self, chunk_text: str, pages: dict) -> int:
        """粗略推算 chunk 属于哪一页（用文本前 50 字匹配）。"""
        snippet = chunk_text[:50]
        for page_num, page_text in pages.items():
            if snippet in page_text:
                return page_num
        return 1
