# memory/longterm.py —— 长期分层记忆（L0~L3 金字塔架构）
#
#   参照腾讯 TencentDB Agent Memory 的"L0~L3 四层金字塔分层架构"：
#     "长期记忆采用渐进式提炼，由原始对话向上沉淀为稳定认知，
#      自上而下轻量化召回"
#
#   四层结构：
#     L0 原始会话层（Conversation）：完整工具调用/对话记录，证据溯源底座
#     L1 原子事实层（Atom）：自动抽取偏好/约束/事件/需求等独立事实，精准检索
#     L2 场景记忆层（Scenario）：按项目/任务/场景聚合原子事实，快速恢复上下文
#     L3 核心画像层（Core/Persona）：长期稳定的用户画像/角色设定，最高优先级载入
#
#   召回策略：
#     "优先加载高层轻量化记忆；需要细节时向下检索底层原始数据"
#     L3（全量载入）→ L2（按当前任务匹配）→ L1（FTS5 关键词检索）→ L0（按需取回）
#
#   存储底座：SQLite（已有 memory.db），不依赖 Redis，重启不丢。
import json
import time
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


# ===== 数据模型 =====

@dataclass
class AtomFact:
    """L1 原子事实：一条独立的、可检索的持久事实。"""
    fact_id: str               # 唯一 ID
    content: str               # 事实内容（如 "用户在做 AgentForge 项目"）
    category: str = "general"  # 分类：preference / constraint / event / requirement / general
    source_session: str = ""   # 来源会话 ID
    confidence: float = 1.0    # 置信度（LLM 提取时给的分）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class ScenarioMemory:
    """L2 场景记忆：按项目/任务/场景聚合的原子事实集合。"""
    scenario_id: str           # 唯一 ID
    name: str                  # 场景名（如 "AgentForge 知识库开发"）
    description: str = ""      # 场景描述
    fact_ids: List[str] = field(default_factory=list)  # 关联的 L1 原子事实
    summary: str = ""          # 场景摘要（LLM 生成，用于快速恢复上下文）
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


@dataclass
class CorePersona:
    """L3 核心画像：长期稳定的用户画像/角色设定，最高优先级。"""
    key: str                   # 键（如 "role" / "tech_stack" / "communication_style"）
    value: str                 # 值（如 "后端架构师" / "Python + FastAPI" / "简洁直接"）
    priority: int = 0          # 优先级（数字越大越先载入 prompt）


# ===== 存储层 =====

