# longterm_memory_tool.py —— 长期记忆管理工具（L0~L3 分层）
#
#   让 agent 能主动操作分层长期记忆：
#     remember_fact:  存一条 L1 原子事实（偏好/约束/事件/需求）
#     recall_memory:  检索长期记忆（L3 画像 + L2 场景 + L1 事实）
#     save_scenario:  创建一个 L2 场景记忆（按项目/任务聚合事实）
#
#   与旧 memory 工具（markdown 文件读写）的区别：
#     旧 memory 工具：手动写 markdown 文件，无检索、无分层
#     本工具：SQLite + FTS5 全文检索，L0~L3 四层金字塔，渐进式召回
from typing import Any, Mapping

from agentforge.llm.llm_basics import ToolCallResult
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.tools.tool_manager import register_tool


@register_tool(name="remember_fact", providers="*")
class RememberFactTool(BaseTool):
    """存一条长期记忆事实（L1 原子事实层）。"""

    name = "remember_fact"
    display_name = "Remember Fact"
    description = (
        "Store a durable fact to long-term memory for future sessions. "
        "Use this to remember user preferences, project constraints, important events, "
        "or task requirements that should persist across conversations. "
        "Categories: preference, constraint, event, requirement, general."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember (e.g. 'User prefers concise code')",
            },
            "category": {
                "type": "string",
                "enum": ["preference", "constraint", "event", "requirement", "general"],
                "default": "general",
                "description": "Fact category",
            },
        },
        "required": ["content"],
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        content = kwargs.get("content", "")
        category = kwargs.get("category", "general")
        call_id = kwargs.get("call_id", "")

        if not content.strip():
            return ToolCallResult(call_id=call_id, error="content is required")

        try:
            from agentforge.memory.longterm import get_longterm_memory
            ltm = get_longterm_memory()
            fact_id = ltm.add_fact(content=content, category=category)
            return ToolCallResult(
                call_id=call_id,
                result=f"✅ 已记住 [{category}]: {content}\nfact_id: {fact_id}",
            )
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"记忆存储失败: {e}")


@register_tool(name="recall_memory", providers="*")
class RecallMemoryTool(BaseTool):
    """检索长期记忆（L3 画像 + L2 场景 + L1 事实）。"""

    name = "recall_memory"
    display_name = "Recall Memory"
    description = (
        "Recall relevant long-term memories (user persona, scenario context, historical facts). "
        "Searches L3 core persona (always loaded) + L2 scenario memories (matched by task) "
        "+ L1 atomic facts (keyword search). Use this when you need context from past sessions."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to recall (e.g. 'user tech stack' or 'knowledge base project')",
            },
        },
        "required": ["query"],
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        query = kwargs.get("query", "")
        call_id = kwargs.get("call_id", "")

        if not query.strip():
            return ToolCallResult(call_id=call_id, error="query is required")

        try:
            from agentforge.memory.longterm import get_longterm_memory
            ltm = get_longterm_memory()
            result = ltm.recall(task_description=query)
            if not result:
                return ToolCallResult(call_id=call_id, result="暂无相关长期记忆")
            return ToolCallResult(call_id=call_id, result=result)
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"记忆检索失败: {e}")


@register_tool(name="save_scenario", providers="*")
class SaveScenarioTool(BaseTool):
    """创建一个 L2 场景记忆（按项目/任务聚合事实）。"""

    name = "save_scenario"
    display_name = "Save Scenario"
    description = (
        "Create a scenario memory (L2) to group related facts by project, task, or context. "
        "This helps quickly restore working context when resuming a similar task. "
        "Include a summary of the scenario for fast context recovery."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Scenario name (e.g. 'AgentForge knowledge base development')",
            },
            "summary": {
                "type": "string",
                "description": "Brief summary of this scenario for quick context recovery",
            },
            "description": {
                "type": "string",
                "description": "Detailed description of the scenario",
            },
        },
        "required": ["name", "summary"],
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        name = kwargs.get("name", "")
        summary = kwargs.get("summary", "")
        description = kwargs.get("description", "")
        call_id = kwargs.get("call_id", "")

        if not name.strip():
            return ToolCallResult(call_id=call_id, error="name is required")

        try:
            from agentforge.memory.longterm import get_longterm_memory
            ltm = get_longterm_memory()
            sid = ltm.create_scenario(name=name, summary=summary, description=description)
            return ToolCallResult(
                call_id=call_id,
                result=f"✅ 场景记忆已创建\n名称: {name}\nscenario_id: {sid}\n摘要: {summary}",
            )
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"场景创建失败: {e}")
