# base_tool.py 核心流程：所有工具的父类，定义风险等级 + 结果契约
#
#   工具子类继承 BaseTool，声明自己的 risk_level：
#      SAFE   → 永不弹审批（read_file/glob/ls 这类）
#      MEDIUM/HIGH → 按权限档决定是否问用户（bash/write_file/edit）
#      ↓
#   tool.execute(**args) → 返回 ToolCallResult
#      ToolCallResult.success 是派生属性 = (error is None)
#
#   关键：
#     - risk_level 决定审批门怎么对待这个工具
#     - build() 按 provider 返回不同 schema（claude 一种、openai 一种），作为 API 的 tools= 字段
#     - ToolManager.execute 返回 (success, result)，error 文本被丢弃——真实 error 留在 ToolCallResult
#
#   代码位置：
#     ToolRiskLevel        base_tool.py:8
#     BaseTool.execute     base_tool.py  （子类实现）
#     ToolCallResult       llm_basics.py:50  （success 是派生属性）
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict, Mapping, Optional

from agentforge.llm.llm_basics import ToolCallConfirmationDetails, ToolCallResult


class ToolRiskLevel(Enum):
    """Tool risk levels for permission control."""
    SAFE = "safe"           # 只读操作，自动执行
    LOW = "low"             # 低风险操作，简单确认
    MEDIUM = "medium"       # 中等风险，详细确认
    HIGH = "high"           # 高风险操作，强制确认

class BaseTool(ABC):
    name: str = ""
    display_name: str = ""
    description: str = ""
    parameter_schema: Dict[str, Any] = {}
    risk_level: ToolRiskLevel = ToolRiskLevel.SAFE
    
    @abstractmethod
    async def execute(self, **kwargs) -> ToolCallResult:
        """Execute the tool."""
        pass
    
    def validate_parameters(self, **kwargs) -> bool:
        """Validate tool parameters."""
        return True
    
    def get_risk_level(self, **kwargs) -> ToolRiskLevel:
        """Get the risk level for this tool call."""
        return self.risk_level

    def is_risky(self, **kwargs) -> bool:
        """Determine if this tool call is risky and needs approval."""
        return self.get_risk_level(**kwargs) != ToolRiskLevel.SAFE

    async def get_confirmation_details(self, **kwargs) -> Optional[ToolCallConfirmationDetails]:
        """Get details for user confirmation."""
        risk_level = self.get_risk_level(**kwargs)
        if risk_level == ToolRiskLevel.SAFE:
            return None

        confirmation_message = await self._generate_confirmation_message(**kwargs)

        return ToolCallConfirmationDetails(
            type="exec",
            message=confirmation_message,
            is_risky=risk_level in [ToolRiskLevel.MEDIUM, ToolRiskLevel.HIGH],
            metadata={
                "tool_name": self.name,
                "parameters": kwargs,
                "risk_level": risk_level.value
            }
        )

    async def _generate_confirmation_message(self, **kwargs) -> str:
        """Generate detailed confirmation message. Override in subclasses."""
        return f"Execute {self.display_name}: {kwargs}"
    
    def get_function_declaration(self) -> Dict[str, Any]:
        """Get function declaration for LLM."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameter_schema
        }

    def build(self, provider: str = "", func_type: str = "") -> Mapping[str, Any]:
        """生成 LLM 可消费的工具声明。

        统一输出 OpenAI function calling 格式（底层全走 OpenAI 兼容 SDK）。
        provider 参数保留兼容但不影响输出格式——所有 OpenAI 兼容模型用同一种格式。
        """
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameter_schema}}

Tool = BaseTool