class LongTermMemory:
    """长期分层记忆管理器（L0~L3）。

    底座是 SQLite（~/.agentforge/memory.db），与已有的 MemoryStore 共享数据库。
    不依赖 Redis，重启不丢。

    用法：
        ltm = LongTermMemory()

        # L3：会话开头载入核心画像
        persona_prompt = ltm.load_persona_prompt()

        # L1：会话中自动提取原子事实
        ltm.extract_facts_from_text("用户偏好简洁代码", session_id="sess_xxx")

        # L2：按当前任务匹配场景
        scenarios = ltm.match_scenarios("知识库开发")

        # L0：原始记录（由 ToolResultStore + MemoryStore 负责，这里只做接口）
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS lt_l1_atoms (
        fact_id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        source_session TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        created_at REAL,
        updated_at REAL
    );

    CREATE TABLE IF NOT EXISTS lt_l1_atoms_fts (
        content,
        fact_id UNINDEXED,
        category UNINDEXED
    );

    CREATE TABLE IF NOT EXISTS lt_l2_scenarios (
        scenario_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        fact_ids TEXT DEFAULT '[]',
        summary TEXT DEFAULT '',
        created_at REAL,
        last_accessed REAL
    );

    CREATE TABLE IF NOT EXISTS lt_l2_scenarios_fts (
        name,
        description,
        summary,
        scenario_id UNINDEXED
    );

    CREATE TABLE IF NOT EXISTS lt_l3_persona (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        priority INTEGER DEFAULT 0
    );
    """

    # FTS5 虚拟表需单独创建
    FTS_STATEMENTS = [
        "CREATE VIRTUAL TABLE IF NOT EXISTS lt_l1_atoms_fts USING fts5(content, fact_id UNINDEXED, category UNINDEXED);",
        "CREATE VIRTUAL TABLE IF NOT EXISTS lt_l2_scenarios_fts USING fts5(name, description, summary, scenario_id UNINDEXED);",
    ]

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".agentforge" / "memory.db")
        self._db_path = db_path
        self._init_db()

    # 连接单例：与 MemoryStore 共享同一 db 文件，复用连接池
    _conn_pool: dict = {}

    def _conn(self) -> sqlite3.Connection:
        """获取连接（WAL + busy_timeout + 单例复用）。"""
        if self._db_path in LongTermMemory._conn_pool:
            conn = LongTermMemory._conn_pool[self._db_path]
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                del LongTermMemory._conn_pool[self._db_path]
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        LongTermMemory._conn_pool[self._db_path] = conn
        return conn

    def _init_db(self):
        conn = self._conn()
        conn.executescript(self.SCHEMA)
        conn.commit()
        for stmt in self.FTS_STATEMENTS:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # FTS 表已存在

    # ===== L1：原子事实 =====

    def add_fact(self, content: str, category: str = "general",
                 source_session: str = "", confidence: float = 1.0) -> str:
        """添加一条 L1 原子事实。"""
        import uuid
        fact_id = f"fact_{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn = self._conn()
        conn.execute(
            "INSERT INTO lt_l1_atoms (fact_id, content, category, source_session, confidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fact_id, content, category, source_session, confidence, now, now)
        )
        conn.execute(
            "INSERT INTO lt_l1_atoms_fts (content, fact_id, category) VALUES (?, ?, ?)",
            (content, fact_id, category)
        )
        conn.commit()
        logger.info(f"[LTM/L1] 添加原子事实 [{category}]: {content[:50]}")
        return fact_id

    def add_facts_batch(self, facts: List[Dict[str, Any]]) -> int:
        """批量添加原子事实。facts: [{content, category, confidence}]"""
        added = 0
        # 批量去重：一次查出所有已存在的 content（修复 N+1）
        conn = self._conn()
        all_contents = [f.get("content", "").strip() for f in facts if f.get("content", "").strip()]
        if not all_contents:
            return 0
        placeholders = ",".join("?" * len(all_contents))
        existing_rows = conn.execute(
            f"SELECT content FROM lt_l1_atoms WHERE content IN ({placeholders})", all_contents
        ).fetchall()
        existing_contents = {r["content"] for r in existing_rows}

        import uuid
        now = time.time()
        rows_to_insert = []
        fts_rows_to_insert = []
        for f in facts:
            content = f.get("content", "").strip()
            if not content or content in existing_contents:
                continue
            fact_id = f"fact_{uuid.uuid4().hex[:12]}"
            rows_to_insert.append((
                fact_id, content, f.get("category", "general"),
                f.get("source_session", ""), f.get("confidence", 1.0), now, now
            ))
            fts_rows_to_insert.append((content, fact_id, f.get("category", "general")))
            added += 1

        if rows_to_insert:
            conn.executemany(
                "INSERT INTO lt_l1_atoms (fact_id, content, category, source_session, confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", rows_to_insert
            )
            conn.executemany(
                "INSERT INTO lt_l1_atoms_fts (content, fact_id, category) VALUES (?, ?, ?)", fts_rows_to_insert
            )
            conn.commit()
            logger.info(f"[LTM/L1] 批量添加 {added} 条原子事实")
        return added

    def search_facts(self, query: str, limit: int = 5) -> List[AtomFact]:
        """FTS5 关键词检索 L1 原子事实。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT a.* FROM lt_l1_atoms_fts f "
                "JOIN lt_l1_atoms a ON a.fact_id = f.fact_id "
                "WHERE lt_l1_atoms_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit)
            ).fetchall()
        except Exception:
            # FTS 查询失败时退化为 LIKE
            rows = conn.execute(
                "SELECT * FROM lt_l1_atoms WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", limit)
            ).fetchall()
        return [AtomFact(**dict(r)) for r in rows]

    def get_all_facts(self, category: Optional[str] = None) -> List[AtomFact]:
        """获取全部（或某分类）原子事实。"""
        conn = self._conn()
        if category:
            rows = conn.execute(
                "SELECT * FROM lt_l1_atoms WHERE category = ? ORDER BY updated_at DESC",
                (category,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lt_l1_atoms ORDER BY updated_at DESC"
            ).fetchall()
        return [AtomFact(**dict(r)) for r in rows]

    def _fact_exists(self, content: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM lt_l1_atoms WHERE content = ? LIMIT 1", (content,)
        ).fetchone()
        return row is not None

    # ===== L2：场景记忆 =====

    def create_scenario(self, name: str, description: str = "",
                        fact_ids: Optional[List[str]] = None,
                        summary: str = "") -> str:
        """创建一个 L2 场景记忆。"""
        import uuid
        sid = f"scn_{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn = self._conn()
        conn.execute(
            "INSERT INTO lt_l2_scenarios (scenario_id, name, description, fact_ids, summary, created_at, last_accessed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, name, description, json.dumps(fact_ids or []), summary, now, now)
        )
        conn.execute(
            "INSERT INTO lt_l2_scenarios_fts (name, description, summary, scenario_id) VALUES (?, ?, ?, ?)",
            (name, description, summary, sid)
        )
        conn.commit()
        logger.info(f"[LTM/L2] 创建场景: {name}")
        return sid

    def match_scenarios(self, query: str, limit: int = 3) -> List[ScenarioMemory]:
        """按当前任务匹配 L2 场景（FTS5 检索场景名/描述/摘要）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT s.* FROM lt_l2_scenarios_fts f "
                "JOIN lt_l2_scenarios s ON s.scenario_id = f.scenario_id "
                "WHERE lt_l2_scenarios_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit)
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT * FROM lt_l2_scenarios WHERE name LIKE ? OR description LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)
            ).fetchall()
        # 更新访问时间
        for r in rows:
            conn.execute(
                "UPDATE lt_l2_scenarios SET last_accessed = ? WHERE scenario_id = ?",
                (time.time(), r["scenario_id"])
            )
        conn.commit()
        return [ScenarioMemory(
            scenario_id=r["scenario_id"], name=r["name"],
            description=r["description"], fact_ids=json.loads(r["fact_ids"]),
            summary=r["summary"], created_at=r["created_at"], last_accessed=r["last_accessed"]
        ) for r in rows]

    def get_scenario_facts(self, scenario_id: str) -> List[AtomFact]:
        """取一个场景下的全部原子事实。"""
        conn = self._conn()
        row = conn.execute(
            "SELECT fact_ids FROM lt_l2_scenarios WHERE scenario_id = ?", (scenario_id,)
        ).fetchone()
        if not row:
            return []
        fact_ids = json.loads(row["fact_ids"])
        conn = self._conn()
        placeholders = ",".join("?" * len(fact_ids))
        rows = conn.execute(
            f"SELECT * FROM lt_l1_atoms WHERE fact_id IN ({placeholders})", fact_ids
        ).fetchall()
        return [AtomFact(**dict(r)) for r in rows]

    # ===== L3：核心画像 =====

    def set_persona(self, key: str, value: str, priority: int = 0) -> None:
        """设置/更新一条 L3 核心画像。"""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO lt_l3_persona (key, value, priority) VALUES (?, ?, ?)",
            (key, value, priority)
        )
        conn.commit()
        logger.info(f"[LTM/L3] 画像 [{key}] = {value[:50]}")

    def get_persona(self, key: str) -> Optional[str]:
        conn = self._conn()
        row = conn.execute(
            "SELECT value FROM lt_l3_persona WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def get_all_persona(self) -> List[CorePersona]:
        """获取全部核心画像（按优先级排序）。"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM lt_l3_persona ORDER BY priority DESC"
        ).fetchall()
        return [CorePersona(**dict(r)) for r in rows]

    def load_persona_prompt(self) -> str:
        """L3 核心画像 → 注入 prompt 的文本（会话开头调）。"""
        personas = self.get_all_persona()
        if not personas:
            return ""
        lines = ["[用户画像]"]
        for p in personas:
            lines.append(f"  {p.key}: {p.value}")
        return "\n".join(lines)

    # ===== 渐进式召回（整合 L3→L2→L1）=====

    def recall(self, task_description: str, include_facts: bool = True) -> str:
        """渐进式召回：L3 全量 → L2 按 task 匹配 → L1 关键词检索。

        对应腾讯设计的："优先加载高层轻量化记忆；需要细节时向下检索"
        """
        parts = []

        # L3：核心画像（全量载入，最高优先级）
        persona = self.load_persona_prompt()
        if persona:
            parts.append(persona)

        # L2：按任务匹配场景
        scenarios = self.match_scenarios(task_description, limit=2)
        if scenarios:
            lines = ["[相关场景记忆]"]
            for scn in scenarios:
                lines.append(f"  场景: {scn.name}")
                if scn.summary:
                    lines.append(f"  摘要: {scn.summary[:100]}")
            parts.append("\n".join(lines))

        # L1：关键词检索原子事实
        if include_facts:
            facts = self.search_facts(task_description, limit=5)
            if facts:
                lines = ["[相关事实记忆]"]
                for f in facts:
                    lines.append(f"  - {f.content}")
                parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    # ===== LLM 提取 L1 原子事实 =====

    async def extract_facts_from_conversation(
        self, conversation_text: str, session_id: str, llm_client=None
    ) -> int:
        """用 LLM 从对话中提取 L1 原子事实。

        对应腾讯 L1："自动抽取偏好、约束、事件、需求等独立事实"
        """
        if not llm_client or not conversation_text.strip():
            return 0

        prompt = EXTRACT_FACTS_PROMPT.format(conversation=conversation_text[:3000])

        try:
            from agentforge.llm.llm_events import LLM_Events
            buf = []
            async for event in llm_client.astream_response(
                messages=[{"role": "user", "content": prompt}],
                tools=[], api="chat",
            ):
                if event.type == LLM_Events.ASSISTANT_DELTA and event.data:
                    buf.append(str(event.data))
                elif event.type in (LLM_Events.RESPONSE_FINISHED, LLM_Events.ERROR):
                    break

            response = "".join(buf).strip()
            # 解析 JSON
            import re
            if "```" in response:
                match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', response, re.DOTALL)
                if match:
                    response = match.group(1)

            facts_data = json.loads(response)
            facts = []
            for item in facts_data:
                if isinstance(item, dict):
                    facts.append({
                        "content": item.get("content", ""),
                        "category": item.get("category", "general"),
                        "confidence": item.get("confidence", 0.8),
                        "source_session": session_id,
                    })
                elif isinstance(item, str):
                    facts.append({"content": item, "source_session": session_id})

            return self.add_facts_batch(facts)

        except json.JSONDecodeError:
            logger.debug("[LTM] LLM 提取响应不是有效 JSON，跳过")
            return 0
        except Exception as e:
            logger.warning(f"[LTM] L1 原子事实提取失败: {e}")
            return 0


# ===== LLM 提取 prompt =====

EXTRACT_FACTS_PROMPT = """Analyze the conversation and extract durable facts. Classify each fact.

Categories:
- preference: User preferences (coding style, communication style, tool choices)
- constraint: Project constraints (tech stack, deadlines, requirements)
- event: Important events (deployments, milestones, decisions made)
- requirement: Task requirements (what the user needs to accomplish)
- general: Other durable facts

Rules:
- Only extract facts that persist across sessions
- Ignore temporary task details
- Be concise: one fact per entry

Conversation:
{conversation}

Respond in JSON array ONLY:
[{{"content": "fact text", "category": "preference", "confidence": 0.9}}, ...]
"""


# 全局单例
_ltm_instance: Optional[LongTermMemory] = None


def get_longterm_memory() -> LongTermMemory:
    global _ltm_instance
    if _ltm_instance is None:
        _ltm_instance = LongTermMemory()
    return _ltm_instance
