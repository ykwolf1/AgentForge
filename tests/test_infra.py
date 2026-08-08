"""Infra 基础设施测试 —— InfraManager + 降级"""
import asyncio
import pytest
from agentforge.infra import InfraManager
from agentforge.infra.sandbox_client import SandboxClient
from agentforge.infra.redis_client import RedisClient
from agentforge.infra.vector_store import VectorStore


class TestSandboxClient:
    def test_disabled(self):
        c = SandboxClient({"enabled": False})
        assert c.available is False

    def test_health_check_disabled(self):
        c = SandboxClient({"enabled": False})
        result = asyncio.run(c.health_check())
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_unreachable(self):
        c = SandboxClient({"enabled": True, "url": "http://localhost:9999"})
        result = await c.health_check()
        assert result is False
        assert c.available is False


class TestRedisClient:
    def test_disabled(self):
        c = RedisClient({"enabled": False})
        assert c.available is False

    @pytest.mark.asyncio
    async def test_health_check_unreachable(self):
        c = RedisClient({"enabled": True, "host": "localhost", "port": 9999})
        result = await c.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_no_password(self):
        c = RedisClient({"enabled": True, "host": "localhost", "port": 6379, "password": "wrong"})
        result = await c.health_check()
        assert result is False


class TestVectorStore:
    def test_disabled(self):
        c = VectorStore({"enabled": False})
        assert c.available is False

    def test_store_unavailable(self):
        c = VectorStore({"enabled": False})
        assert c.store("text") is False

    def test_search_unavailable(self):
        c = VectorStore({"enabled": False})
        assert c.search("query") == []


class TestInfraManager:
    @pytest.mark.asyncio
    async def test_all_disabled(self):
        mgr = InfraManager({})
        results = await mgr.health_check_all()
        assert results["sandbox"] == "DISABLED"
        assert results["redis"] == "DISABLED"
        assert results["milvus"] == "DISABLED"
        assert results["minio"] == "DISABLED"

    def test_status_all_off(self):
        mgr = InfraManager({})
        assert mgr.status() == {"sandbox": False, "redis": False, "milvus": False, "minio": False}

    @pytest.mark.asyncio
    async def test_graceful_degradation(self):
        """服务不可用时不崩，降级"""
        mgr = InfraManager({
            "sandbox": {"enabled": True, "url": "http://localhost:9999"},
            "redis": {"enabled": True, "host": "localhost", "port": 9999},
            "milvus": {"enabled": True, "host": "localhost", "port": 9999},
        })
        results = await mgr.health_check_all()
        assert results["sandbox"] == "FALLBACK"
        assert results["redis"] == "FALLBACK"
        assert mgr.status() == {"sandbox": False, "redis": False, "milvus": False, "minio": False}
