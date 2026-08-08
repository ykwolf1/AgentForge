# spawn_tool.py 核心流程：动态生成子 agent + 监控
#
#   spawn_agent: LLM 指定 task/tools/instructions → 动态 new Agent → 异步执行
#   check_sub_agent: 查子 agent 状态/进度/结果
#
#   工程化（在 TaskManager 里）：失败重试 + 并发队列 + 超时 + 进度监控
import copy
from typing import Any, Dict, List, Mapping

from agentforge.llm.llm_basics import ToolCallResult
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.tools.tool_manager import register_tool


def _get_task_manager():
    """获取全局 TaskManager。CLI 模式下没初始化则返回 None。"""
    try:
        from agentforge.server.task_manager import _task_mgr
        if _task_mgr._cfg_mgr is None:
            return None  # CLI 模式没 setup
        return _task_mgr
    except Exception:
        return None


@register_tool(name="spawn_agent", providers="*")
class SpawnAgentTool(BaseTool):
    """动态生成子 agent，异步执行任务。

    主 agent 调用后立即返回 sub_task_id（不阻塞）。
    子 agent 在后台独立运行，有自己的工具集和指令。
    结果通过 check_sub_agent 查询。"""
    name = "spawn_agent"
    display_name = "Spawn Sub-Agent"
    description = (
        "Dynamically create a sub-agent to execute a task asynchronously. "
        "The sub-agent runs independently with its own tools and instructions. "
        "Returns a sub_task_id immediately. Use check_sub_agent to get the result. "
        "Use this when you need a specialized agent for a subtask "
        "(e.g. 'spawn an agent to analyze data with bash + read_file tools')."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Clear description of what the sub-agent should do"
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tools the sub-agent can use (e.g. ['bash','read_file']). If omitted, all tools available."
            },
            "instructions": {
                "type": "string",
                "description": "Role/persona for the sub-agent (e.g. 'You are a data analyst'). If omitted, uses default."
            }
        },
        "required": ["task"]
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        task_desc = kwargs.get("task", "")
        tools_list = kwargs.get("tools")  # None = 全部工具
        instructions = kwargs.get("instructions", "")
        call_id = kwargs.get("call_id", "")

        if not task_desc:
            return ToolCallResult(call_id=call_id, error="task is required")

        # 从 kwargs 拿主 agent 的配置（由 tool_mgr.execute 透传 agent=self）
        agent = kwargs.get("agent")
        if not agent:
            return ToolCallResult(call_id=call_id, error="spawn_agent 必须从 agent 内部调用")

        # 动态构造 AgentConfig：复用主 agent 的 model/api_key，覆盖 prompt/tools
        base_cfg = agent.agent_config
        sub_cfg = copy.deepcopy(base_cfg)
        sub_cfg.agent_name = f"sub_{id(kwargs)}"
        sub_cfg.system_prompt = instructions or (
            f"You are a specialized sub-agent. Task: {task_desc}. "
            "Execute the task using your tools. When done, provide a clear summary."
        )
        sub_cfg.allowed_tools = tools_list if tools_list else None
        sub_cfg.role = "worker"

        # 提交到 TaskManager（异步，不阻塞）
        try:
            tm = _get_task_manager()
            if tm is None:
                return ToolCallResult(
                    call_id=call_id,
                    result="spawn_agent 仅在 API 服务模式（--serve）下可用。CLI 模式请用 delegate 工具。",
                )
            sub_task_id = await tm.submit(
                prompt=task_desc,
                session_id="",  # 子 agent 独立会话，不继承主 agent 的 history
                agent_config_override=sub_cfg,
                max_retries=2,
                timeout=120,
            )
            return ToolCallResult(
                call_id=call_id,
                result=f"子 agent 已创建并开始执行。sub_task_id={sub_task_id}\n"
                       f"用 check_sub_agent 工具查询进度和结果。",
                metadata={"sub_task_id": sub_task_id},
            )
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"spawn 失败: {e}")


@register_tool(name="check_sub_agent", providers="*")
class CheckSubAgentTool(BaseTool):
    """查询子 agent 的状态/进度/结果。"""
    name = "check_sub_agent"
    display_name = "Check Sub-Agent"
    description = (
        "Check the status and result of a previously spawned sub-agent. "
        "Returns status (pending/running/completed/failed/cancelled), "
        "progress info, and result (if completed)."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "sub_task_id": {
                "type": "string",
                "description": "The sub_task_id returned by spawn_agent"
            }
        },
        "required": ["sub_task_id"]
    }
    risk_level = ToolRiskLevel.SAFE

    async def execute(self, **kwargs) -> ToolCallResult:
        sub_task_id = kwargs.get("sub_task_id", "")
        call_id = kwargs.get("call_id", "")

        if not sub_task_id:
            return ToolCallResult(call_id=call_id, error="sub_task_id is required")

        try:
            tm = _get_task_manager()
            if tm is None:
                return ToolCallResult(call_id=call_id, error="check_sub_agent 仅在 API 服务模式下可用")
            task = tm.get(sub_task_id)
            if not task:
                return ToolCallResult(
                    call_id=call_id,
                    error=f"子 agent {sub_task_id} 不存在"
                )

            status = task.status.value
            if status == "completed":
                return ToolCallResult(
                    call_id=call_id,
                    result=f"子 agent 已完成 ✅\n结果:\n{task.result}",
                )
            elif status == "failed":
                return ToolCallResult(
                    call_id=call_id,
                    result=f"子 agent 失败 ❌\n错误: {task.error}",
                )
            elif status == "cancelled":
                return ToolCallResult(
                    call_id=call_id,
                    result=f"子 agent 已取消。",
                )
            else:
                # running 或 pending
                progress = task.progress
                return ToolCallResult(
                    call_id=call_id,
                    result=f"子 agent 正在执行中... (status={status}, turn={progress.get('turn', 0)})",
                )
        except Exception as e:
            return ToolCallResult(call_id=call_id, error=f"查询失败: {e}")
