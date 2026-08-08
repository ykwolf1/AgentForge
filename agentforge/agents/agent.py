# agent.py 核心流程：通用 agent 内核（唯一的主循环）
#
#   run(user_msg)
#      ↓ 装配 history = [system, cwd, env, project, skills, user_msg]
#   ┌─→ while turn < max_turns:
#   │     ↓
#   │   _process_turn_stream()
#   │     ├─ messages = history 转换
#   │     ├─ tools = list_for_provider(provider, allowlist).build()  ← 用 agent_config 过滤
#   │     ├─ llm_client.astream_response(messages, tools, api=wire_api)  ← 不再硬编码
#   │     │    流式事件分流：ASSISTANT_DELTA / TOOL_CALL_READY / RESPONSE_FINISHED
#   │     │
#   │   TOOL_CALL_READY → _process_tool_calls
#   │     tool_mgr.execute（四道关卡）→ 结果/失败回灌 history
#   │     含 handoff 分支：delegate 工具返回信号 → 嵌套 peer.run
#   └── 回 while 顶部
#
#   关键：
#     - 后端靠 LLMClient+adapter 切（openai/anthropic），本类不感知
#     - 领域靠 agent_config.system_prompt + allowed_tools 切（code agent 是一种配置）
#     - 失败也回灌，不抛异常（撞墙回头）
#     - 到 max_turns 静默退出
import asyncio
import json
import platform
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Mapping

from agentforge.agents.agent_events import AgentEvent
from agentforge.config.config import AgentConfig
from agentforge.config.manager import ConfigManager
from agentforge.config.token_limits import TokenLimits
from agentforge.llm.llm_basics import LLMMessage, LLMResponse, ToolCall
from agentforge.llm.llm_client import LLMClient
from agentforge.llm.llm_events import LLM_Events
from agentforge.memory.memory_monitor import MemoryMonitor
from agentforge.tools.mcp_tool import sync_mcp_servers
from agentforge.tools.tool_manager import ToolManager
from agentforge.utils.session_stats import session_stats
from agentforge.utils.trajectory_recorder import TrajectoryRecorder

from agentforge.cli.cli_console import CLIConsole


# 默认 system prompt（agent_config.system_prompt 为空时用）
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant with tool-use capabilities.\n\n"
    "IMPORTANT rules for using tools:\n"
    "1. You have access to a set of tools (including any MCP-provided tools like chart generators). "
    "ALWAYS prefer calling a provided tool over writing your own code to do the same thing. "
    "For example, if a `generate_bar_chart` tool exists, call it instead of writing matplotlib code.\n"
    "2. To call a tool, emit a proper tool_call (function call) — do NOT write tool names as plain text or code blocks.\n"
    "3. After a tool returns its result, read the result and continue the task based on it.\n"
    "4. When the task is fully complete, respond with a concise summary WITHOUT calling any tool.\n"
    "5. If a tool fails, the error will be fed back to you — adjust and try a different approach.\n"
)


