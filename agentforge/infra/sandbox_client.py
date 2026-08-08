# sandbox_client.py 核心流程：沙箱代码执行客户端
#
#   替代 bash_tool 的直接执行——代码/命令走 Docker 沙箱隔离。
#   故障降级：sandbox 不可用时，bash_tool 走本地执行（现有逻辑）。
#
#   统一模式：enabled + health_check + available + 降级
import aiohttp
from typing import Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class SandboxClient:
    """沙箱客户端：通过 HTTP 调 sandbox 的 /execute 接口执行代码。
    如果连不上，available=False，调用方降级到本地 bash。"""

    def __init__(self, config: dict):
        self._enabled = config.get("enabled", False)
        # 兼容两种配置字段名：endpoint（InfraManager 用的）和 url（旧写法）
        self._url = (config.get("endpoint") or config.get("url") or "http://localhost:8000").rstrip("/")
        self._healthy = False
        self._timeout = config.get("timeout", 30)

    async def health_check(self) -> bool:
        """检查 sandbox 是否可用。"""
        if not self._enabled:
            return False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        self._healthy = True
                        logger.info(f"[Infra/Sandbox] 健康检查通过: {self._url}")
                        return True
        except Exception as e:
            logger.warning(f"[Infra/Sandbox] 不可用（降级到本地 bash）: {e}")
        self._healthy = False
        return False

    @property
    def available(self) -> bool:
        return self._enabled and self._healthy

    async def execute(self, code: str, language: str = "python") -> dict:
        """在沙箱里执行代码。返回 {success, stdout, stderr, exit_code}。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._url}/execute",
                    json={"code": code, "language": language},
                    timeout=aiohttp.ClientTimeout(total=self._timeout)
                ) as resp:
                    result = await resp.json()
                    return {
                        "success": result.get("success", False),
                        "stdout": result.get("stdout", ""),
                        "stderr": result.get("stderr", ""),
                        "exit_code": result.get("exit_code", -1),
                    }
        except Exception as e:
            logger.warning(f"[Infra/Sandbox] 执行失败（降级）: {e}")
            return {"success": False, "stdout": "", "stderr": str(e), "exit_code": -1}

    async def execute_shell(self, command: str) -> dict:
        """在沙箱里执行 shell 命令（用 subprocess 包一层）。

        执行后自动同步沙箱工作目录的文件列表到结果里，
        让调用方知道沙箱里生成了哪些文件（解决沙箱/主机文件系统不通的问题）。
        """
        code = f"import subprocess; r = subprocess.run('''{command}''', shell=True, capture_output=True, text=True); print(r.stdout); import sys; sys.stderr.write(r.stderr)"
        result = await self.execute(code)

        # 执行后获取沙箱工作目录的文件列表（让 agent 知道沙箱里有什么文件）
        if result.get("success"):
            try:
                files = await self.list_files()
                result["sandbox_files"] = files
            except Exception:
                pass
        return result

    async def list_files(self) -> list:
        """列出沙箱工作目录的文件。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/files",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            return data
                        return data.get("files", [])
        except Exception:
            pass
        return []

    async def download_file(self, filename: str, local_path: str = "") -> str:
        """从沙箱下载文件到主机。"""
        if not local_path:
            import tempfile
            local_path = f"{tempfile.gettempdir()}/{filename}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/files/{filename}/content",
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(local_path, "wb") as f:
                            f.write(content)
                        return local_path
        except Exception as e:
            logger.warning(f"[Infra/Sandbox] 下载文件失败: {e}")
        return ""

    async def upload_file(self, local_path: str, filename: str = "") -> bool:
        """上传主机文件到沙箱。"""
        if not filename:
            from pathlib import Path
            filename = Path(local_path).name
        try:
            with open(local_path, "rb") as f:
                data = f.read()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self._url}/files/upload",
                    json={"filename": filename, "content": data.hex()},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.warning(f"[Infra/Sandbox] 上传文件失败: {e}")
        return False
