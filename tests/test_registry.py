"""Agent Registry 注册中心测试"""
import pytest
from agentforge.agents.registry import AgentRegistry, AgentRecord


class TestAgentRegistry:
    def setup_method(self):
        self.reg = AgentRegistry()

    def test_register_and_get(self):
        r = AgentRecord(name="coder", role="worker", capabilities=["coding"])
        self.reg.register(r)
        assert self.reg.get("coder") is not None
        assert self.reg.get("coder").role == "worker"

    def test_get_not_found(self):
        assert self.reg.get("nonexistent") is None

    def test_list_all(self):
        self.reg.register(AgentRecord(name="a"))
        self.reg.register(AgentRecord(name="b"))
        assert len(self.reg.list_all()) == 2

    def test_find_by_capability(self):
        self.reg.register(AgentRecord(name="coder", capabilities=["coding", "debugging"]))
        self.reg.register(AgentRecord(name="reviewer", capabilities=["review"]))
        results = self.reg.find(capability="coding")
        assert len(results) == 1
        assert results[0].name == "coder"

    def test_find_by_role(self):
        self.reg.register(AgentRecord(name="planner", role="coordinator"))
        self.reg.register(AgentRecord(name="coder", role="worker"))
        results = self.reg.find(role="coordinator")
        assert len(results) == 1
        assert results[0].name == "planner"

    def test_unregister(self):
        self.reg.register(AgentRecord(name="temp"))
        assert self.reg.unregister("temp") is True
        assert self.reg.get("temp") is None
        assert self.reg.unregister("temp") is False

    def test_overwrite_on_same_name(self):
        self.reg.register(AgentRecord(name="agent1", version="1.0"))
        self.reg.register(AgentRecord(name="agent1", version="2.0"))
        assert self.reg.get("agent1").version == "2.0"

    def test_to_dict(self):
        self.reg.register(AgentRecord(name="agent1"))
        d = self.reg.to_dict()
        assert d["total"] == 1
        assert d["agents"][0]["name"] == "agent1"

    def test_register_from_config_coding(self):
        """从 AgentConfig 推断能力"""
        class FakeConfig:
            agent_name = "test_coder"
            role = "worker"
            system_prompt = "你是程序员，负责写代码和修复 bug"
            model = type("M", (), {"model_name": "test-model"})()
            allowed_tools = ["bash", "edit"]
        self.reg.register_from_config(FakeConfig())
        r = self.reg.get("test_coder")
        assert r is not None
        assert "coding" in r.capabilities

    def test_register_from_config_review(self):
        class FakeConfig:
            agent_name = "test_reviewer"
            role = "worker"
            system_prompt = "你是代码审查员，负责 review"
            model = type("M", (), {"model_name": "test"})()
            allowed_tools = []
        self.reg.register_from_config(FakeConfig())
        r = self.reg.get("test_reviewer")
        assert "review" in r.capabilities
