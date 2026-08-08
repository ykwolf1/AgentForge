# store.py 核心流程：SQLite 持久化记忆（会话存盘/恢复 + 跨会话检索）
#
#   两张表 + FTS5 全文索引：
#     sessions         活跃 history（压缩后精简版，用于会话恢复）
#     history_archive  原始 history（压缩前完整版，用于检索）
#
#   零外部依赖（sqlite3 是 Python 标准库，FTS5 是 SQLite 内置）
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from agentforge.llm.llm_basics import LLMMessage

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class MemoryStore:
    """SQLite 持久化存储。会话级存盘/恢复 + 跨会话全文检索。

    生产级改进：
    - WAL 模式 + busy_timeout=5s：多请求并发不撞锁
    - 连接单例：同一路径复用连接，避免频繁开关
    - GC：归档/checkpoint/summary 按保留条数自动清理
    """

    # 连接单例：db_path → sqlite3.Connection（同路径复用，避免频繁开关）
    _conn_pool: dict = {}

    def __init__(self, db_path: str = ""):
        if not db_path:
            db_path = str(Path.home() / ".agentforge" / "memory.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.debug(f"MemoryStore 初始化: {db_path}")

    def _get_conn(self) -> sqlite3.Connection:
        """获取连接（单例复用）。

        WAL 模式允许读写并发（读不阻塞写、写不阻塞读）。
        busy_timeout=5000 让写冲突时等 5 秒而不是立刻报 locked。
        """
        if self.db_path in MemoryStore._conn_pool:
            conn = MemoryStore._conn_pool[self.db_path]
            try:
                conn.execute("SELECT 1")  # 健康检查
                return conn
            except Exception:
                del MemoryStore._conn_pool[self.db_path]  # 连接坏了，重建
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下 NORMAL 足够安全且更快
        MemoryStore._conn_pool[self.db_path] = conn
        return conn

    def _init_db(self) -> None:
        """建表（幂等，已存在不重建）"""
        conn = self._get_conn()
        conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT,
                    content TEXT,
                    tool_calls TEXT,
                    tool_call_id TEXT,
                    agent_name TEXT,
                    created_at TEXT,
                    PRIMARY KEY (session_id, seq)
                );

                CREATE TABLE IF NOT EXISTS history_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    seq INTEGER,
                    role TEXT,
                    content TEXT,
                    agent_name TEXT,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_archive_session ON history_archive(session_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
                    content, session_id, agent_name
                );

                -- checkpoint 表：每 N 轮存一次完整快照（崩了能从最近 checkpoint 恢复）
                CREATE TABLE IF NOT EXISTS checkpoints (
                    session_id TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    history_json TEXT,
                    handoff_depth INTEGER DEFAULT 0,
                    created_at TEXT,
                    PRIMARY KEY (session_id, turn)
                );

                -- 摘要记忆表：压缩后的 summary 独立存储（不覆盖 sessions）
                CREATE TABLE IF NOT EXISTS summaries (
                    session_id TEXT NOT NULL,
                    turn INTEGER,
                    summary TEXT,
                    created_at TEXT
                );
            """)

    def _msg_to_row(self, msg: LLMMessage, agent_name: str = "") -> dict:
        """LLMMessage → 可存 dict"""
        tool_calls_json = None
        if msg.tool_calls:
            tool_calls_json = json.dumps([
                {"call_id": tc.call_id, "name": tc.name, "arguments": tc.arguments, "type": tc.type}
                for tc in msg.tool_calls
            ], ensure_ascii=False, default=str)
        return {
            "role": msg.role,
            "content": msg.content or "",
            "tool_calls": tool_calls_json,
            "tool_call_id": msg.tool_call_id or "",
            "agent_name": agent_name,
        }

    def _row_to_msg(self, row: tuple) -> LLMMessage:
        """存 dict → LLMMessage"""
        role, content, tool_calls_json, tool_call_id = row[2], row[3], row[4], row[5]
        tool_calls = None
        if tool_calls_json:
            try:
                from agentforge.llm.llm_basics import ToolCall
                raw = json.loads(tool_calls_json)
                tool_calls = [ToolCall(
                    call_id=tc.get("call_id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}),
                    type=tc.get("type", "function"),
                ) for tc in raw]
            except Exception:
                pass
        return LLMMessage(role=role, content=content, tool_calls=tool_calls, tool_call_id=tool_call_id)

    def save_session(self, session_id: str, history: List[LLMMessage], agent_name: str = "") -> None:
        """全量保存一个会话的 history（先删后插）"""
        if not session_id:
            return
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        for i, msg in enumerate(history):
            d = self._msg_to_row(msg, agent_name)
            conn.execute(
                "INSERT INTO sessions (session_id, seq, role, content, tool_calls, tool_call_id, agent_name, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, i, d["role"], d["content"], d["tool_calls"], d["tool_call_id"], d["agent_name"], now)
            )
        logger.debug(f"save_session: {session_id}, {len(history)} 条消息")

    def archive_before_compact(self, session_id: str, history: List[LLMMessage]) -> None:
        """压缩前归档原始 history（用于跨会话检索）"""
        if not session_id:
            return
        now = datetime.now().isoformat()
        conn = self._get_conn()
        for i, msg in enumerate(history):
            content = msg.content or ""
            agent = ""
            conn.execute(
                "INSERT INTO history_archive (session_id, seq, role, content, agent_name, created_at) VALUES (?,?,?,?,?,?)",
                (session_id, i, msg.role, content, agent, now)
            )
            # 同步写 FTS 索引（只索引有内容的消息）
            if content.strip():
                conn.execute(
                    "INSERT INTO history_fts (content, session_id, agent_name) VALUES (?,?,?)",
                    (content, session_id, agent)
                )
        logger.debug(f"archive_before_compact: {session_id}, {len(history)} 条")

    def load_session(self, session_id: str) -> List[LLMMessage]:
        """恢复一个会话的 history"""
        if not session_id:
            return []
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        msgs = [self._row_to_msg(r) for r in rows]
        logger.debug(f"load_session: {session_id}, 恢复 {len(msgs)} 条")
        return msgs

    def search(self, query: str, limit: int = 10) -> List[dict]:
        """跨会话全文检索（FTS5）。query 用引号包裹避免特殊字符报错。"""
        # FTS5 对含 . / - 等的查询要用双引号包成短语
        safe_query = '"' + query.replace('"', '""') + '"'
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT session_id, content, agent_name FROM history_fts WHERE content MATCH ? LIMIT ?",
                (safe_query, limit)
            ).fetchall()
        except sqlite3.OperationalError:
            # 如果短语匹配也失败，退化为 LIKE
            rows = conn.execute(
                "SELECT session_id, content, agent_name FROM history_fts WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", limit)
            ).fetchall()
        results = [{"session_id": r[0], "content": r[1][:200], "agent": r[2]} for r in rows]
        logger.debug(f"search '{query}': 找到 {len(results)} 条")
        return results

    def list_sessions(self) -> List[str]:
        """列出所有有记录的会话 ID"""
        conn = self._get_conn()
        rows = conn.execute("SELECT DISTINCT session_id FROM sessions").fetchall()
        return [r[0] for r in rows]

    def has_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row[0] > 0

    # ===== Checkpoint（每 N 轮存一次快照，崩了能恢复）=====

    def save_checkpoint(self, session_id: str, turn: int, history: List[LLMMessage], handoff_depth: int = 0) -> None:
        """存一个 checkpoint 快照（同 session 同 turn 覆盖）。"""
        if not session_id:
            return
        # 序列化 history（复用 _msg_to_row）
        history_data = [self._msg_to_row(msg) for msg in history]
        history_json = json.dumps(history_data, ensure_ascii=False, default=str)
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints (session_id, turn, history_json, handoff_depth, created_at) VALUES (?,?,?,?,?)",
            (session_id, turn, history_json, handoff_depth, now)
        )
        logger.debug(f"save_checkpoint: {session_id} turn={turn}, {len(history)} 条")

    def load_latest_checkpoint(self, session_id: str) -> Optional[dict]:
        """取最近一个 checkpoint 快照。返回 {turn, history, handoff_depth} 或 None。"""
        if not session_id:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT turn, history_json, handoff_depth FROM checkpoints WHERE session_id = ? ORDER BY turn DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        turn, history_json, handoff_depth = row
        try:
            history_data = json.loads(history_json)
        except Exception:
            return None
        # 反序列化回 LLMMessage（🔴2 修复：还原 tool_calls，不能只取三个字段）
        msgs = []
        for d in history_data:
            tool_calls = None
            tc_json = d.get("tool_calls")
            if tc_json:
                try:
                    from agentforge.llm.llm_basics import ToolCall
                    raw_tcs = json.loads(tc_json) if isinstance(tc_json, str) else tc_json
                    tool_calls = [ToolCall(
                        call_id=tc.get("call_id", ""),
                        name=tc.get("name", ""),
                        arguments=tc.get("arguments", {}),
                        type=tc.get("type", "function"),
                    ) for tc in raw_tcs]
                except Exception:
                    pass
            msgs.append(LLMMessage(
                role=d.get("role", "user"),
                content=d.get("content", ""),
                tool_call_id=d.get("tool_call_id", ""),
                tool_calls=tool_calls,
            ))
        logger.debug(f"load_latest_checkpoint: {session_id} turn={turn}, {len(msgs)} 条")
        return {"turn": turn, "history": msgs, "handoff_depth": handoff_depth}

    def list_checkpoints(self, session_id: str) -> List[int]:
        """列出某会话的所有 checkpoint 轮次（回放用）。"""
        if not session_id:
            return []
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT turn FROM checkpoints WHERE session_id = ? ORDER BY turn", (session_id,)
        ).fetchall()
        return [r[0] for r in rows]

    # ===== 摘要记忆（独立存储，不覆盖 sessions）=====

    def save_summary(self, session_id: str, turn: int, summary: str) -> None:
        """存压缩后的 summary（每 session 可多条，按 turn 区分）。"""
        if not session_id:
            return
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO summaries (session_id, turn, summary, created_at) VALUES (?,?,?,?)",
            (session_id, turn, summary, now)
        )
        logger.debug(f"save_summary: {session_id} turn={turn}")

    def load_latest_summary(self, session_id: str) -> Optional[str]:
        """取最近的 summary。"""
        if not session_id:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT summary FROM summaries WHERE session_id = ? ORDER BY turn DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        return row[0] if row else None

    # ===== GC / 清理（阻断1：防止数据无限增长）=====

    def cleanup_session(self, session_id: str) -> int:
        """会话结束后清理该 session 的所有数据（archive/checkpoint/summary）。

        返回删除的行数。sessions 表的清理由 save_session 的先删后插处理。
        """
        if not session_id:
            return 0
        conn = self._get_conn()
        deleted = 0
        # 清理归档
        cur = conn.execute("DELETE FROM history_archive WHERE session_id = ?", (session_id,))
        deleted += cur.rowcount
        # 清理 FTS（FTS5 的删除要用特殊语法）
        try:
            conn.execute("DELETE FROM history_fts WHERE session_id = ?", (session_id,))
        except Exception:
            pass
        # 清理 checkpoint
        cur = conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
        deleted += cur.rowcount
        # 清理 summary
        cur = conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        deleted += cur.rowcount
        conn.commit()
        if deleted > 0:
            logger.info(f"[GC] 清理 session {session_id}: 删除 {deleted} 行")
        return deleted

    def gc_retention(self, max_archive_per_session: int = 50, max_checkpoints_per_session: int = 20) -> int:
        """全局保留策略：每个 session 最多保留 N 条归档和 M 个 checkpoint。

        超出的旧数据按时间排序删除。定期调用（如每天一次）防止无限增长。
        返回删除的总行数。
        """
        conn = self._get_conn()
        deleted = 0
        # 归档：每个 session 保留最近 N 条
        conn.execute("""
            DELETE FROM history_archive WHERE rowid IN (
                SELECT rowid FROM history_archive a1
                WHERE (SELECT COUNT(*) FROM history_archive a2
                       WHERE a2.session_id = a1.session_id AND a2.seq >= a1.seq) > ?
                ORDER BY a1.session_id, a1.seq
            )
        """, (max_archive_per_session,))
        deleted += conn.total_changes
        # checkpoint：每个 session 保留最近 M 个 turn
        conn.execute("""
            DELETE FROM checkpoints WHERE (session_id, turn) NOT IN (
                SELECT session_id, turn FROM (
                    SELECT session_id, turn,
                           ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY turn DESC) as rn
                    FROM checkpoints
                ) WHERE rn <= ?
            )
        """, (max_checkpoints_per_session,))
        conn.commit()
        # VACUUM 回收空间（WAL 模式下 VACUUM 会重建数据库文件）
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        if deleted > 0:
            logger.info(f"[GC] retention: 删除 {deleted} 行过期数据")
        return deleted


# ===== 单例工厂（修复⑦：避免散落 7+ 处各自 new MemoryStore()）=====

_memory_store_instance: Optional[MemoryStore] = None

def get_memory_store(db_path: str = "") -> MemoryStore:
    """全局单例 MemoryStore（同路径复用，避免频繁开关连接）。"""
    global _memory_store_instance
    if _memory_store_instance is None:
        _memory_store_instance = MemoryStore(db_path)
    return _memory_store_instance
