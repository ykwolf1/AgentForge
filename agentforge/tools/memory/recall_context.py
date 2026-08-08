# recall_context_tool.py —— 按需调取卸载的工具结果
#
#   配合 ContextManager 的符号化记忆：
#     工具结果超长时被卸载到存储层，上下文只保留符号化摘要 + ref_id。
#     agent 需要原始数据时，调本工具按 ref_id 取回。
#
#   对应腾讯设计的："Agent 如需原始证据，可按需触发检索调取完整日志"
from typing import Any, Mapping

from agentforge.llm.llm_basics import ToolCallResult
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.tools.tool_manager import register_tool


@register_tool(name="recall_tool_result", providers="*")
class RecallToolResultTool(BaseTool):
    """调取之前被卸载的工具完整结果。"""

    name = "recall_tool_result"
    display_name = "Recall Tool Result"
    description = (
        "Retrieve the full original result of a previous tool call that was offloaded. "
        "When a tool result was too long for the context window, only a symbolic summary "
        "was kept (with a ref_id like 'ctx_xxx'). Use this tool with that ref_id to get "
        "the complete original data when you need the full details."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "ref_id": {
                "type": "string",
                "description": "The ref_id from the symbolic summary (e.g. 'ctx_xxxxx_xxxxxx')",
            },
        },
        "required": ["ref_id"],
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        ref_id = kwargs.get("ref_id", "")
        call_id = kwargs.get("call_id", "")

        if not ref_id:
            return ToolCallResult(call_id=call_id, error="ref_id is required")

        try:
            from agentforge.context import get_context_manager
            mgr = get_context_manager()
            content = mgr.recall(ref_id)
            if content is None:
                return ToolCallResult(
                    call_id=call_id,
                    error=f"未找到 ref_id={ref_id} 的工具结果（可能已过期）",
                )
            return ToolCallResult(call_id=call_id, result=content)
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"调取失败: {e}")
