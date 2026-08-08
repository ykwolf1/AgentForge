# redis_client.py 核心流程：Redis 客户端（任务队列 + 状态持久化）
#
#   TaskManager 从"内存存储"升级到"Redis 持久化"。
#   故障降级：Redis 不可用时，TaskManager 走内存模式（现有逻辑）。
import json
from typing import Optional, Any

try:
    import redis.asyncio as aioredis
except Exception:
    aioredis = None

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class RedisClient:
    """Redis 客户端：任务队列持久化 + 会话状态外部存储。
    连不上时 available=False，调用方降级到内存。"""

    def __init__(self, config: dict):
        self._enabled = config.get("enabled", False)
        self._host = config.get("host", "localhost")
        self._port = config.get("port", 6379)
        self._password = config.get("password", "")
        self._db = config.get("db", 0)
        self._healthy = False
        self._client = None

    async def health_check(self) -> bool:
        if not self._enabled or aioredis is None:
            return False
        try:
            self._client = aioredis.Redis(
                host=self._host, port=self._port,
                password=self._password or None,
                db=self._db, decode_responses=True,
            )
            await self._client.ping()
            self._healthy = True
            logger.info(f"[Infra/Redis] 健康检查通过: {self._host}:{self._port}")
            return True
        except Exception as e:
            logger.warning(f"[Infra/Redis] 不可用（降级到内存）: {e}")
            self._healthy = False
            return False

    @property
    def available(self) -> bool:
        return self._enabled and self._healthy and self._client is not None

    async def set(self, key: str, value: Any, ttl: int = 0) -> None:
        """存值（自动 JSON 序列化）。失败时标记不健康 + 自动降级。"""
        if not self.available:
            return
        try:
            data = json.dumps(value, ensure_ascii=False, default=str)
            if ttl > 0:
                await self._client.set(key, data, ex=ttl)
            else:
                await self._client.set(key, data)
        except Exception as e:
            logger.warning(f"[Infra/Redis] set 失败（降级到内存）: {e}")
            self._healthy = False  # 标记不健康，后续操作直接降级

    async def get(self, key: str) -> Optional[Any]:
        """取值（自动 JSON 反序列化）。失败时降级。"""
        if not self.available:
            return None
        try:
            data = await self._client.get(key)
        except Exception as e:
            logger.warning(f"[Infra/Redis] get 失败（降级）: {e}")
            self._healthy = False
            return None
        if data is None:
            return None
        try:
            return json.loads(data)
        except Exception:
            return data

    async def delete(self, key: str) -> None:
        if not self.available:
            return
        await self._client.delete(key)

    async def hset(self, name: str, key: str, value: Any) -> None:
        """Hash 操作：存任务状态。"""
        if not self.available:
            return
        await self._client.hset(name, key, json.dumps(value, ensure_ascii=False, default=str))

    async def hget(self, name: str, key: str) -> Optional[Any]:
        """Hash 操作：取任务状态。"""
        if not self.available:
            return None
        data = await self._client.hget(name, key)
        if data is None:
            return None
        try:
            return json.loads(data)
        except Exception:
            return data

    async def hgetall(self, name: str) -> dict:
        """Hash 操作：取所有。"""
        if not self.available:
            return {}
        data = await self._client.hgetall(name)
        result = {}
        for k, v in data.items():
            try:
                result[k] = json.loads(v)
            except Exception:
                result[k] = v
        return result

    async def close(self) -> None:
        if self._client:
            await self._client.close()
