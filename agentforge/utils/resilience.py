# resilience.py 核心流程：统一的超时 + 重试 + 熔断机制
#
#   三类外部调用（LLM / MCP / 工具）共用这一套韧性机制：
#     ① CircuitBreaker —— 连续失败 N 次后熔断，冷却后恢复
#     ② resilient_call —— 装饰器：超时 + 指数退避重试 + 熔断检查
#
#   设计：装饰器/包装模式，不调时走原路径，零侵入
import asyncio
import functools
import time
from typing import Callable, Optional, Tuple, Type

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """熔断器开启时抛出，调用方应直接返回错误不重试。"""
    def __init__(self, name: str, recovery_in: float):
        self.name = name
        self.recovery_in = recovery_in
        super().__init__(f"Circuit '{name}' is OPEN, retry in {recovery_in:.0f}s")


class CircuitBreaker:
    """简单计数熔断器：连续失败 N 次后熔断，冷却时间后进入 half-open 允许试探一次。
    状态机：closed（正常）→ open（熔断）→ half-open（试探）→ closed/open"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._last_failure_time: float = 0.0
        self._state = "closed"

    @property
    def state(self) -> str:
        return self._state

    def can_call(self) -> bool:
        """熔断中返回 False（调用方应跳过直接返回错误）。"""
        if self._state == "open":
            elapsed = time.time() - self._last_failure_time
            if elapsed > self.recovery_timeout:
                self._state = "half-open"
                logger.debug(f"CircuitBreaker[{self.name}] open→half-open (试探)")
                return True
            return False
        return True

    def recovery_in(self) -> float:
        """距离恢复还有多少秒（用于错误信息）。"""
        if self._state != "open":
            return 0.0
        return max(0.0, self.recovery_timeout - (time.time() - self._last_failure_time))

    def record_success(self) -> None:
        if self._state != "closed":
            logger.debug(f"CircuitBreaker[{self.name}] {self._state}→closed (成功)")
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.time()
        if self._failures >= self.failure_threshold and self._state != "open":
            self._state = "open"
            logger.warning(
                f"CircuitBreaker[{self.name}] → OPEN "
                f"(连续失败 {self._failures} 次，冷却 {self.recovery_timeout}s)"
            )


# 全局熔断器注册表（按名字区分，如 "llm:Qwen" / "mcp:chart"）
_breakers: dict = {}

def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """获取或创建一个命名熔断器（同名复用，状态跨调用保持）。"""
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name, **kwargs)
    return _breakers[name]


def resilient_call(
    timeout: float = 60.0,
    retries: int = 3,
    backoff_base: float = 1.0,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    breaker: Optional[CircuitBreaker] = None,
):
    """异步调用装饰器：熔断检查 + 超时 + 指数退避重试。

    Args:
        timeout: 单次调用超时（秒）
        retries: 最大重试次数（不含首次）
        backoff_base: 退避基数（实际等待 backoff_base * 2^attempt）
        retry_on: 哪些异常触发重试
        breaker: 绑定的熔断器（None=不熔断）
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # ① 熔断检查
            if breaker and not breaker.can_call():
                raise CircuitOpenError(breaker.name, breaker.recovery_in())

            last_err: Optional[Exception] = None
            for attempt in range(retries + 1):
                try:
                    # ② 超时包裹
                    result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
                    # ④ 成功记入熔断器
                    if breaker:
                        breaker.record_success()
                    return result
                except asyncio.TimeoutError as e:
                    last_err = e
                    logger.warning(
                        f"[{func.__name__}] 超时 (attempt {attempt+1}/{retries+1}, "
                        f"timeout={timeout}s)"
                    )
                except retry_on as e:
                    last_err = e
                    # 不重试的异常直接抛
                    if attempt >= retries:
                        break
                    wait = backoff_base * (2 ** attempt)
                    logger.warning(
                        f"[{func.__name__}] 失败 (attempt {attempt+1}/{retries+1}): "
                        f"{e!r} → {wait:.1f}s 后重试"
                    )
                    await asyncio.sleep(wait)

            # ④ 失败记入熔断器
            if breaker:
                breaker.record_failure()
            raise last_err if last_err else RuntimeError("resilient_call exhausted")

        return wrapper
    return decorator
