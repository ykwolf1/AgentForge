# llm_client.py 核心流程：统一用 UnifiedAdapter，转发流式调用
#
#   不再按 provider 分 openai/anthropic 两套 adapter。
#   UnifiedAdapter 一套代码对接所有 OpenAI 兼容模型（DeepSeek/Qwen/ModelScope/GLM/OpenAI）。
#   旧的 provider="openai"/"anthropic" 配置仍然兼容（内部统一转 UnifiedAdapter）。
#
#   代码位置：
#     _build_adapter    llm_client.py:37
#     astream_response  llm_client.py:55
from __future__ import annotations

from typing import AsyncGenerator, Dict, Generator, List, Protocol, cast

from agentforge.config.config import AgentConfig
from agentforge.llm.llm_basics import LLMResponse

from .adapters.unified_adapter import UnifiedAdapter
from .llm_events import ResponseEvent


class ProviderAdapter(Protocol):
    def generate_response(self, messages: List[Dict[str, str]], **params) -> LLMResponse: ...
    def stream_response(self, messages: List[Dict[str, str]], **params) -> Generator[ResponseEvent, None, None]: ...
    async def agenerate_response(self, messages: List[Dict[str, str]], **params) -> LLMResponse: ...
    async def astream_response(self, messages: List[Dict[str, str]], **params) -> AsyncGenerator[ResponseEvent, None]: ...

class LLMClient:
    def __init__(self, cfg: AgentConfig) -> None:
        self.cfg = cfg
        self._adapter: ProviderAdapter = self._build_adapter(self.cfg)

    @staticmethod
    def _build_adapter(cfg: AgentConfig) -> ProviderAdapter:
        # 统一用 UnifiedAdapter——不再分 openai/anthropic
        # 旧的 provider 值（openai/anthropic/compatible）全部兼容，内部统一处理
        impl = UnifiedAdapter(
            api_key=cfg.model.api_key,
            base_url=cfg.model.base_url,
            default_model=cfg.model.model_name or "",
            wire_api=cfg.wire_api or "chat",
        )
        return cast(ProviderAdapter, impl)

    # 同步，非流式
    def generate_response(self, messages: List[Dict[str, str]], **params) -> LLMResponse:
        return self._adapter.generate_response(messages, **params)

    # 同步，流式
    def stream_response(self, messages: List[Dict[str, str]], **params) -> Generator[ResponseEvent, None, None]:
        yield ResponseEvent(type="error", data="sync stream not implemented")

    # 异步，非流式
    async def agenerate_response(self, messages: List[Dict[str, str]], **params) -> LLMResponse:
        return await self._adapter.agenerate_response(messages, **params)

    # 异步，流式
    async def astream_response(self, messages: List[Dict[str, str]], **params) -> AsyncGenerator[ResponseEvent, None]:
        stream = cast(AsyncGenerator[ResponseEvent, None], self._adapter.astream_response(messages, **params))
        async for ch in stream:
            yield ch
