# unified_adapter.py —— 统一 LLM 适配器（一个 adapter 两条路）
#
#   对外：一套接口（astream_response / agenerate_response），agent 不用关心底层
#   对内：按 wire_api 自动选 SDK 和协议
#     wire_api="chat"      → openai SDK，chat.completions（DeepSeek/Qwen/ModelScope/z.ai paas）
#     wire_api="responses" → openai SDK，/responses（OpenAI o 系列/codex）
#     wire_api="messages"  → anthropic SDK，/messages（Claude/z.ai anthropic/GLM anthropic）
#
#   合并了旧 openai_adapter + anthropic_adapter 的全部能力，消除了两套代码的维护负担。
#   旧 provider 值（openai/anthropic/compatible）全部兼容，内部统一处理。
from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator, Dict, Generator, List, Optional, cast

from agentforge.llm.llm_basics import LLMResponse
from agentforge.llm.llm_events import ResponseEvent

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


def _to_chat_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """统一消息格式 → OpenAI chat 格式。"""
    converted = []
    for msg in messages:
        role = msg.get("role")
        item: Dict[str, Any] = {"role": role}
        if "content" in msg:
            item["content"] = msg["content"]
        if role == "assistant" and "tool_calls" in msg:
            item["tool_calls"] = msg["tool_calls"]
        if role == "tool" and "tool_call_id" in msg:
            item["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            item["name"] = msg["name"]
        converted.append(item)
    return converted


def _to_anthropic_messages(messages: List[Dict[str, Any]]):
    """统一消息格式 → Anthropic messages 格式（system 单独提取）。"""
    system = ""
    content: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        msg_content = m.get("content", "")
        if role == "system":
            system += (msg_content + "\n")
        elif role == "user":
            content.append({"role": "user", "content": msg_content})
        elif role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                assistant_content = []
                if msg_content:
                    assistant_content.append({"type": "text", "text": msg_content})
                for tc in tool_calls:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tc.get("call_id", ""),
                        "name": tc.get("name", ""),
                        "input": tc.get("arguments", {})
                    })
                content.append({"role": "assistant", "content": assistant_content})
            else:
                content.append({"role": "assistant", "content": msg_content})
        elif role == "tool":
            content.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": msg_content
            }]})

    return system.strip(), content


