# working_memory.py 核心流程：工作记忆 Redis 后端
#
#   conversation_history 的 Redis 后端。跨请求/多轮对话不丢上下文。
#   Redis 不可用时降级到内存 list（现有行为完全不变）。
from typing import List, Optional

from agentforge.llm.llm_basics import LLMMessage

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class WorkingMemory:
    """工作记忆：conversation_history 的 Redis 后端。
    Redis 不可用时所有方法 no-op，调用方走内存 list。"""

    def __init__(self, redis_client=None):
        self._redis = redis_client

    def _key(self, session_id: str) -> str:
        return f"session:{session_id}:working_memory"

    async def load(self, session_id: str) -> Optional[List[LLMMessage]]:
        """从 Redis 读当前会话 history。返回 None = Redis 没有/不可用。"""
        if not self._redis or not self._redis.available or not session_id:
            return None
        try:
            data = await self._redis.get(self._key(session_id))
            if data is None:
                return None
            return [
                LLMMessage(
                    role=m.get("role", "user"),
                    content=m.get("content", ""),
                    tool_call_id=m.get("tool_call_id", ""),
                )
                for m in data
            ]
        except Exception as e:
            logger.warning(f"[WorkingMemory] load 失败（降级到内存）: {e}")
            return None

    async def save(self, session_id: str, history: List[LLMMessage]) -> None:
        """写回 Redis（全量覆盖，带 24h TTL 防止进程崩溃后永久残留）。"""
        if not self._redis or not self._redis.available or not session_id:
            return
        try:
            data = [
                {"role": m.role, "content": m.content or "", "tool_call_id": m.tool_call_id or ""}
                for m in history
            ]
            # GC: 24h TTL——进程崩溃不清理时，Redis 自动过期
            await self._redis.set(self._key(session_id), data, ttl=86400)
        except Exception as e:
            logger.warning(f"[WorkingMemory] save 失败（忽略）: {e}")

    async def clear(self, session_id: str) -> None:
        """清空 Redis 里的工作记忆。"""
        if not self._redis or not self._redis.available or not session_id:
            return
        try:
            await self._redis.delete(self._key(session_id))
        except Exception as e:
            logger.warning(f"[WorkingMemory] clear 失败（忽略）: {e}")