class Agent:
    """通用 agent 内核：循环 + 四道关卡工具执行 + handoff。
    后端靠 llm_client（adapter）切，领域靠 role/system_prompt 切。"""

    def __init__(
        self,
        name: str,
        agent_config: AgentConfig,
        config_mgr: ConfigManager,
        cli: CLIConsole,
        tool_mgr: ToolManager,
        shared_state: "SharedState | None" = None,
    ) -> None:
        self.name = name
        self.type = name                       # 兼容旧代码里用 self.type 的地方
        self.agent_config = agent_config
        self.config_mgr = config_mgr
        self.cli = cli
        self.tool_mgr = tool_mgr

        # 关键：用传入的 agent_config 建 LLMClient，不再用全局 active
        self.llm_client = LLMClient(agent_config)
        self.role = agent_config.role
        self.allowed_tools = agent_config.allowed_tools     # None = 全部工具

        # 共享状态：多 agent 时所有 agent 持有同一 SharedState 引用
        # 单 agent 时（shared_state=None）自建一个，退化正常
        from agentforge.agents.shared_state import SharedState
        self._shared: SharedState = shared_state if shared_state is not None else SharedState()

        self.max_turns = config_mgr.get_app_config().max_turns
        self.current_turn_index = 0
        self._consecutive_errors = 0  # 连续错误计数（连续 3 轮出错终止）
        self._history_initialized = False

        # MCP
        self._mcp_mgr = None
        self._mcp_init_lock = asyncio.Lock()  # 🟢17 修复：在 __init__ 创建，不在 setup 里重复创建

        # Budget Manager：从配置读预算上限
        budget_cfg = getattr(agent_config, "budget", None) or {}
        if isinstance(budget_cfg, dict):
            self._shared.budget["max_tokens"] = budget_cfg.get("max_tokens", 0)
            self._shared.budget["max_tool_calls"] = budget_cfg.get("max_tool_calls", 0)
            self._shared.budget["max_cost"] = budget_cfg.get("max_cost", 0.0)

        # DLP 敏感信息检测
        from agentforge.tools.dlp import DLPDetector
        self._dlp = DLPDetector()
        self._dlp_enabled = getattr(agent_config, "enable_dlp", True)

        # 验证器 + Reflection（闭环后半圈）
        from agentforge.agents.verifier import ToolVerifier, ReflectionEngine
        self._verifier = ToolVerifier()
        self._reflection = ReflectionEngine(
            self.llm_client,
            max_reflections=getattr(agent_config, "max_reflections", 2),
        ) if getattr(agent_config, "enable_reflection", False) else None

        # 缓存 prompt
        self.skills_prompt = config_mgr.get_skills_prompt()
        self.project_prompt = config_mgr.get_project_prompt()

    # ===== 共享状态 property（指向 SharedState，所有 agent 读写同一份）=====
    @property
    def conversation_history(self) -> List[LLMMessage]:
        return self._shared.conversation_history

    @conversation_history.setter
    def conversation_history(self, value: List[LLMMessage]) -> None:
        # 原地替换内容，保持共享引用不断（context_compact 用）
        self._shared.conversation_history.clear()
        if value:
            self._shared.conversation_history.extend(value)

    @property
    def trajectory_recorder(self) -> TrajectoryRecorder:
        return self._shared.trajectory_recorder

    @property
    def _peers(self) -> dict:
        return self._shared.peers

    # ===== handoff 支持 =====
    def register_peer(self, name: str, agent: "Agent") -> None:
        """注册另一个 agent 到团队成员表（共享）。"""
        self._shared.register_peer(name, agent)

    def get_peer(self, name: str):
        return self._shared.get_peer(name)

    @staticmethod
    def _check_handoff(result):
        from agentforge.tools.collaboration.delegate import decode_handoff
        return decode_handoff(result)

    # ===== 压缩（原地改，保持共享引用不断）=====
    async def context_compact(self, mem: MemoryMonitor, turn: int) -> None:
        history = self.conversation_history
        # token 估算：content + tool_calls 的 arguments
        tokens_used = 0
        for m in history:
            tokens_used += self.approx_token_count(m.content or "")
            if m.tool_calls:
                for tc in m.tool_calls:
                    tokens_used += self.approx_token_count(json.dumps(tc.arguments or {}, ensure_ascii=False))

        # 阻断3：压缩前抽出 ref_id 符号（压缩后回贴，防止卸载的工具结果变孤儿）
        import re
        ref_ids = []
        for m in history:
            if m.content:
                # 找所有 ref: ctx_xxx
                refs = re.findall(r'ref: (ctx_\w+)', m.content)
                ref_ids.extend(refs)

        used, summary = await mem.run_monitored(self.llm_client, self.cli, history, tokens_used, turn)
        if used > 0 and summary:
            # 阻断3：压缩后把 ref_id 回贴到 summary 末尾
            if ref_ids:
                unique_refs = list(dict.fromkeys(ref_ids))  # 去重保序
                ref_note = "\n\n[已卸载的工具结果引用] " + " | ".join(f"ref: {r}" for r in unique_refs[:20])
                summary = summary + ref_note

            # ⚠ 原地 clear + append，不能用 history = [...]（会断共享引用）
            history.clear()
            history.append(LLMMessage(role="user", content=summary))
            self.cli.set_current_tokens(used)

    async def _run_peer_background(self, peer, handoff, call_id: str):
        """后台运行 peer.run，完成后把结果推入 queue（不阻塞主循环）。"""
        try:
            async for _ in peer.run(handoff.task_description, fresh=False):
                pass  # peer 的事件不 yield 给主循环（并行模式下不交错）
            # peer 跑完，取最后一条 assistant 消息作为结果
            result_text = ""
            for msg in reversed(peer.conversation_history):
                if msg.role == "assistant" and msg.content and msg.content.strip():
                    result_text = msg.content
                    break
            if not result_text:
                result_text = f"(peer {handoff.target_agent_name} 完成，无输出)"
            # 推入 queue
            if self._shared.peer_results_queue is None:
                self._shared.peer_results_queue = asyncio.Queue()
            await self._shared.peer_results_queue.put({
                "peer_name": handoff.target_agent_name,
                "call_id": call_id,
                "result": result_text,
                "status": "completed",
            })
        except Exception as e:
            if self._shared.peer_results_queue is None:
                self._shared.peer_results_queue = asyncio.Queue()
            await self._shared.peer_results_queue.put({
                "peer_name": handoff.target_agent_name,
                "call_id": call_id,
                "result": str(e),
                "status": "failed",
            })
        finally:
            self._shared._handoff_depth -= 1

    async def _collect_peer_results(self) -> AsyncGenerator[AgentEvent, None]:
        """检查后台 peer 是否有完成的结果，有就 merge 到 history 并 yield 事件。"""
        if self._shared.peer_results_queue is None:
            return
        # 非阻塞检查 queue
        while not self._shared.peer_results_queue.empty():
            try:
                item = self._shared.peer_results_queue.get_nowait()
                peer_name = item.get("peer_name", "unknown")
                result = item.get("result", "")
                status = item.get("status", "completed")
                call_id = item.get("call_id", "")
                # merge 到 history
                self.conversation_history.append(
                    LLMMessage(role="tool", content=f"[{peer_name} 结果] {result}", tool_call_id=call_id)
                )
                if status == "completed":
                    yield AgentEvent.text_delta(f"\n[{peer_name} 已完成] {result[:100]}\n")
                else:
                    yield AgentEvent.text_delta(f"\n[{peer_name} 失败] {result[:100]}\n")
            except asyncio.QueueEmpty:
                break

    async def _await_all_peers(self) -> AsyncGenerator[AgentEvent, None]:
        """等待所有后台 peer 完成（在 task_complete 前调）。"""
        if not self._shared.background_peers:
            return
        # 等所有后台 task 完成
        pending = [t for t in self._shared.background_peers if not t.done()]
        if pending:
            yield AgentEvent.text_delta(f"\n等待 {len(pending)} 个后台任务完成...\n")
            await asyncio.gather(*pending, return_exceptions=True)
        # 收集所有剩余结果
        async for event in self._collect_peer_results():
            yield event
        self._shared.background_peers.clear()

    # 🟢16 修复：按模型查费率表（不再硬编码 GPT-3.5 费率）
    MODEL_PRICING = {
        # 模型名前缀 → 每千 token 美元
        "deepseek": 0.002,
        "gpt-4": 0.06,
        "gpt-5": 0.08,
        "claude-sonnet": 0.015,
        "claude-opus": 0.075,
        "claude-haiku": 0.001,
        "qwen": 0.004,
        "glm": 0.003,
    }

    def _get_model_price(self) -> float:
        """根据当前模型名查费率。"""
        model_name = (self.agent_config.model.model_name or "").lower()
        for prefix, price in self.MODEL_PRICING.items():
            if model_name.startswith(prefix):
                return price
        return 0.002  # 默认费率

    def _check_budget(self):
        """检查预算是否超限。超限返回原因字符串，没超返回 None。"""
        b = self._shared.budget
        usage = self._shared.token_usage
        if b["max_tokens"] > 0 and usage["total"] >= b["max_tokens"]:
            return f"token 预算耗尽（{usage['total']}/{b['max_tokens']}）"
        if b["max_tool_calls"] > 0 and b["tool_calls"] >= b["max_tool_calls"]:
            return f"工具调用上限（{b['tool_calls']}/{b['max_tool_calls']}）"
        if b["max_cost"] > 0:
            price = self._get_model_price()
            cost = usage["total"] / 1000 * price
            if cost >= b["max_cost"]:
                return f"费用超限（${cost:.4f}/${b['max_cost']:.4f}，费率 ${price}/1K）"
        return None

    def _save_checkpoint(self) -> None:
        """存当前状态快照到 SQLite（每 3 轮自动调一次）。"""
        sid = self._shared.session_id
        if not sid:
            return
        try:
            from agentforge.memory.store import MemoryStore, get_memory_store
            store = get_memory_store()
            store.save_checkpoint(
                session_id=sid,
                turn=self.current_turn_index,
                history=self.conversation_history,
                handoff_depth=self._shared._handoff_depth,
            )
        except Exception as e:
            try:
                from loguru import logger
                logger.warning(f"checkpoint 存盘失败（忽略）: {e}")
            except Exception:
                pass

    async def aclose(self) -> None:
        """资源清理：关 MCP server + 取消后台 peer task，避免泄漏。"""
        import contextlib
        # 🔴5 修复：取消所有后台 peer task（否则 agent 退出后孤儿 task 继续跑）
        if self._shared.background_peers:
            pending = [t for t in self._shared.background_peers if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                with contextlib.suppress(Exception):
                    await asyncio.gather(*pending, return_exceptions=True)
            self._shared.background_peers.clear()
        # 关 MCP server
        if self._mcp_mgr is not None:
            with contextlib.suppress(Exception):
                await self._mcp_mgr.close()
            self._mcp_mgr = None

    # ===== MCP =====
    async def setup_tools_mcp(self):
        import asyncio
        async with self._mcp_init_lock:  # 🟢17 修复：复用 __init__ 创建的 lock
            if self._mcp_mgr is not None:
                return
            try:
                mcp_mgr, _ = await sync_mcp_servers(cfg_mgr=self.config_mgr)
                self._mcp_mgr = mcp_mgr
            except Exception as e:
                self._mcp_mgr = None
                try:
                    from loguru import logger
                    logger.warning(f"MCP 同步失败（MCP 工具将不可用）: {e}")
                except Exception:
                    pass

    # ===== 主循环 =====
    async def run(self, user_message: str, fresh: bool = True) -> AsyncGenerator[AgentEvent, None]:
        """主循环入口。装配 history（仅 fresh=True 时），进 while。
        handoff 复用共享 history 时传 fresh=False，跳过装配避免重复。"""
        model_name = self.agent_config.model.model_name or ""
        max_tokens = TokenLimits.get_limit(self.role, model_name)
        if hasattr(self.cli, "set_max_context_tokens"):
            self.cli.set_max_context_tokens(max_tokens)
        await self.setup_tools_mcp()
        if fresh:
            self.current_turn_index = 0
            self._shared._handoff_depth = 0    # 新会话重置 handoff 深度
            if self._reflection:
                self._reflection.reset()        # 新任务重置反思计数
        session_stats.record_task_start(self.type)
        self.trajectory_recorder.start_recording(
            task=user_message,
            provider=self.agent_config.provider or "",
            model=model_name,
            max_steps=self.max_turns,
        )
        if fresh:
            yield AgentEvent.user_message(user_message, self.current_turn_index)

        # 装配 history（仅首次或新会话；handoff 复用共享 history 时跳过）
        if fresh or not self._history_initialized:
            restored = False
            sid = self._shared.session_id

            # 恢复优先级：① Redis 工作记忆（热数据）> ② checkpoint > ③ session > ④ 全新
            if fresh and sid:
                # ① 先试 Redis 工作记忆（跨请求复用，最快）
                if self._shared.working_memory:
                    redis_history = await self._shared.working_memory.load(sid)
                    if redis_history:
                        self._shared.conversation_history.clear()
                        self._shared.conversation_history.extend(redis_history)
                        self._history_initialized = True
                        restored = True
                        try:
                            from loguru import logger
                            logger.info(f"从 Redis 工作记忆恢复 {sid}: {len(redis_history)} 条")
                        except Exception:
                            pass
                if not restored:
                    try:
                      from agentforge.memory.store import MemoryStore, get_memory_store
                      store = get_memory_store()
                      # ① 先试 checkpoint（能恢复 turn_index + handoff_depth）
                      cp = store.load_latest_checkpoint(sid)
                      if cp:
                          self._shared.conversation_history.clear()
                          self._shared.conversation_history.extend(cp["history"])
                          self.current_turn_index = cp["turn"]
                          self._shared._handoff_depth = cp.get("handoff_depth", 0)
                          self._history_initialized = True
                          restored = True
                          try:
                              from loguru import logger
                              logger.info(f"从 checkpoint 恢复 {sid}: turn={cp['turn']}, {len(cp['history'])} 条")
                          except Exception:
                              pass
                      elif store.has_session(sid):
                          # ② fallback 到 session 级恢复
                          loaded = store.load_session(sid)
                          if loaded:
                              self._shared.conversation_history.clear()
                              self._shared.conversation_history.extend(loaded)
                              self._history_initialized = True
                              restored = True
                              try:
                                  from loguru import logger
                                  logger.info(f"从 session 恢复 {sid}: {len(loaded)} 条")
                              except Exception:
                                  pass
                    except Exception as e:
                        try:
                            from loguru import logger
                            logger.warning(f"恢复失败（忽略，走全新装配）: {e}")
                        except Exception:
                            pass

            if not restored:
                # 全新装配
                system_prompt = self.agent_config.system_prompt or self._build_default_system_prompt()
                self.conversation_history.append(LLMMessage(role="system", content=system_prompt))

                cwd_prompt = (
                    f"Please note the user launched the agent under the path {Path.cwd()}.\n"
                    "All subsequent file operations should be performed within this directory."
                )
                env_prompt = self._build_env_prompt()
                if self.project_prompt:
                    self.conversation_history.append(LLMMessage(role="user", content=self.project_prompt))
                self.conversation_history.append(LLMMessage(role="user", content=env_prompt + cwd_prompt))
                if self.skills_prompt:
                    self.conversation_history.append(LLMMessage(role="user", content=self.skills_prompt))
                # 长期记忆注入：L3 核心画像 + 按任务匹配的 L2 场景 + L1 事实
                try:
                    from agentforge.memory.longterm import get_longterm_memory
                    ltm = get_longterm_memory()
                    memory_prompt = ltm.recall(task_description=user_message[:200])
                    if memory_prompt:
                        self.conversation_history.append(LLMMessage(role="user", content=memory_prompt))
                except Exception:
                    pass
                self.conversation_history.append(LLMMessage(role="user", content=user_message))
                self._history_initialized = True
            else:
                # 恢复成功：只 append 新的 user_message
                self.conversation_history.append(LLMMessage(role="user", content=user_message))

        else:
            # handoff 场景：peer 接手时只 append 任务描述，不重复装 system/env
            self.conversation_history.append(LLMMessage(role="user", content=user_message))

        while self.current_turn_index < self.max_turns:
            try:
                async for event in self._process_turn_stream():
                    yield event
            except Exception as e:
                # 🔴3 修复：单轮异常不死会话——回灌错误让 LLM 自纠正，turn 照常推进
                self.current_turn_index += 1
                self._consecutive_errors += 1
                error_msg = f"[系统错误] {type(e).__name__}: {str(e)[:200]}"
                self.conversation_history.append(LLMMessage(role="user", content=error_msg))
                yield AgentEvent.error(error_msg)
                try:
                    from loguru import logger
                    logger.error(f"[Agent] 第 {self.current_turn_index} 轮异常: {e}")
                except Exception:
                    pass
                # 连续 3 轮出错就终止，不再死磕
                if self._consecutive_errors >= 3:
                    yield AgentEvent.task_complete(f"连续 {self._consecutive_errors} 轮出错，终止任务")
                    return
            else:
                self._consecutive_errors = 0  # 成功一轮就重置计数
            # 🟡5 修复：checkpoint 在工具结果回灌后再存（不在 RESPONSE_FINISHED 时存）
            # 避免快照里有"要调工具但没结果"的半残消息
            if self.current_turn_index > 0 and self.current_turn_index % 3 == 0:
                self._save_checkpoint()
            # 每轮结束后检查上下文是否需要压缩
            await self._check_context_compact()

        # 🟡7 修复：max_turns 到了要告诉上层"撞墙了"，不是静默退出
        if self.current_turn_index >= self.max_turns:
            yield AgentEvent.turn_max_reached(self.max_turns)

    def _build_default_system_prompt(self) -> str:
        """agent_config.system_prompt 为空时的默认 prompt。
        角色（coder/reviewer）的复杂 prompt 应通过 agent_config.system_prompt 配置传入。"""
        return DEFAULT_SYSTEM_PROMPT

    async def _check_context_compact(self) -> None:
        """每轮结束后检查上下文是否需要压缩 + 定期提取长期记忆。

        这是 agent 内核的压缩入口，所有运行模式（Interactive / Headless）都走。
        修复之前"只有 InteractiveSession 才压缩"的缺陷。
        压缩逻辑委托给 MemoryMonitor（如果可用）。
        同时每 5 轮从对话中提取 L1 原子事实到长期记忆。

        🟡10 修复：如果有后台 peer 正在跑，跳过本次压缩——
        否则 history.clear() 会清掉 peer 正在读写的数据。
        """
        # 并发安全：后台 peer 跑的时候不压缩（避免 history.clear 和 peer append 竞争）
        if self._shared.background_peers:
            pending = [t for t in self._shared.background_peers if not t.done()]
            if pending:
                try:
                    from loguru import logger
                    logger.debug(f"[Agent] 跳过压缩（{len(pending)} 个后台 peer 正在跑）")
                except Exception:
                    pass
                return  # 等 peer 跑完再压缩

        try:
            from agentforge.memory.memory_monitor import MemoryMonitor
            from agentforge.config.manager import ConfigManager
            if not hasattr(self, "_mem_monitor") or self._mem_monitor is None:
                cfg_mgr = ConfigManager()
                self._mem_monitor = MemoryMonitor(cfg_mgr)
            await self.context_compact(self._mem_monitor, turn=self.current_turn_index)
        except Exception:
            pass  # 压缩失败不影响 agent 正常运行

        # 每 5 轮提取 L1 原子事实（长期记忆沉淀）
        if self.current_turn_index > 0 and self.current_turn_index % 5 == 0:
            try:
                from agentforge.memory.longterm import get_longterm_memory
                ltm = get_longterm_memory()
                # 取最近几轮的对话文本
                recent = self.conversation_history[-10:]
                conv_text = "\n".join(
                    f"{m.role}: {m.content[:200]}" for m in recent if m.content
                )
                if conv_text.strip():
                    await ltm.extract_facts_from_conversation(
                        conv_text, self._shared.session_id, self.llm_client
                    )
            except Exception:
                pass

    async def _process_turn_stream(self) -> AsyncGenerator[AgentEvent, None]:
        messages = [self._convert_single_message(msg) for msg in self.conversation_history]
        trajectory_msg = self.conversation_history.copy()
        # 工具过滤 + schema 缓存：allowed_tools 白名单是主要过滤手段。
        # schema 在单次 run() 里不变，第一次构建后缓存（不再每轮重建 50 次）。
        from agentforge.tools.tool_manager import TOOL_REGISTRY
        avail = [
            e.instance for e in TOOL_REGISTRY.values()
            if e.enabled and (self.allowed_tools is None or e.instance.name in self.allowed_tools)
        ]
        if not hasattr(self, '_cached_tools_schema') or self._cached_tools_schema is None:
            schema_provider = self.agent_config.provider or "unified"
            self._cached_tools_schema = [t.build(schema_provider) for t in avail]
        tools = self._cached_tools_schema
        completed_resp: LLMResponse = LLMResponse(content="")

        tokens_used = sum(self.approx_token_count(m.content or "") for m in self.conversation_history)
        if hasattr(self.cli, "set_current_tokens"):
            self.cli.set_current_tokens(tokens_used)

        # 可观测性：记录 LLM 调用开始时间（算延迟用）
        import time as _time
        _llm_start = _time.time()
        _turn_tool_calls = []  # 本轮工具调用记录

        # 关键改动③：api 从 agent_config.wire_api 拿，不再硬编码 "chat"
        api = self.agent_config.wire_api or "chat"
        async for event in self.llm_client.astream_response(messages=messages, tools=tools, api=api):
            if event.type == LLM_Events.REQUEST_STARTED:
                yield AgentEvent.llm_stream_start()
            elif event.type == LLM_Events.ASSISTANT_DELTA:
                yield AgentEvent.text_delta(str(event.data))
            elif event.type == LLM_Events.TOOL_CALL_DELTA:
                continue
            elif event.type == LLM_Events.TOOL_CALL_READY:
                tool_calls = event.data or {}
                tc_list = [ToolCall.from_raw(tc) for tc in tool_calls]
                assistant_msg = LLMMessage(role="assistant", tool_calls=tc_list, content="")
                self.conversation_history.append(assistant_msg)
                async for tc_event in self._process_tool_calls(tc_list):
                    yield tc_event
            elif event.type == LLM_Events.TOKEN_USAGE:
                usage = event.data or {}
                total = usage.get("total_tokens", 0)
                # 费用统计：累加到共享状态
                self._shared.token_usage["input"] += usage.get("input_tokens", 0) or 0
                self._shared.token_usage["output"] += usage.get("output_tokens", 0) or 0
                self._shared.token_usage["total"] += total
                # 接入 session_stats（token 统计 + 错误率）
                try:
                    from agentforge.utils.session_stats import session_stats
                    from agentforge.llm.llm_basics import LLMUsage
                    llm_usage = LLMUsage(
                        input_tokens=usage.get("input_tokens", 0) or 0,
                        output_tokens=usage.get("output_tokens", 0) or 0,
                        total_tokens=total,
                    )
                    session_stats.record_llm_interaction(
                        provider=self.agent_config.provider or "unified",
                        model=self.agent_config.model.model_name or "",
                        usage=llm_usage,
                        agent_name=self.type,
                    )
                except Exception:
                    pass
                yield AgentEvent.turn_token_usage(total)
            elif event.type == LLM_Events.ERROR or (hasattr(event, 'type') and event.type == "error"):
                # 🔴1 修复：LLM 报错（网络断/鉴权失败/服务端 502）不再被忽略
                error_info = event.data or {}
                error_msg = error_info.get("message", str(error_info)) if isinstance(error_info, dict) else str(event.data)
                # turn 照常推进——否则会死循环重打 LLM
                self.current_turn_index += 1
                self._consecutive_errors += 1
                # 回灌错误让 LLM 下轮自纠正（如果是临时性错误，下一轮可能就好了）
                self.conversation_history.append(
                    LLMMessage(role="user", content=f"[LLM 错误] {str(error_msg)[:200]}。请检查并重试。")
                )
                yield AgentEvent.error(f"LLM 错误: {str(error_msg)[:100]}")
                try:
                    from loguru import logger
                    logger.error(f"[Agent] LLM 返回错误 (turn {self.current_turn_index}): {error_msg}")
                except Exception:
                    pass
                # 半残 assistant 回滚：如果这一轮已经 append 了带 tool_calls 的 assistant 消息，
                # 但没等到 RESPONSE_FINISHED（LLM 断流），把这条半残消息删掉
                # （否则下轮发给 LLM 会因消息配对错误而 400）
                if self.conversation_history and self.conversation_history[-1].role == "assistant":
                    last = self.conversation_history[-1]
                    if last.tool_calls and not last.content:
                        self.conversation_history.pop()
                        try:
                            from loguru import logger
                            logger.warning("[Agent] 回滚半残 assistant 消息（LLM 断流）")
                        except Exception:
                            pass
                # 连续 3 次 LLM 错误就放弃
                if self._consecutive_errors >= 3:
                    yield AgentEvent.task_complete(f"LLM 连续 {self._consecutive_errors} 次报错，终止任务")
                    return
                # 错误后也要检查上下文压缩
                break  # 跳出 async for，让外层 while 决定是否继续

            elif event.type == LLM_Events.RESPONSE_FINISHED:
                self.current_turn_index += 1
                # 可观测性：本轮结构化日志
                _llm_latency = _time.time() - _llm_start
                try:
                    from loguru import logger
                    _ctx_tokens = sum(self.approx_token_count(m.content or "") for m in self.conversation_history)
                    logger.info(
                        f"[Turn {self.current_turn_index}/{self.max_turns}] "
                        f"LLM {_llm_latency:.1f}s | ctx~{_ctx_tokens}t | "
                        f"finish={event.data.get('finish_reason', '?') if event.data else '?'}"
                    )
                except Exception:
                    pass
                if not event.data:
                    continue
                finish_reason = event.data.get("finish_reason")
                completed_resp = LLMResponse.from_raw(event.data or {})
                self.trajectory_recorder.record_llm_interaction(
                    messages=trajectory_msg,
                    response=completed_resp,
                    provider=self.agent_config.provider or "",
                    model=self.agent_config.model.model_name or "",
                    tools=tools,
                    agent_name=self.type,
                )
                # checkpoint 移到 run() 循环里（等工具结果回灌后再存，避免半残消息）
                # 并行 delegate：收集已完成的后台 peer 结果
                async for peer_event in self._collect_peer_results():
                    yield peer_event
                # Budget Manager
                budget_err = self._check_budget()
                if budget_err:
                    # 等待后台 peer 完成再停
                    async for peer_event in self._await_all_peers():
                        yield peer_event
                    yield AgentEvent.task_complete(f"预算超限: {budget_err}")
                    return
                if finish_reason and finish_reason != "tool_calls":
                    # 任务完成前，等所有后台 peer 跑完并收集结果
                    async for peer_event in self._await_all_peers():
                        yield peer_event
                    yield AgentEvent.task_complete(finish_reason)

    async def _process_tool_calls(self, tool_calls: List[ToolCall]) -> AsyncGenerator[AgentEvent, None]:
        """执行工具 + handoff 检测 + 结果/失败回灌。
        多个独立工具并发执行（asyncio.gather），结果顺序 yield。"""
        # 🟡9 修复：重复工具调用检测（防止 LLM 死循环）
        for tc in tool_calls:
            call_sig = f"{tc.name}:{hash(json.dumps(tc.arguments or {}, sort_keys=True, default=str))}"
            # 记录最近 5 次工具调用的签名
            if not hasattr(self, '_recent_tool_calls'):
                self._recent_tool_calls = []
            # 同一签名连续出现 3 次说明 LLM 在死循环
            if self._recent_tool_calls.count(call_sig) >= 3:
                warn = f"[重复调用检测] 工具 {tc.name} 已连续调用 3 次以上，可能是死循环。请换一种方法。"
                self.conversation_history.append(
                    LLMMessage(role="tool", content=warn, tool_call_id=tc.call_id)
                )
                yield AgentEvent.tool_result(tc.call_id, tc.name, warn, False, tc.arguments)
                try:
                    from loguru import logger
                    logger.warning(f"[Agent] 检测到重复工具调用: {tc.name}")
                except Exception:
                    pass
                return
            self._recent_tool_calls.append(call_sig)
            # 只保留最近 10 次
            if len(self._recent_tool_calls) > 10:
                self._recent_tool_calls = self._recent_tool_calls[-10:]

        # 先并发执行所有工具（无依赖的工具并行跑）
        results = await asyncio.gather(
            *[self._execute_single_tool(tc) for tc in tool_calls],
            return_exceptions=True,
        )
        # 顺序 yield 每个工具的事件 + 回灌
        for tc, result in zip(tool_calls, results):
            async for event in self._process_single_result(tc, result):
                yield event

    async def _execute_single_tool(self, tc: ToolCall) -> dict:
        """执行单个工具，返回结构化结果（不 yield，给 gather 用）。

        可观测性：记录工具调用耗时 + 接入 session_stats 统计。
        """
        tool = self.tool_mgr.get_tool(tc.name)
        if not tool:
            return {"skip": True}
        name = tc.name
        call_id = tc.call_id
        arguments = {}
        if isinstance(tc.arguments, Mapping):
            arguments = dict(tc.arguments)
        self._shared.budget["tool_calls"] += 1

        import time as _time
        t0 = _time.time()
        try:
            is_success, result = await self.tool_mgr.execute(name, arguments, tool, session_id=self._shared.session_id, agent=self)
            duration = _time.time() - t0
            # 可观测性：接入 session_stats 工具统计
            try:
                from agentforge.utils.session_stats import session_stats
                session_stats.record_tool_call(name, is_success, agent_name=self.type)
            except Exception:
                pass
            # 可观测性：per-tool 耗时日志
            try:
                from loguru import logger
                logger.info(f"[Tool] {name}({'|'.join(f'{k}' for k in list(arguments)[:2])}…) → {'✅' if is_success else '❌'} {duration:.1f}s")
            except Exception:
                pass
            return {"name": name, "call_id": call_id, "arguments": arguments, "tool": tool,
                    "is_success": is_success, "result": result, "error": None, "duration": duration}
        except Exception as e:
            duration = _time.time() - t0
            try:
                from agentforge.utils.session_stats import session_stats
                session_stats.record_tool_call(name, False, agent_name=self.type)
            except Exception:
                pass
            return {"name": name, "call_id": call_id, "arguments": arguments, "tool": tool,
                    "is_success": False, "result": None, "error": str(e), "duration": duration}

    async def _process_single_result(self, tc: ToolCall, result_data) -> AsyncGenerator[AgentEvent, None]:
        """处理单个工具的执行结果：handoff/验证/反思/DLP/回灌。"""
        if isinstance(result_data, Exception):
            # gather 的 return_exceptions 把异常也收集了
            result_data = {"name": tc.name, "call_id": tc.call_id, "arguments": {},
                           "is_success": False, "result": None, "error": str(result_data)}

        if result_data.get("skip"):
            return

        name = result_data["name"]
        call_id = result_data["call_id"]
        arguments = result_data["arguments"]
        is_success = result_data["is_success"]
        result = result_data["result"]
        error = result_data.get("error")

        yield AgentEvent.tool_call(call_id, name, arguments)

        # 执行异常 → 回灌错误
        if error:
            error_msg = f"Tool execution failed: {error}"
            self.conversation_history.append(
                LLMMessage(role="tool", content=error_msg, tool_call_id=call_id)
            )
            yield AgentEvent.tool_result(call_id, name, error_msg, False, arguments)
            return

        # handoff 检测
        handoff = self._check_handoff(result) if is_success else None
        if handoff is not None:
            peer = self.get_peer(handoff.target_agent_name)
            if peer is not None:
                # 🔴4 修复：深度限制从配置读（不再硬编码 3）
                max_depth = getattr(self.agent_config, 'max_handoff_depth', None) or 3
                if self._shared._handoff_depth >= max_depth:
                    err = f"[handoff 深度超限] 已嵌套 {self._shared._handoff_depth} 层"
                    self.conversation_history.append(
                        LLMMessage(role="tool", content=err, tool_call_id=call_id)
                    )
                    yield AgentEvent.tool_result(call_id, name, err, False, arguments)
                    return
                yield AgentEvent.handoff(self.type, handoff.target_agent_name, handoff.reason)
                peer._history_initialized = True
                self._shared._handoff_depth += 1
                # 并行 delegate：peer 后台跑，不阻塞 gather
                # 启动后台 task，立即返回"已委派"，不阻塞其它工具
                bg_task = asyncio.create_task(
                    self._run_peer_background(peer, handoff, call_id)
                )
                self._shared.background_peers.append(bg_task)
                # 回灌"已委派"给 LLM（让 LLM 知道委派已启动，可以继续做别的）
                delegate_msg = f"[DELEGATE] 已将任务委派给 {handoff.target_agent_name}，后台执行中。任务: {handoff.task_description[:80]}"
                self.conversation_history.append(
                    LLMMessage(role="tool", content=delegate_msg, tool_call_id=call_id)
                )
                yield AgentEvent.tool_result(call_id, name, delegate_msg, True, arguments)
                return
            else:
                err = f"[delegate failed] No peer named '{handoff.target_agent_name}'. Available: {list(self._peers.keys())}"
                self.conversation_history.append(
                    LLMMessage(role="tool", content=err, tool_call_id=call_id)
                )
                yield AgentEvent.tool_result(call_id, name, err, False, arguments)
                return

        # 失败回灌
        if not is_success:
            self.conversation_history.append(
                LLMMessage(role="tool", content=str(result), tool_call_id=call_id)
            )
            yield AgentEvent.tool_result(call_id, name, result, False, arguments)
            return

        # 验证器
        verify_config = getattr(self.agent_config, "verify_after", None)
        if verify_config:
            verify_result = await self._verifier.verify(name, arguments, str(result), verify_config)
            if verify_result and not verify_result.passed:
                feedback = f"[VERIFY FAILED] {verify_result.message}"
                self.conversation_history.append(
                    LLMMessage(role="tool", content=feedback, tool_call_id=call_id)
                )
                yield AgentEvent.tool_result(call_id, name, feedback, False, arguments)
                return

        # Reflection
        if self._reflection and not self._reflection.exhausted:
            task_desc = ""
            for msg in reversed(self.conversation_history):
                if msg.role == "user" and msg.content:
                    task_desc = msg.content[:200]
                    break
            reflection = await self._reflection.reflect(task_desc, str(result)[:500])
            if reflection and not reflection.acceptable:
                feedback = f"[REFLECTION] quality={reflection.quality_score:.0f}/100. 改进建议: {reflection.feedback}"
                self.conversation_history.append(
                    LLMMessage(role="tool", content=feedback, tool_call_id=call_id)
                )
                yield AgentEvent.tool_result(call_id, name, feedback, False, arguments)
                return

        # DLP
        if self._dlp_enabled and isinstance(result, str):
            masked, findings = self._dlp.scan(result)
            if findings:
                result = masked + f"\n\n[DLP] 已屏蔽 {len(findings)} 处敏感信息"

        # 正常回灌 —— 工具结果超过阈值时卸载到存储层，上下文只放符号化摘要
        yield AgentEvent.tool_result(call_id, name, result, True, arguments)
        content = result if isinstance(result, str) else json.dumps(result)

        # 上下文管理：超长结果卸载，上下文只保留符号化摘要
        try:
            from agentforge.context import get_context_manager
            ctx_mgr = get_context_manager()
            if ctx_mgr.should_offload(content):
                symbol, ref_id = ctx_mgr.offload_result(
                    tool_name=name, call_id=call_id, arguments=arguments,
                    result=result, success=True,
                )
                content = symbol  # 符号化摘要替代原始结果
        except ImportError:
            pass  # context 模块不可用时退化为原行为（全量回灌）
        except Exception as e:
            try:
                from loguru import logger
                logger.warning(f"[Agent] 工具结果卸载失败（退化为全量回灌）: {e}")
            except Exception:
                pass

        self.conversation_history.append(
            LLMMessage(role="tool", content=content, tool_call_id=call_id)
        )

    # ===== message 转换 =====
    def _convert_single_message(self, msg: LLMMessage) -> Dict[str, Any]:
        role = msg.role
        data: Dict[str, Any] = {"role": role}
        if role in ("system", "user"):
            data["content"] = msg.content or ""
            return data
        if role == "assistant":
            if msg.content is not None:
                data["content"] = msg.content
            if msg.tool_calls:
                converted = []
                for tc in msg.tool_calls:
                    converted.append({
                        "id": tc.call_id,
                        "type": tc.type or "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments or {}),
                        },
                    })
                data["tool_calls"] = converted
                return data
            return data
        if role == "tool":
            if not msg.tool_call_id:
                raise ValueError("Tool message must have tool_call_id")
            data["tool_call_id"] = msg.tool_call_id
            data["content"] = msg.content
            return data
        raise ValueError(f"Unsupported role: {role!r}")

    def _build_env_prompt(self) -> str:
        sys_name = platform.system()
        release = platform.release()
        python = platform.python_version()
        if sys_name == "Windows":
            return f"[Windows {release}, Python {python}]"
        if sys_name == "Darwin":
            return f"[macOS {release}, Python {python}]"
        return f"[{sys_name} {release}, Python {python}]"

    def approx_token_count(self, text: str) -> int:
        """估算文本的 token 数。

        优先用 tiktoken（精确），降级到字符估算（中文按 1.5 字符/token，英文按 4 字符/token）。
        """
        if not text:
            return 0
        # 优先用 tiktoken
        try:
            if not hasattr(self, '_tokenizer'):
                import tiktoken
                self._tokenizer = tiktoken.get_encoding("cl100k_base")
            return len(self._tokenizer.encode(text))
        except Exception:
            pass
        # 降级：中文多的文本用 1.5 字符/token，纯英文用 4 字符/token
        chinese_ratio = sum(1 for c in text if '\u4e00' <= c <= '\u9fff') / max(len(text), 1)
        if chinese_ratio > 0.3:
            return int(len(text) / 1.5)  # 中文为主
        return max(len(text.split()), len(text) // 4)  # 英文为主
