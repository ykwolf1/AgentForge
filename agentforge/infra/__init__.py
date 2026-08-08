# __init__.py 核心流程：基础设施统一入口
#
#   InfraManager 统一管理外部服务（Sandbox / Redis / Milvus 知识库）。
#   启动时统一健康检查，不通的服务自动降级。
from typing import Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class InfraManager:
    """基础设施管理器。统一初始化 + 健康检查 + 服务注入。"""

    def __init__(self, config: dict):
        self._config = config or {}
        self.sandbox = None
        self.redis = None
        self.minio = None  # MinIO 对象存储
        self.kb = None  # KnowledgeBase（Milvus + Embedding）

    async def health_check_all(self) -> dict:
        """统一健康检查。返回各服务状态 dict。"""
        results = {}

        # Sandbox
        sb_config = self._config.get("sandbox", {})
        if sb_config.get("enabled"):
            from .sandbox_client import SandboxClient
            self.sandbox = SandboxClient(sb_config)
            ok = await self.sandbox.health_check()
            results["sandbox"] = "OK" if ok else "FALLBACK"
        else:
            results["sandbox"] = "DISABLED"

        # Redis
        rd_config = self._config.get("redis", {})
        if rd_config.get("enabled"):
            from .redis_client import RedisClient
            self.redis = RedisClient(rd_config)
            ok = await self.redis.health_check()
            results["redis"] = "OK" if ok else "FALLBACK"
        else:
            results["redis"] = "DISABLED"

        # MinIO 对象存储
        mn_config = self._config.get("minio", {})
        if mn_config.get("enabled"):
            from .minio_client import MinIOClient
            self.minio = MinIOClient(mn_config)
            ok = self.minio.health_check()
            results["minio"] = "OK" if ok else "FALLBACK"
        else:
            results["minio"] = "DISABLED"

        # Knowledge Base (Milvus + Embedding)
        milvus_config = self._config.get("milvus", {})
        embedding_config = self._config.get("embedding", {})
        if milvus_config.get("enabled") or embedding_config.get("model_path"):
            from agentforge.knowledge import get_kb
            self.kb = get_kb()
            self.kb.setup({
                "milvus": milvus_config,
                "embedding": embedding_config,
            })
            await self.kb.init()
            results["milvus"] = "OK" if (self.kb.milvus and self.kb.milvus.available) else "FALLBACK"
        else:
            results["milvus"] = "DISABLED"

        logger.info(f"[Infra] 基础设施状态: {results}")
        return results

    def status(self) -> dict:
        return {
            "sandbox": self.sandbox.available if self.sandbox else False,
            "redis": self.redis.available if self.redis else False,
            "milvus": (self.kb and self.kb.milvus and self.kb.milvus.available) if self.kb else False,
            "minio": self.minio.available if self.minio else False,
        }

    async def recheck_health(self) -> dict:
        """重新健康检查——让中途挂掉但已恢复的服务能重新接入。

        生产场景：Milvus 被重启了、Redis 网络闪断了——
        调这个方法重新探测，恢复的服务重新标记为 healthy。
        """
        results = {}
        if self.redis and not self.redis.available:
            results["redis"] = await self.redis.health_check()
        if self.sandbox and not self.sandbox.available:
            results["sandbox"] = await self.sandbox.health_check()
        if self.minio and not self.minio.available:
            results["minio"] = self.minio.health_check()
        if self.kb and self.kb.milvus and not self.kb.milvus.available:
            results["milvus"] = await self.kb.milvus.health_check()
        return results

    async def close(self) -> None:
        if self.redis:
            await self.redis.close()
