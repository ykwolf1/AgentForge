# context/store.py —— 工具结果存储层（L0 原始证据层）
#
#   设计参照腾讯 TencentDB Agent Memory 的"外部卸载"：
#     "将繁重原始数据外部卸载至存储层，上下文只保留轻量化结构化信息"
#
#   每次工具调用产生的完整结果，存到磁盘文件（~/.agentforge/context_store/）。
#   conversation_history 里只放符号化摘要 + 存储引用（ref_id）。
#   agent 需要原始数据时，用 ref_id 按需调取。
#
#   存储格式：每个工具结果一个 JSON 文件，含完整内容 + 元数据。
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class ToolResultEntry:
    """一条工具结果的完整记录（对应腾讯 L0 原始会话层）。"""
    ref_id: str                         # 唯一引用 ID（agent 用它调取原始数据）
    tool_name: str                      # 工具名（web_search / knowledge_search / bash ...）
    call_id: str                        # 工具调用 ID（关联 tool_call_id）
    arguments: Dict[str, Any]           # 调用参数（用于追溯 agent 传了什么）
    result: Any                         # 完整原始结果
    success: bool                       # 是否成功
    timestamp: float = field(default_factory=time.time)
    # 符号化摘要（卸载时生成，放上下文里替代原始结果）
    summary: str = ""                   # 一句话摘要："web_search '趋势' → 5 条结果，前3条关于..."
    key_facts: List[str] = field(default_factory=list)  # 关键事实提取（最多 5 条）


class ToolResultStore:
    """工具结果存储层。把完整工具结果卸载到磁盘，上下文只保留符号。"""

    def __init__(self, store_dir: Optional[str] = None, max_entries: int = 500):
        if store_dir is None:
            store_dir = str(Path.home() / ".agentforge" / "context_store")
        self._store_dir = Path(store_dir)
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, ToolResultEntry] = {}
        self._max_entries = max_entries  # 磁盘文件上限（超过自动清理最老的）

    def store(self, tool_name: str, call_id: str, arguments: Dict[str, Any],
              result: Any, success: bool, summary: str = "",
              key_facts: Optional[List[str]] = None) -> str:
        """存一条工具结果，返回 ref_id。"""
        ref_id = f"ctx_{int(time.time()*1000)}_{call_id[-6:] if call_id else 'xxxxxx'}"

    def store(self, tool_name: str, call_id: str, arguments: Dict[str, Any],
              result: Any, success: bool, summary: str = "",
              key_facts: Optional[List[str]] = None) -> str:
        """存一条工具结果，返回 ref_id。"""
        ref_id = f"ctx_{int(time.time()*1000)}_{call_id[-6:] if call_id else 'xxxxxx'}"

        entry = ToolResultEntry(
            ref_id=ref_id,
            tool_name=tool_name,
            call_id=call_id,
            arguments=self._shrink_args(tool_name, arguments),
            result=result,
            success=success,
            summary=summary,
            key_facts=key_facts or [],
        )

        # 写磁盘（JSON）
        path = self._store_dir / f"{ref_id}.json"
        write_ok = True
        try:
            path.write_text(json.dumps(asdict(entry), ensure_ascii=False, default=str), encoding="utf-8")
        except Exception as e:
            write_ok = False
            logger.warning(f"[ContextStore] 写磁盘失败: {e}")

        # 只有写盘成功才塞缓存（阻断修复10：避免跨进程不一致）
        if write_ok:
            self._cache[ref_id] = entry

        # GC：超过上限时清理最老的文件
        self._gc()

        return ref_id

    def _gc(self) -> int:
        """GC：磁盘文件超过 max_entries 时，按修改时间删最老的。"""
        files = sorted(self._store_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        deleted = 0
        while len(files) > self._max_entries:
            oldest = files.pop(0)
            try:
                oldest.unlink()
                deleted += 1
            except Exception:
                pass
        # 内存缓存也要清理（最多保留 max_entries）
        if len(self._cache) > self._max_entries:
            # 按时间戳排序（ref_id 里有毫秒时间戳）
            sorted_keys = sorted(self._cache.keys())
            while len(self._cache) > self._max_entries:
                self._cache.pop(sorted_keys.pop(0))
        return deleted

    def retrieve(self, ref_id: str) -> Optional[ToolResultEntry]:
        """按 ref_id 取回完整原始结果。"""
        if ref_id in self._cache:
            return self._cache[ref_id]
        path = self._store_dir / f"{ref_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = ToolResultEntry(**data)
            self._cache[ref_id] = entry
            return entry
        except Exception as e:
            logger.warning(f"[ContextStore] 读取失败 {ref_id}: {e}")
            return None

    def _shrink_args(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """存储时对超长参数做精简（只存摘要，不存全文）。

        write_file 的 content 可能 5 万字，存储时只存前 200 字 + 长度。
        原始文件内容已经写到磁盘文件了，不需要在这里再存一份。
        """
        SHRINK_FIELDS = {
            "write_file": ["content"],
            "edit": ["content"],
            "bash": ["command"],
        }
        shrink_keys = SHRINK_FIELDS.get(tool_name, [])
        if not shrink_keys:
            return args
        shrunk = {}
        for k, v in args.items():
            if k in shrink_keys and isinstance(v, str) and len(v) > 200:
                shrunk[k] = f"{v[:200]}...[{len(v)} 字符]"
            else:
                shrunk[k] = v
        return shrunk
