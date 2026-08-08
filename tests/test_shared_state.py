"""SharedState 测试 —— 共享状态 + 并行收集 + handoff depth"""
import pytest
from agentforge.agents.shared_state import SharedState
from agentforge.llm.llm_basics import LLMMessage


class TestSharedState:
    def test_init(self):
        s = SharedState()
        assert s.conversation_history == []
        assert s.peers == {}
        assert s._handoff_depth == 0
        assert s.session_id == ""
        assert s.token_usage == {"input": 0, "output": 0, "total": 0}
        assert s.budget["tool_calls"] == 0
        assert s.background_peers == []

    def test_register_peer(self):
        s = SharedState()
        s.register_peer("coder", object())
        assert "coder" in s.peers

    def test_get_peer_not_found(self):
        s = SharedState()
        assert s.get_peer("nonexistent") is None

    def test_conversation_history_shared(self):
        """两个引用同一 SharedState 的 agent 看到同一份 history"""
        s = SharedState()
        s.conversation_history.append(LLMMessage(role="user", content="hello"))
        # 模拟另一个 agent 通过属性访问
        assert len(s.conversation_history) == 1
        assert s.conversation_history[0].content == "hello"

    def test_budget_defaults(self):
        s = SharedState()
        assert s.budget["max_tokens"] == 0  # 0=无限制
        assert s.budget["max_tool_calls"] == 0
        assert s.budget["max_cost"] == 0.0

    def test_token_usage_accumulate(self):
        s = SharedState()
        s.token_usage["total"] += 100
        s.token_usage["total"] += 50
        assert s.token_usage["total"] == 150

    def test_handoff_depth(self):
        s = SharedState()
        s._handoff_depth += 1
        assert s._handoff_depth == 1
        s._handoff_depth -= 1
        assert s._handoff_depth == 0
