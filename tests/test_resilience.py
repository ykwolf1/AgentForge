"""Resilience 测试 —— 熔断器状态机 + 重试 + 超时"""
import asyncio
import pytest
from agentforge.utils.resilience import CircuitBreaker, CircuitOpenError


class TestCircuitBreaker:
    def test_init_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        assert cb.state == "closed"
        assert cb.can_call() is True

    def test_open_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=60)
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert cb.can_call() is False

    def test_recovery_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        assert cb.state == "open"
        assert cb.can_call() is False
        asyncio.run(asyncio.sleep(0.15))
        # 冷却后进入 half-open，允许试探
        assert cb.can_call() is True
        assert cb.state == "half-open"

    def test_success_resets(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        assert cb._failures == 0

    def test_circuit_open_error(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60)
        cb.record_failure()
        assert cb.recovery_in() > 0


class TestResilientCall:
    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        from agentforge.utils.resilience import resilient_call

        call_count = 0

        @resilient_call(timeout=5, retries=0)
        async def good():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await good()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        from agentforge.utils.resilience import resilient_call

        call_count = 0

        @resilient_call(timeout=5, retries=2, backoff_base=0.01)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "recovered"

        result = await flaky()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_timeout(self):
        from agentforge.utils.resilience import resilient_call

        @resilient_call(timeout=0.05, retries=0)
        async def slow():
            await asyncio.sleep(1)
            return "should not reach"

        with pytest.raises(asyncio.TimeoutError):
            await slow()
