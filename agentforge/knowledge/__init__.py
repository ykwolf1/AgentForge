# knowledge/__init__.py —— 知识库模块入口
#
#   KnowledgeBase：统一管理 MilvusStore + EmbeddingEngine。
#   由 InfraManager 启动时初始化，注入到工具层。

class KnowledgeBase:
    """知识库：MilvusStore + EmbeddingEngine 的统一封装。"""

    def __init__(self):
        self.milvus = None        # MilvusStore
        self.embedding = None     # EmbeddingEngine
        self._config = None

    def setup(self, config: dict):
        """设置配置（延迟初始化，首次用时才加载模型）。"""
        self._config = config

    @property
    def available(self) -> bool:
        return self.milvus is not None and self.milvus.available

    async def init(self):
        """初始化 Milvus + Embedding（启动时调）。"""
        if not self._config:
            return

        # 加载 Embedding Engine
        emb_config = self._config.get("embedding", {})
        if emb_config.get("model_path"):
            from .embedding import EmbeddingEngine
            self.embedding = EmbeddingEngine(
                model_path=emb_config.get("model_path"),
                reranker_path=emb_config.get("reranker_path"),
                server_url=emb_config.get("server_url"),
            )
            # 如果配了 server_url，检查 HTTP 服务是否可用
            if emb_config.get("server_url"):
                await self.embedding.health_check()

        # 初始化 Milvus
        milvus_config = self._config.get("milvus", {})
        if milvus_config.get("enabled"):
            from .milvus_store import MilvusStore
            self.milvus = MilvusStore(milvus_config, self.embedding)
            await self.milvus.health_check()


# 全局单例（工具层通过 _get_kb() 访问）
_kb_instance = None

def get_kb() -> "KnowledgeBase":
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance
