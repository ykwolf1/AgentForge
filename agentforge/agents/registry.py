# registry.py 核心流程：Agent 注册中心
#
#   管理所有 agent 的元信息（名字/版本/角色/能力/状态/标签）。
#   启动时自动注册配置里的 agent；运行时可查询、搜索。
#   API 暴露 /agent/registry 端点。
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class AgentRecord:
    """注册中心里的一条 agent 记录"""
    name: str                                    # 唯一标识
    version: str = "1.0.0"                       # 语义版本
    role: str = "worker"                         # coordinator | worker | custom
    capabilities: List[str] = field(default_factory=list)   # ["coding", "debugging", "review"]
    description: str = ""
    model: str = ""                              # 用什么模型
    allowed_tools: List[str] = field(default_factory=list)
    status: str = "active"                       # active | inactive | deprecated
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "role": self.role,
            "capabilities": self.capabilities,
            "description": self.description,
            "model": self.model,
            "allowed_tools": self.allowed_tools,
            "status": self.status,
            "created_at": self.created_at,
            "tags": self.tags,
        }


class AgentRegistry:
    """Agent 注册中心。管理所有 agent 的元信息。

    用途：
    - 启动时自动注册配置里的 agent
    - 运行时查询"谁能干什么"（按 capability 搜索）
    - spawn_agent 时从 registry 发现合适的 agent 配置
    - API 暴露 /agent/registry 查看所有 agent
    """

    def __init__(self):
        self._records: Dict[str, AgentRecord] = {}

    def register(self, record: AgentRecord) -> None:
        """注册一个 agent。同名覆盖。"""
        self._records[record.name] = record
        logger.info(f"Registry: 注册 agent '{record.name}' (role={record.role}, caps={record.capabilities})")

    def register_from_config(self, agent_config) -> None:
        """从 AgentConfig 自动提取信息注册。"""
        caps = []
        # 从 system_prompt 推断能力（简单关键词匹配）
        prompt = (getattr(agent_config, "system_prompt", "") or "").lower()
        if any(k in prompt for k in ["代码", "编程", "code", "program", "edit", "fix", "bug"]):
            caps.append("coding")
        if any(k in prompt for k in ["审查", "review", "检查", "check"]):
            caps.append("review")
        if any(k in prompt for k in ["规划", "plan", "coordinator", "分发", "委派", "delegate"]):
            caps.append("planning")
        if any(k in prompt for k in ["搜索", "search", "研究", "research", "分析", "analyze"]):
            caps.append("research")
        if not caps:
            caps = ["general"]

        record = AgentRecord(
            name=agent_config.agent_name,
            version="1.0.0",
            role=getattr(agent_config, "role", "worker"),
            capabilities=caps,
            description=(agent_config.system_prompt or "")[:200],
            model=getattr(agent_config.model, "model_name", "") if hasattr(agent_config, "model") else "",
            allowed_tools=getattr(agent_config, "allowed_tools", None) or [],
            status="active",
        )
        self.register(record)

    def unregister(self, name: str) -> bool:
        return self._records.pop(name, None) is not None

    def get(self, name: str) -> Optional[AgentRecord]:
        return self._records.get(name)

    def list_all(self) -> List[AgentRecord]:
        return list(self._records.values())

    def find(self, capability: str = "", role: str = "") -> List[AgentRecord]:
        """按能力/角色搜索 agent。"""
        results = []
        for r in self._records.values():
            if capability and capability not in r.capabilities:
                continue
            if role and r.role != role:
                continue
            results.append(r)
        return results

    def to_dict(self) -> dict:
        return {
            "total": len(self._records),
            "agents": [r.to_dict() for r in self._records.values()],
        }


# 全局单例
_registry: Optional[AgentRegistry] = None

def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry
