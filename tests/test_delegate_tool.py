"""Delegate Tool 测试 —— handoff 信号编解码"""
import asyncio
import pytest
from agentforge.tools.collaboration.delegate import (
    DelegateTool, encode_handoff, decode_handoff, HandoffSignal,
    HANDOFF_PREFIX,
)


class TestHandoffCodec:
    def test_encode(self):
        sig = HandoffSignal(target_agent_name="coder", task_description="fix bug", reason="delegated")
        encoded = encode_handoff(sig)
        assert encoded.startswith(HANDOFF_PREFIX)
        assert "coder" in encoded
        assert "fix bug" in encoded

    def test_decode_valid(self):
        encoded = HANDOFF_PREFIX + '{"target": "coder", "task": "fix bug", "reason": "test"}'
        sig = decode_handoff(encoded)
        assert sig is not None
        assert sig.target_agent_name == "coder"
        assert sig.task_description == "fix bug"

    def test_decode_invalid(self):
        assert decode_handoff("normal text") is None
        assert decode_handoff("") is None
        assert decode_handoff(None) is None
        assert decode_handoff({"key": "value"}) is None  # 非 str

    def test_decode_corrupted(self):
        assert decode_handoff(HANDOFF_PREFIX + "not json") is None
        assert decode_handoff(HANDOFF_PREFIX + '{"missing": "fields"}') is None

    def test_roundtrip(self):
        original = HandoffSignal(target_agent_name="reviewer", task_description="审查代码", reason="delegate")
        encoded = encode_handoff(original)
        decoded = decode_handoff(encoded)
        assert decoded.target_agent_name == original.target_agent_name
        assert decoded.task_description == original.task_description


class TestDelegateTool:
    @pytest.mark.asyncio
    async def test_execute_returns_handoff(self):
        tool = DelegateTool()
        result = await tool.execute(target_agent="coder", task_description="fix auth.py")
        assert result.success is True
        assert result.result.startswith(HANDOFF_PREFIX)
        sig = decode_handoff(result.result)
        assert sig.target_agent_name == "coder"
        assert sig.task_description == "fix auth.py"

    @pytest.mark.asyncio
    async def test_execute_missing_target(self):
        tool = DelegateTool()
        result = await tool.execute(task_description="some task")
        assert result.success is False
        assert "target_agent" in (result.error or "")

    def test_build_openai_schema(self):
        tool = DelegateTool()
        schema = tool.build("openai")
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "delegate"
        assert "target_agent" in schema["function"]["parameters"]["properties"]

    def test_build_anthropic_schema(self):
        # 统一 adapter 后，所有 provider 输出同一种格式（OpenAI function calling）
        tool = DelegateTool()
        schema = tool.build("anthropic")
        assert schema["function"]["name"] == "delegate"
        assert "parameters" in schema["function"]
