# recall_tool.py 核心流程：让 LLM 检索跨会话历史记忆
#
#   LLM 调 recall(query="上次修 auth.py") → 查 SQLite FTS5 → 返回相关历史片段
#   注册成普通工具，走四道关卡
from typing import Any, Mapping

from agentforge.llm.llm_basics import ToolCallResult
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.tools.tool_manager import register_tool


@register_tool(name="recall", providers="*")
class RecallTool(BaseTool):
    """检索跨会话的历史记忆（之前聊过什么、修过什么文件等）。"""
    name = "recall"
    display_name = "Recall Memory"
    description = (
        "Search past conversation history across sessions. "
        "Use this when you need to recall what was discussed or done before "
        "(e.g. 'what bug did we fix last time', 'auth.py'). "
        "Returns matching conversation snippets."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query — keywords or phrases to look up in past conversations"
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 5)",
                "default": 5
            }
        },
        "required": ["query"]
    }
    risk_level = ToolRiskLevel.SAFE   # 只读检索，无风险

    async def execute(self, **kwargs) -> ToolCallResult:
        query = kwargs.get("query", "")
        limit = kwargs.get("limit", 5)
        if not query:
            return ToolCallResult(call_id=kwargs.get("call_id", ""), error="query is required")

        try:
            from agentforge.memory.store import MemoryStore, get_memory_store
            store = get_memory_store()
            results = store.search(query, limit=limit)
            if not results:
                return ToolCallResult(
                    call_id=kwargs.get("call_id", ""),
                    result=f"No past memories found for '{query}'."
                )
            # 格式化结果给 LLM
            parts = [f"Found {len(results)} memories for '{query}':\n"]
            for i, r in enumerate(results, 1):
                parts.append(f"[{i}] session={r['session_id']}\n    {r['content']}\n")
            return ToolCallResult(
                call_id=kwargs.get("call_id", ""),
                result="".join(parts)
            )
        except Exception as e:
            return ToolCallResult(
                call_id=kwargs.get("call_id", ""),
                error=f"Recall failed: {e}"
            )