class UnifiedAdapter:
    """统一 LLM 适配器：一个类对接所有模型。

    按 wire_api 自动选协议路径：
    - chat / responses → OpenAI SDK
    - messages → Anthropic SDK

    韧性：SDK 内置 max_retries=3 + timeout=60s + 熔断器（5 次失败熔断 90s）
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: str = "",
        wire_api: str = "chat",
    ):
        self._api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or ""
        self._base_url = base_url
        self._default_model = default_model
        self._wire_api = wire_api or "chat"

        # 按需初始化 SDK（不用的不初始化，省内存）
        self._openai_async = None
        self._openai_sync = None
        self._anthropic_async = None
        self._anthropic_sync = None

        # 熔断器
        from agentforge.utils.resilience import get_breaker
        self._breaker = get_breaker(f"llm:{default_model}", failure_threshold=5, recovery_timeout=90)

    def _get_openai_async(self):
        if self._openai_async is None:
            from openai import AsyncOpenAI
            self._openai_async = AsyncOpenAI(
                api_key=self._api_key, base_url=self._base_url,
                max_retries=3, timeout=60.0,
            )
        return self._openai_async

    def _get_openai_sync(self):
        if self._openai_sync is None:
            from openai import OpenAI
            self._openai_sync = OpenAI(
                api_key=self._api_key, base_url=self._base_url,
                max_retries=3, timeout=60.0,
            )
        return self._openai_sync

    def _get_anthropic_async(self):
        if self._anthropic_async is None:
            from anthropic import AsyncAnthropic
            self._anthropic_async = AsyncAnthropic(
                api_key=self._api_key, base_url=self._base_url,
                max_retries=3, timeout=60.0,
            )
        return self._anthropic_async

    def _get_anthropic_sync(self):
        if self._anthropic_sync is None:
            from anthropic import Anthropic
            self._anthropic_sync = Anthropic(
                api_key=self._api_key, base_url=self._base_url,
                max_retries=3, timeout=60.0,
            )
        return self._anthropic_sync

    def _pick_api(self, override: Optional[str]) -> str:
        if override in ("responses", "chat", "messages"):
            return override
        return self._wire_api

    def _build_anthropic_kwargs(self, messages, model, params):
        system, msg = _to_anthropic_messages(messages)
        kwargs = {
            "model": model,
            "max_tokens": params.get("max_tokens", 4096),
            "messages": msg,
            **{k: v for k, v in params.items() if k not in ("model", "max_tokens", "api", "tools")},
        }
        if system:
            kwargs["system"] = system
        return kwargs

    # ===== 异步流式（agent 主循环用）=====

    async def astream_response(self, messages: List[Dict[str, Any]], **params) -> AsyncGenerator[ResponseEvent, None]:
        api_choice = self._pick_api(params.get("api"))
        model = params.get("model", self._default_model)
        call_params = {k: v for k, v in params.items() if k not in ("model", "api")}

        try:
            if api_choice == "messages":
                async for evt in self._anthropic_stream(messages, model, call_params):
                    yield evt
            elif api_choice == "responses":
                async for evt in self._openai_responses_stream(messages, model, call_params):
                    yield evt
            else:
                async for evt in self._openai_chat_stream(messages, model, call_params):
                    yield evt
        except Exception as e:
            yield ResponseEvent.error(str(e), {"exception_type": type(e).__name__})

    # ===== OpenAI chat 协议 =====

    async def _openai_chat_stream(self, messages, model, params) -> AsyncGenerator[ResponseEvent, None]:
        client = self._get_openai_async()
        chat_msgs = _to_chat_messages(messages)
        # stream_options={"include_usage": True} 让 API 在流式模式下也返回 token 用量
        stream = await client.chat.completions.create(
            model=model, messages=chat_msgs, stream=True,
            stream_options={"include_usage": True},
            **params,
        )
        yield ResponseEvent.request_started({})
        tool_calls: dict[int, dict] = {}
        text_buffer: str = ""
        final_usage = None  # 保存最后一个 chunk 的 usage

        async for chunk in stream:
            # 最后一个 chunk 的 choices 可能为空，但带着 usage
            if chunk.usage:
                final_usage = chunk.usage

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            for tc_delta in delta.tool_calls or []:
                idx = tc_delta.index
                data = tool_calls.setdefault(idx, {"call_id": "", "name": "", "arguments": "", "type": ""})
                data["type"] = tc_delta.type or data["type"]
                data["call_id"] = tc_delta.id or data["call_id"]
                if tc_delta.function:
                    data["name"] = tc_delta.function.name or data["name"]
                    data["arguments"] += tc_delta.function.arguments or ""
                    yield ResponseEvent.tool_call_delta(data["call_id"], data["name"], tc_delta.function.arguments or "", data["type"])

            if delta.content:
                text_buffer += delta.content
                yield ResponseEvent.assistant_delta(delta.content)

            finish_reason = chunk.choices[0].finish_reason
            if finish_reason == "tool_calls":
                for tc in tool_calls.values():
                    try:
                        tc["arguments"] = json.loads(tc["arguments"])
                    except Exception:
                        tc["arguments"] = {}
                yield ResponseEvent.tool_call_ready(list(tool_calls.values()))

            if finish_reason is not None:
                # token 用量从 final_usage 取（流式模式下最后一个 chunk 才有）
                if final_usage:
                    yield ResponseEvent.token_usage({
                        "input_tokens": getattr(final_usage, "prompt_tokens", 0) or 0,
                        "output_tokens": getattr(final_usage, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(final_usage, "total_tokens", 0) or 0,
                    })
                payload = {"content": text_buffer, "finish_reason": finish_reason, "usage": final_usage or {}}
                yield ResponseEvent.response_finished(payload)

    # ===== OpenAI responses 协议 =====

    async def _openai_responses_stream(self, messages, model, params) -> AsyncGenerator[ResponseEvent, None]:
        client = self._get_openai_async()
        stream = await client.responses.create(model=model, input=messages, stream=True, **params)
        async for event in stream:
            if event.type == "response.created":
                yield ResponseEvent.request_started({"response_id": event.response.id})
            elif event.type == "response.failed":
                yield ResponseEvent.error(getattr(event, "error", "response failed"))
            elif event.type == "response.output_item.done":
                yield ResponseEvent.tool_call_ready(event.item)
            elif event.type == "response.output_text.delta":
                yield ResponseEvent.assistant_delta(event.delta)
            elif event.type == "response.reasoning_text.delta":
                yield ResponseEvent.reasoning_delta(event.delta)
            elif event.type == "response.reasoning_summary_text.delta":
                yield ResponseEvent.reasoning_finished(event.delta)
            elif event.type == "response.completed":
                resp_usage = event.response.usage
                yield ResponseEvent.token_usage({
                    "input_tokens": resp_usage.input_tokens if resp_usage else 0,
                    "output_tokens": resp_usage.output_tokens if resp_usage else 0,
                    "total_tokens": resp_usage.total_tokens if resp_usage else 0,
                })
                yield ResponseEvent.response_finished(event.response)
                break
            elif event.type == "error":
                yield ResponseEvent.error(getattr(event, "error", "") or "error")
                break

    # ===== Anthropic messages 协议 =====

    async def _anthropic_stream(self, messages, model, params) -> AsyncGenerator[ResponseEvent, None]:
        client = self._get_anthropic_async()
        kwargs = self._build_anthropic_kwargs(messages, model, params)
        input_tokens_from_start = None

        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "message_start":
                    message = getattr(event, "message", None)
                    data = {}
                    if message:
                        mid = getattr(message, "id", "")
                        if mid:
                            data["response_id"] = mid
                        usage = getattr(message, "usage", None)
                        if usage:
                            input_tokens_from_start = getattr(usage, "input_tokens", None)
                    yield ResponseEvent.request_started(data)

                elif event.type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", None) == "tool_use":
                        self._current_tool_call = {
                            "call_id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "arguments": "",
                        }

                elif event.type == "content_block_delta":
                    delta = event.delta
                    delta_type = getattr(delta, "type", None)
                    if delta_type == "text_delta":
                        text = getattr(delta, "text", "")
                        if text:
                            yield ResponseEvent.assistant_delta(text)
                    elif delta_type == "input_json_delta":
                        partial = getattr(delta, "partial_json", "")
                        if partial and hasattr(self, "_current_tool_call"):
                            self._current_tool_call["arguments"] += partial
                            yield ResponseEvent.tool_call_delta(
                                self._current_tool_call["call_id"],
                                self._current_tool_call["name"],
                                partial, "function"
                            )
                    elif delta_type == "thinking_delta":
                        thinking = getattr(delta, "thinking", "")
                        if thinking:
                            yield ResponseEvent.reasoning_delta(thinking)

                elif event.type == "content_block_stop":
                    if hasattr(self, "_current_tool_call") and self._current_tool_call:
                        tc = self._current_tool_call.copy()
                        try:
                            tc["arguments"] = json.loads(tc["arguments"]) if tc["arguments"] else {}
                        except json.JSONDecodeError:
                            tc["arguments"] = {}
                        delattr(self, "_current_tool_call")
                        yield ResponseEvent.tool_call_ready(tc)

                elif event.type == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage:
                        input_t = getattr(usage, "input_tokens", None)
                        if input_t is None and input_tokens_from_start is not None:
                            input_t = input_tokens_from_start
                        output_t = getattr(usage, "output_tokens", 0)
                        yield ResponseEvent.token_usage({
                            "input_tokens": input_t or 0,
                            "output_tokens": output_t or 0,
                            "total_tokens": (input_t or 0) + (output_t or 0),
                        })

                elif event.type == "message_stop":
                    yield ResponseEvent.response_finished({})
                    break

    # ===== 非流式 =====

    async def agenerate_response(self, messages: List[Dict[str, str]], **params) -> LLMResponse:
        model = params.get("model", self._default_model)
        call_params = {k: v for k, v in params.items() if k not in ("model", "api")}
        api_choice = self._pick_api(params.get("api"))
        try:
            if api_choice == "messages":
                client = self._get_anthropic_async()
                kwargs = self._build_anthropic_kwargs(messages, model, call_params)
                resp = await client.messages.create(**kwargs)
                text = resp.content[0].text if resp.content else ""
            elif api_choice == "responses":
                client = self._get_openai_async()
                resp = await client.responses.create(model=model, input=messages, **call_params)
                text = resp.output_text if hasattr(resp, "output_text") else ""
            else:
                client = self._get_openai_async()
                chat_msgs = _to_chat_messages(messages)
                resp = await client.chat.completions.create(model=model, messages=chat_msgs, **call_params)
                text = resp.choices[0].message.content if resp.choices else ""
            return LLMResponse(text)
        except Exception as e:
            return LLMResponse("", error=str(e))

    def generate_response(self, messages: List[Dict[str, str]], **params) -> LLMResponse:
        model = params.get("model", self._default_model)
        call_params = {k: v for k, v in params.items() if k not in ("model", "api")}
        api_choice = self._pick_api(params.get("api"))
        try:
            if api_choice == "messages":
                client = self._get_anthropic_sync()
                kwargs = self._build_anthropic_kwargs(messages, model, call_params)
                resp = client.messages.create(**kwargs)
                text = resp.content[0].text if resp.content else ""
            else:
                client = self._get_openai_sync()
                chat_msgs = _to_chat_messages(messages)
                resp = client.chat.completions.create(model=model, messages=chat_msgs, **call_params)
                text = resp.choices[0].message.content if resp.choices else ""
            return LLMResponse(text)
        except Exception as e:
            return LLMResponse("", error=str(e))
