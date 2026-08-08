"""MemoryStore SQLite 测试 —— 存取 + checkpoint + 检索"""
import os
import pytest
from agentforge.memory.store import MemoryStore
from agentforge.llm.llm_basics import LLMMessage


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test_memory.db"
    return MemoryStore(str(db))


class TestMemoryStore:
    def test_save_and_load_session(self, store):
        history = [
            LLMMessage(role="system", content="你是助手"),
            LLMMessage(role="user", content="修 auth.py"),
            LLMMessage(role="assistant", content="好的"),
        ]
        store.save_session("s1", history, agent_name="planner")
        loaded = store.load_session("s1")
        assert len(loaded) == 3
        assert loaded[0].role == "system"
        assert loaded[1].content == "修 auth.py"
        assert loaded[2].role == "assistant"

    def test_load_nonexistent(self, store):
        assert store.load_session("nonexistent") == []

    def test_has_session(self, store):
        assert store.has_session("s1") is False
        store.save_session("s1", [LLMMessage(role="user", content="hi")])
        assert store.has_session("s1") is True

    def test_list_sessions(self, store):
        store.save_session("s1", [LLMMessage(role="user", content="a")])
        store.save_session("s2", [LLMMessage(role="user", content="b")])
        sessions = store.list_sessions()
        assert "s1" in sessions
        assert "s2" in sessions

    def test_overwrite_session(self, store):
        store.save_session("s1", [LLMMessage(role="user", content="old")])
        store.save_session("s1", [LLMMessage(role="user", content="new")])
        loaded = store.load_session("s1")
        assert len(loaded) == 1
        assert loaded[0].content == "new"

    def test_checkpoint_save_and_load(self, store):
        history = [LLMMessage(role="user", content="task"), LLMMessage(role="assistant", content="result")]
        store.save_checkpoint("s1", turn=3, history=history, handoff_depth=1)
        cp = store.load_latest_checkpoint("s1")
        assert cp is not None
        assert cp["turn"] == 3
        assert cp["handoff_depth"] == 1
        assert len(cp["history"]) == 2

    def test_checkpoint_none(self, store):
        assert store.load_latest_checkpoint("nonexistent") is None

    def test_checkpoint_list(self, store):
        store.save_checkpoint("s1", turn=3, history=[])
        store.save_checkpoint("s1", turn=6, history=[])
        turns = store.list_checkpoints("s1")
        assert turns == [3, 6]

    def test_search(self, store):
        history = [LLMMessage(role="user", content="fix auth.py bug in calculate")]
        store.archive_before_compact("s1", history)
        results = store.search("auth.py")
        assert len(results) >= 1
        assert "auth.py" in results[0]["content"]

    def test_tool_calls_serialized(self, store):
        from agentforge.llm.llm_basics import ToolCall
        history = [
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(call_id="tc1", name="bash", arguments={"command": "ls"})],
            ),
        ]
        store.save_session("s1", history)
        loaded = store.load_session("s1")
        assert loaded[0].tool_calls is not None
        assert loaded[0].tool_calls[0].name == "bash"
