# handoff.py 核心流程：实现 Swarm 式 agent 转交
#
#   LLM 调 delegate(target_agent, task_description)
#      ↓
#   DelegateTool.execute 返回带 __HANDOFF__ 标记的特殊字符串
#      ↓
#   主循环 _process_tool_calls 检测到标记 → 解析 → 嵌套调用 peer.run
#
#   设计要点：
#     - handoff 信号藏在工具返回的 result 字符串里（用特殊前缀标记）
#     - 不改 ToolCallResult 结构，对 ToolManager 透明
#     - LLM 看到的 result 是"已转交给 X"的提示，下轮它能继续
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agentforge.llm.llm_basics import ToolCallResult
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.tools.tool_manager import register_tool


# handoff 信号前缀，主循环靠它识别
HANDOFF_PREFIX = "__HANDOFF__:"


@dataclass
class HandoffSignal:
    """handoff 信号 —— 主循环解析出来后用于切换 agent"""
    target_agent_name: str
    task_description: str
    reason: str = ""


def encode_handoff(signal: HandoffSignal) -> str:
    """把 HandoffSignal 编码成带前缀的字符串，藏进 tool result"""
    return HANDOFF_PREFIX + json.dumps({
        "target": signal.target_agent_name,
        "task": signal.task_description,
        "reason": signal.reason,
    }, ensure_ascii=False)


def decode_handoff(result: Any) -> Optional[HandoffSignal]:
    """从 tool result 里检测并解析 handoff 信号。不是 handoff 返回 None。"""
    if not isinstance(result, str):
        return None
    if not result.startswith(HANDOFF_PREFIX):
        return None
    try:
        payload = json.loads(result[len(HANDOFF_PREFIX):])
        return HandoffSignal(
            target_agent_name=payload["target"],
            task_description=payload["task"],
            reason=payload.get("reason", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return None


@register_tool(name="delegate", providers="*")
class DelegateTool(BaseTool):
    """
    delegate 工具 —— 让 LLM 触发 agent 转交。
    LLM 调 delegate(target_agent="coder", task_description="修复 auth.py 的 bug")
    → 工具返回 handoff 信号 → 主循环切换到 coder agent 执行任务
    """
    name = "delegate"
    display_name = "Delegate to Agent"
    description = (
        "Delegate a subtask to another agent. Use this when the task requires "
        "a different agent's capability (e.g. coding, review). "
        "The target agent will execute the task and return the result."
    )
    parameter_schema = {
        "type": "object",
        "properties": {
            "target_agent": {
                "type": "string",
                "description": "Name of the target agent to delegate to (e.g. 'coder', 'reviewer')"
            },
            "task_description": {
                "type": "string",
                "description": "Clear description of the subtask to delegate"
            }
        },
        "required": ["target_agent", "task_description"]
    }
    risk_level = ToolRiskLevel.SAFE  # 委派本身不危险，被委派的 agent 干活时才走自己的审批

    async def execute(self, **kwargs) -> ToolCallResult:
        target = kwargs.get("target_agent", "")
        task = kwargs.get("task_description", "")
        if not target:
            return ToolCallResult(call_id="", error="target_agent is required")
        # 返回 handoff 信号字符串（主循环会识别并处理）
        signal = encode_handoff(HandoffSignal(
            target_agent_name=target,
            task_description=task,
            reason=f"delegated by delegate tool",
        ))
        return ToolCallResult(call_id="", result=signal)
