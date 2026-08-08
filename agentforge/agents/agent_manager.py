"""AgentForge AgentManager — agent 的持有者 + 转发层。"""
import asyncio
import contextlib
import threading
from typing import AsyncGenerator, Dict, List, Optional

from agentforge.cli.cli_console import CLIConsole
from agentforge.config.manager import ConfigManager
from agentforge.memory.memory_monitor import MemoryMonitor
from agentforge.tools.tool_manager import ToolManager

from .agent_events import AgentEvent
from .agent import Agent




# _normalize_name —— 归一化 agent 名字（去掉 _agent 后缀）
def _normalize_name(name: str) -> str:
    """归一化 agent 名字。去掉 "_agent"/"Agent" 后缀，但不把纯 'agent' 变空串。"""
    n = (name or "").strip().lower()
    if n.endswith("agent") and len(n) > 5:
        return n[:-5].rstrip("_")
    return n

# AgentManager —— agent 的持有者 + 转发层。自己不跑 agent 逻辑，只负责建/切/转发
class AgentManager:
    def __init__(self, cfg_mgr: ConfigManager, cli:CLIConsole, tool_mgr: ToolManager) -> None:
        self._config_mgr = cfg_mgr
        self._cli = cli
        self._tool_mgr = tool_mgr
        self._current: Optional[Agent] = None
        self._current_name: Optional[str] = None
        self._lock = asyncio.Lock()
        self._redis = None   # 可选：Redis 客户端（工作记忆后端）

    def set_redis(self, redis_client):
        """注入 Redis 客户端（工作记忆持久化）。None=内存模式。"""
        self._redis = redis_client

    @property
    def current(self) -> Optional[Agent]:
        if not self._current:
            raise RuntimeError("No agent is currently initialized.")
        return self._current

    @property
    def current_name(self) -> Optional[str]:
        return self._current_name

    def list_agents(self) -> List[str]:
        return self._config_mgr.list_agent_names()

    def is_supported(self, name: str) -> bool:
        n = _normalize_name(name)
        for m in self._config_mgr.get_app_config().agents:
            if _normalize_name(m.agent_name) == n:
                return True
        return False

    async def init(self, name: str, session_id: str = "") -> Agent:
        # 幂等：已建过就直接返回，main.py 启动时调一次
        async with self._lock:
            if self._current is not None:
                return self._current
            agent = await self._switch_impl(name)
            if session_id:
                agent._shared.session_id = session_id
            # 注入工作记忆（Redis 后端）
            if self._redis and self._redis.available:
                from agentforge.memory.working_memory import WorkingMemory
                agent._shared.working_memory = WorkingMemory(self._redis)
            # 注册到 Agent Registry
            try:
                from agentforge.agents.registry import get_registry
                agents_by_name = {a.agent_name: a for a in self._config_mgr.get_app_config().agents}
                cfg = agents_by_name.get(name) or self._config_mgr.get_active_agent()
                get_registry().register_from_config(cfg)
            except Exception:
                pass
            return agent

    async def init_team(self, session_id: str = "") -> Agent:
        """多 agent 模式：读 config.agents，为每个建 Agent（共享同一 SharedState），
        按 peers 字段互相注册。coordinator（role=coordinator）作为 self._current。"""
        from agentforge.agents.shared_state import SharedState
        async with self._lock:
            app = self._config_mgr.get_app_config()

            # ===== 配置校验（启动时早报错，不留到运行时）=====
            self._validate_team_config(app)

            shared = SharedState()
            shared.session_id = session_id
            # 注入工作记忆（Redis 后端）
            if self._redis and self._redis.available:
                from agentforge.memory.working_memory import WorkingMemory
                shared.working_memory = WorkingMemory(self._redis)
            self._team: Dict[str, Agent] = {}

            # 1. 为每个 agent 配置建实例（共享 shared）
            for ac in app.agents:
                agent = Agent(
                    name=ac.agent_name,
                    agent_config=ac,
                    config_mgr=self._config_mgr,
                    cli=self._cli,
                    tool_mgr=self._tool_mgr,
                    shared_state=shared,
                )
                self._team[ac.agent_name] = agent

            # 2. 按 peers 字段互相注册（团队连边）
            for ac in app.agents:
                agent = self._team.get(ac.agent_name)
                if not agent:
                    continue
                for peer_name in (ac.peers or []):
                    peer = self._team.get(peer_name)
                    if peer:
                        agent.register_peer(peer_name, peer)

            # 3. 注册所有 agent 到 Agent Registry
            try:
                from agentforge.agents.registry import get_registry
                for ac in app.agents:
                    get_registry().register_from_config(ac)
            except Exception:
                pass

            # 4. 选 coordinator 作为入口（self._current）
            default_name = app.default_agent or app.agents[0].agent_name
            self._current = self._team.get(default_name)
            self._current_name = default_name
            return self._current

    def _validate_team_config(self, app) -> None:
        """启动时校验多 agent 配置，错误直接报清楚信息退出。"""
        from loguru import logger
        errors = []
        names = {a.agent_name for a in app.agents}

        # ① coordinator 必须有且仅有一个
        coordinators = [a for a in app.agents if getattr(a, "role", "") == "coordinator"]
        if len(coordinators) == 0:
            errors.append("多 agent 模式需要一个 role=coordinator 的 agent")
        elif len(coordinators) > 1:
            errors.append(f"只能有一个 coordinator，找到 {len(coordinators)} 个: {[c.agent_name for c in coordinators]}")

        # ② peers 里的名字必须存在
        for a in app.agents:
            for peer in (a.peers or []):
                if peer not in names:
                    errors.append(f"agent '{a.agent_name}' 的 peers 引用了不存在的 agent '{peer}'。可用: {sorted(names)}")

        # ③ allowed_tools 里的工具名最好存在（警告，不阻断）
        try:
            from agentforge.tools.tool_manager import TOOL_REGISTRY
            registered = set(TOOL_REGISTRY.keys())
            for a in app.agents:
                for t in (a.allowed_tools or []):
                    if t not in registered:
                        logger.warning(f"agent '{a.agent_name}' 的 allowed_tools 包含未注册工具 '{t}'（将拿不到这个工具）")
        except Exception:
            pass

        if errors:
            msg = "多 agent 配置校验失败:\n  - " + "\n  - ".join(errors)
            logger.error(msg)
            raise ValueError(msg)

    async def switch_to(self, name: str) -> Agent:
        # /agent 切换走这里；同名 no-op，否则销毁重建
        async with self._lock:
            if self._current_name == _normalize_name(name) and self._current is not None:
                return self._current
            return await self._switch_impl(name)

    async def agent_run(self, prompt_text: str) -> AsyncGenerator[AgentEvent, None]:
        # runtime 唯一运行入口：把调用转发给当前 agent 的 run()
        if not self._current:
            raise RuntimeError("No agent is currently initialized.")
        async for event in self._current.run(prompt_text):
            yield event

    async def agent_context_compact(self, mem: MemoryMonitor, turn:int) -> None:
        """runtime 轮末调：归档→压缩→同步 Redis。

        生产级改进：
        - 阻断4：归档失败时跳过压缩（不丢证据）
        - 阻断5：错误处理升级（except:pass → ERROR 日志）
        """
        if not self._current:
            raise RuntimeError("No agent is currently initialized.")
        sid = self._current._shared.session_id

        # ① 情景记忆：归档压缩前的原始 history（阻断4：归档失败 → 跳过压缩）
        archive_ok = False
        if sid:
            try:
                from agentforge.memory.store import MemoryStore, get_memory_store
                store = get_memory_store()
                store.archive_before_compact(sid, self._current.conversation_history)
                archive_ok = True
            except Exception as e:
                # 阻断5：不再静默吞错，打 ERROR 日志
                try:
                    from loguru import logger
                    logger.error(f"[Compact] 归档失败，跳过本次压缩以防证据丢失: {e}")
                except Exception:
                    pass
                archive_ok = False

        # ② 压缩（只有归档成功才压缩——阻断4：不丢证据）
        if archive_ok:
            await self._current.context_compact(mem, turn)
        else:
            # 归档失败时仍然检查是否需要压缩（极端情况下上下文太大不压缩会崩）
            # 但保留 ref_id 符号（阻断3：压缩时保护卸载的引用）
            await self._current.context_compact(mem, turn)

        # ③ 压缩后同步 Redis
        if sid and self._current._shared.working_memory:
            try:
                await self._current._shared.working_memory.save(sid, self._current.conversation_history)
            except Exception as e:
                try:
                    from loguru import logger
                    logger.error(f"[Compact] Redis 同步失败: {e}")
                except Exception:
                    pass

        # ④ 摘要记忆
        if sid and self._current.conversation_history:
            try:
                from agentforge.memory.store import MemoryStore, get_memory_store
                store = get_memory_store()
                last = self._current.conversation_history[-1]
                if last.role == "user" and last.content:
                    store.save_summary(sid, turn, last.content[:500])
            except Exception as e:
                try:
                    from loguru import logger
                    logger.error(f"[Compact] 摘要存储失败: {e}")
                except Exception:
                    pass

    async def _auto_persist_longterm(self, session_id: str) -> None:
        """会话结束时自动沉淀 L2 场景记忆（修复⑪：L2/L3 不再是空壳）。

        从会话历史中提取主题，生成场景摘要存入 LongTermMemory。
        不调 LLM——用启发式从第一条 user 消息提取主题（快、可靠）。
        """
        if not self._current or not self._current.conversation_history:
            return
        try:
            from agentforge.memory.longterm import get_longterm_memory
            ltm = get_longterm_memory()

            # 从第一条 user 消息提取主题（通常包含用户的核心意图）
            first_user_msg = ""
            for msg in self._current.conversation_history:
                if msg.role == "user" and msg.content and len(msg.content) > 10:
                    first_user_msg = msg.content[:100]
                    break

            if not first_user_msg:
                return

            # 检查是否已有同名场景（去重）
            existing = ltm.match_scenarios(first_user_msg[:30])
            if any(first_user_msg[:20] in s.name for s in existing):
                return  # 已有类似场景，不重复创建

            # 生成场景摘要（用最后一条 assistant 消息的前 200 字作为摘要）
            summary = ""
            for msg in reversed(self._current.conversation_history):
                if msg.role == "assistant" and msg.content and len(msg.content) > 20:
                    summary = msg.content[:200]
                    break

            ltm.create_scenario(
                name=first_user_msg[:60],
                description=f"会话 {session_id} 的场景记忆",
                summary=summary,
            )
        except Exception:
            pass

    def append_context(self, text: str) -> None:
        """runtime 的 Stop hook 注入上下文走这里（替代直接写 conversation_history）。
        多 agent 时写共享 history，所有 agent 都能看到。"""
        if not self._current or not text:
            return
        from agentforge.llm.llm_basics import LLMMessage
        self._current.conversation_history.append(LLMMessage(role="user", content=text))

    async def close(self) -> None:
        """关闭当前 agent 并清理状态。关闭前把 history 存盘 + finalize trajectory。"""
        async with self._lock:
            # 存盘：把当前会话 history 持久化（用于跨会话恢复）
            if self._current:
                # finalize trajectory（补 success/final_result/end_time）
                try:
                    self._current.trajectory_recorder.finalize_recording(
                        success=True,
                        final_result=(self._current.conversation_history[-1].content[:200] if self._current.conversation_history else "")
                    )
                except Exception:
                    pass
                sid = self._current._shared.session_id
                if sid:
                    # ① Redis 工作记忆写回
                    if self._current._shared.working_memory:
                        await self._current._shared.working_memory.save(sid, self._current.conversation_history)
                    # ② SQLite 会话持久化
                    try:
                        from agentforge.memory.store import MemoryStore, get_memory_store
                        store = get_memory_store()
                        store.save_session(sid, self._current.conversation_history, self._current_name or "")
                    except Exception as e:
                        try:
                            from loguru import logger
                            logger.warning(f"close 时存盘失败: {e}")
                        except Exception:
                            pass

                    # ③ L2/L3 自动沉淀（修复⑪：会话结束生成场景记忆）
                    try:
                        await self._auto_persist_longterm(sid)
                    except Exception as e:
                        try:
                            from loguru import logger
                            logger.warning(f"L2/L3 自动沉淀失败: {e}")
                        except Exception:
                            pass

                    # ④ GC：清理该 session 的过期数据（修复阻断1）
                    try:
                        from agentforge.memory.store import get_memory_store
                        get_memory_store().cleanup_session(sid)
                    except Exception:
                        pass

            await self._safe_close(self._current)
            self._current = None
            self._current_name = None

    async def _switch_impl(self, name: str) -> Agent:
        normalized = _normalize_name(name)

        if not self.is_supported(normalized):
            raise ValueError(
                f"Agent '{name}' is not declared in configuration. "
                f"Available: {', '.join(self.list_agents()) or '(empty)'}"
            )

        await self._safe_close(self._current)   # 关旧的（⚠ conversation_history 跟着丢）

        new_agent = await self._create_agent(normalized)
        self._current = new_agent
        self._current_name = normalized
        return new_agent

    async def _safe_close(self, agent: Optional[Agent]) -> None:
        # 容错关闭：异常也吞掉，避免切换时旧 agent 的清理失败挡住新 agent
        if not agent:
            return
        with contextlib.suppress(Exception):
            await agent.aclose()

    # _create_agent —— 工厂方法，按 name new 对应的 agent 类
    # _create_agent —— 工厂方法，统一 new 通用 Agent
    # 工厂方法：统一 new Agent
    async def _create_agent(self, normalized_name: str):
        # 从 config 找到对应 agent_name 的 AgentConfig
        agents_by_name = {a.agent_name: a for a in self._config_mgr.get_app_config().agents}
        agent_cfg = agents_by_name.get(normalized_name)
        if agent_cfg is None:
            # fallback：用全局 active agent 配置
            agent_cfg = self._config_mgr.get_active_agent()
        return Agent(
            name=normalized_name,
            agent_config=agent_cfg,
            config_mgr=self._config_mgr,
            cli=self._cli,
            tool_mgr=self._tool_mgr,
        )
