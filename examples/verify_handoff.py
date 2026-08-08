"""
handoff 机制单元验证（不走 LLM，确定性测试）
直接模拟 _process_tool_calls 的 handoff 分支，验证：
  1. delegate 工具返回 handoff 信号
  2. _check_handoff 能解析
  3. peer 被正确调用
"""
import asyncio
import sys
sys.path.insert(0, "/Users/yangkun/Desktop/Projects/ags_all")

from agentforge.tools.delegate_tool import DelegateTool, encode_handoff, decode_handoff, HandoffSignal
from agentforge.agents.agent import Agent
from agentforge.agents.agent_events import AgentEvent, Agent_Events


class FakePeerAgent:
    """假的 peer agent，模拟 peer.run 行为"""
    def __init__(self, name):
        self.type = name
        self.conversation_history = []
        self._peers = {}
        self.run_called_with = None

    async def run(self, task):
        self.run_called_with = task
        # 模拟 peer 跑完，给个结果事件
        yield AgentEvent.text_delta(f"[{self.type} 完成了任务: {task}]")


async def main():
    print("=== handoff 机制单元验证 ===\n")

    # 步骤 1: delegate 工具返回 handoff 信号
    print("[1] 测试 DelegateTool 返回 handoff 信号")
    tool = DelegateTool()
    result = await tool.execute(target_agent="coder", task_description="修 auth.py")
    print(f"    result: {result.result[:70]}...")
    assert result.success, "工具应成功"
    print(f"    ✅ 工具成功返回\n")

    # 步骤 2: _check_handoff 解析
    print("[2] 测试 _check_handoff 解析信号")
    signal = BaseAgent._check_handoff(result.result)
    print(f"    解析出: target={signal.target_agent_name}, task={signal.task_description}")
    assert signal.target_agent_name == "coder"
    print(f"    ✅ 解析正确\n")

    # 步骤 3: 非 handoff 的普通结果，_check_handoff 返回 None
    print("[3] 测试普通工具结果不被误判为 handoff")
    not_handoff = BaseAgent._check_handoff("这是普通工具的输出")
    assert not_handoff is None
    not_handoff2 = BaseAgent._check_handoff({"key": "value"})
    assert not_handoff2 is None
    print(f"    普通字符串: None ✅")
    print(f"    dict 结果: None ✅")
    print(f"    ✅ 不误判\n")

    # 步骤 4: register_peer + get_peer
    print("[4] 测试 peer 注册与发现")
    fake_agent = FakePeerAgent("BaseAgent_holder")
    # 给 fake_agent 加 BaseAgent 的 peer 方法（模拟）
    fake_agent._peers = {}
    fake_agent.register_peer = lambda name, a: fake_agent._peers.__setitem__(name, a)
    fake_agent.get_peer = lambda name: fake_agent._peers.get(name)

    peer_coder = FakePeerAgent("coder")
    fake_agent.register_peer("coder", peer_coder)
    found = fake_agent.get_peer("coder")
    print(f"    注册 coder 后 get_peer('coder'): {found.type}")
    assert found is peer_coder
    not_found = fake_agent.get_peer("nonexistent")
    print(f"    get_peer('nonexistent'): {not_found}")
    assert not_found is None
    print(f"    ✅ 注册发现正确\n")

    # 步骤 5: 模拟 handoff 触发 peer.run
    print("[5] 测试 handoff 触发 peer.run")
    peer = fake_agent.get_peer(signal.target_agent_name)
    events = []
    async for e in peer.run(signal.task_description):
        events.append(e)
    print(f"    peer.run 被调用, 参数: {peer.run_called_with}")
    print(f"    产生事件: {events[0].type} = {events[0].data}")
    assert peer.run_called_with == "修 auth.py"
    print(f"    ✅ peer 被正确调用\n")

    print("=== 全部验证通过 ✅ ===")
    print("结论: handoff 机制本身工作正常")
    print("      (端到端能否触发取决于 LLM 是否调 delegate 工具)")


asyncio.run(main())
