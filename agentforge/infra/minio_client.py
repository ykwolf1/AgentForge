# minio_client.py 核心流程：MinIO 对象存储客户端
#
#   存储知识库原始文档（md/pdf/docx/html），API 模式下多 worker 共享。
#   不可用时降级到本地文件系统（现有行为）。
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from minio import Minio
except Exception:
    Minio = None

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class MinIOClient:
    """MinIO 对象存储：文档上传/下载。不可用时降级到本地文件。"""

    def __init__(self, config: dict):
        self._enabled = config.get("enabled", False)
        self._endpoint = config.get("endpoint", "localhost:9002")
        self._access_key = config.get("access_key", "admin")
        self._secret_key = config.get("secret_key", "admin123")
        self._bucket = config.get("bucket", "agentforge")
        self._secure = config.get("secure", False)
        self._client = None
        self._healthy = False

    def health_check(self) -> bool:
        """检查 MinIO 是否可用。"""
        if not self._enabled or Minio is None:
            return False
        try:
            self._client = Minio(
                self._endpoint,
                access_key=self._access_key,
                secret_key=self._secret_key,
                secure=self._secure,
            )
            # 确保 bucket 存在
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                logger.info(f"[Infra/MinIO] 创建 bucket: {self._bucket}")
            self._healthy = True
            logger.info(f"[Infra/MinIO] 健康检查通过: {self._endpoint}, bucket={self._bucket}")
            return True
        except Exception as e:
            logger.warning(f"[Infra/MinIO] 不可用（降级到本地文件）: {e}")
            self._healthy = False
            return False

    @property
    def available(self) -> bool:
        return self._enabled and self._healthy and self._client is not None

    def upload(self, file_path: str, object_name: Optional[str] = None) -> str:
        """上传文件到 MinIO。返回 object_name（或本地路径作为降级）。"""
        if not self.available:
            return file_path  # 降级：返回本地路径

        from io import BytesIO
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        if object_name is None:
            # 用日期 + 文件名组织路径
            date_str = datetime.now().strftime("%Y%m%d")
            object_name = f"documents/{date_str}/{p.name}"

        with open(file_path, "rb") as f:
            data = f.read()
        self._client.put_object(
            self._bucket,
            object_name,
            BytesIO(data),
            length=len(data),
            content_type=self._guess_content_type(p.suffix),
        )
        logger.info(f"[MinIO] 已上传: {object_name} ({len(data)} bytes)")
        return object_name

    def download(self, object_name: str, local_path: Optional[str] = None) -> str:
        """从 MinIO 下载文件。返回本地路径。"""
        if not self.available:
            return object_name  # 降级：认为 object_name 就是本地路径

        if local_path is None:
            local_path = f"/tmp/agentforge_download_{Path(object_name).name}"

        response = self._client.get_object(self._bucket, object_name)
        with open(local_path, "wb") as f:
            for d in response.stream(32 * 1024):
                f.write(d)
        logger.info(f"[MinIO] 已下载: {object_name} → {local_path}")
        return local_path

    def list_documents(self, prefix: str = "documents/") -> list:
        """列出知识库中的文档。"""
        if not self.available:
            return []
        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        return [
            {
                "name": obj.object_name,
                "size": obj.size,
                "modified": str(obj.last_modified),
            }
            for obj in objects
        ]

    def delete(self, object_name: str) -> bool:
        """删除文档。"""
        if not self.available:
            return False
        self._client.remove_object(self._bucket, object_name)
        return True

    @staticmethod
    def _guess_content_type(ext: str) -> str:
        return {
            ".md": "text/markdown",
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".html": "text/html",
        }.get(ext.lower(), "application/octet-stream")
