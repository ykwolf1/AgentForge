# shared_state.py 核心流程：团队级共享状态
#
#   多 agent 协作时，所有 agent 持有同一个 SharedState 引用：
#     - conversation_history  共享一份上下文（Swarm 共享状态派）
#     - trajectory_recorder   共享一份轨迹（避免 handoff 后断裂）
#     - peers                 团队成员表（替代每个 agent 自己的 _peers）
#
#   单 agent 时 AgentManager 建一个 SharedState，agent 持有它——退化正常。
#
#   关键：所有 agent 读写的是同一份 list/recorder 对象，不是 copy。
#        这样 coordinator 改了 history，worker 立刻能看到。
from typing import Dict, List, TYPE_CHECKING

from agentforge.llm.llm_basics import LLMMessage
from agentforge.utils.trajectory_recorder import TrajectoryRecorder

if TYPE_CHECKING:
    from agentforge.agents.agent import Agent


class SharedState:
    """团队级共享状态。所有 agent 持有同一引用，读写同一份数据。"""

    def __init__(self) -> None:
        self.conversation_history: List[LLMMessage] = []
        self.trajectory_recorder: TrajectoryRecorder = TrajectoryRecorder()
        self.peers: Dict[str, "Agent"] = {}   # 团队成员表 {name: Agent}
        self._handoff_depth: int = 0           # handoff 深度（防 A→B→A→B 无限循环）
        self.session_id: str = ""              # 会话标识（持久化 key）
        self.token_usage: dict = {"input": 0, "output": 0, "total": 0}  # 费用统计
        self.budget: dict = {                  # 预算治理（0=无限制）
            "max_tokens": 0,
            "max_tool_calls": 0,
            "max_cost": 0.0,
            "tool_calls": 0,                   # 已用工具调用次数
        }
        # 并行 delegate：后台 peer 任务 + 结果收集
        self.background_peers: list = []       # [asyncio.Task, ...] 后台 peer task 引用
        self.peer_results_queue = None          # asyncio.Queue（延迟初始化，首次用时建）
        # 四层记忆：工作记忆 Redis 后端（None = 内存模式）
        self.working_memory = None              # WorkingMemory 实例（可选）

    def register_peer(self, name: str, agent: "Agent") -> None:
        """注册一个 agent 到团队成员表。所有 agent 共享这份表。"""
        self.peers[name] = agent

    def get_peer(self, name: str):
        return self.peers.get(name)
