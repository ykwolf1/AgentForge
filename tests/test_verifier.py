"""Verifier 测试 —— 验证器 + Reflection 引擎"""
import asyncio
import pytest
from agentforge.agents.verifier import (
    ToolVerifier, VerifyResult,
    ReflectionEngine, ReflectionResult,
)


class TestToolVerifier:
    @pytest.mark.asyncio
    async def test_verify_pass(self):
        v = ToolVerifier()
        result = await v.verify("edit", {"file_path": "/tmp/test.py"}, "edited", {"edit": "echo OK"})
        assert result is not None
        assert result.passed is True
        assert "OK" in result.message

    @pytest.mark.asyncio
    async def test_verify_fail(self):
        v = ToolVerifier()
        result = await v.verify("edit", {"file_path": "/tmp/test.py"}, "edited",
                                {"edit": "python -c 'import nonexistent_module_xyz'"})
        assert result is not None
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_verify_no_config(self):
        v = ToolVerifier()
        result = await v.verify("bash", {}, "output", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_verify_tool_not_in_config(self):
        v = ToolVerifier()
        result = await v.verify("bash", {}, "output", {"edit": "echo OK"})
        assert result is None  # bash 没配验证命令

    @pytest.mark.asyncio
    async def test_verify_timeout(self):
        v = ToolVerifier()
        result = await v.verify("edit", {}, "output", {"edit": "sleep 100"})
        assert result.passed is False
        assert "超时" in result.message


class TestReflectionEngine:
    def test_init(self):
        engine = ReflectionEngine(llm_client=None, max_reflections=3)
        assert engine.max == 3
        assert engine.count == 0
        assert engine.exhausted is False

    def test_exhausted(self):
        engine = ReflectionEngine(llm_client=None, max_reflections=2)
        engine._count = 2
        assert engine.exhausted is True

    def test_reset(self):
        engine = ReflectionEngine(llm_client=None, max_reflections=2)
        engine._count = 2
        engine.reset()
        assert engine.count == 0
        assert engine.exhausted is False

    @pytest.mark.asyncio
    async def test_reflect_no_llm(self):
        engine = ReflectionEngine(llm_client=None)
        result = await engine.reflect("task", "result")
        assert result is None

    @pytest.mark.asyncio
    async def test_reflect_no_result(self):
        engine = ReflectionEngine(llm_client=object())
        result = await engine.reflect("task", "")
        assert result is None
