# task_manager.py 核心流程：异步任务管理器
#
#   从"同步阻塞"升级到"异步任务"：
#     submit() → 立即返回 task_id，后台 asyncio.create_task 跑
#     get() → 查进度/结果
#     cancel() → 取消任务
#     list_all() → 列所有任务
#
#   并发控制：Semaphore(max_concurrent) 限制同时跑的任务数
#   隔离：每个任务独立 AgentManager（独立 SharedState/Agent）
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"          # 排队中（等 semaphore）
    RUNNING = "running"          # 执行中
    COMPLETED = "completed"      # 完成
    FAILED = "failed"            # 失败
    CANCELLED = "cancelled"      # 被取消


@dataclass
class AgentTask:
    """一个异步 agent 任务"""
    task_id: str
    prompt: str
    session_id: str
    agent_name: str = ""
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    progress: dict = field(default_factory=lambda: {"turn": 0, "current_tool": "", "token_usage": {}})
    events: list = field(default_factory=list)       # 最近的事件（最多 20 条）
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    # 内部（不序列化）
    _cancel_event: Optional[asyncio.Event] = None
    _bg_task: Optional[asyncio.Task] = None

    def to_dict(self) -> dict:
        """序列化给 API 返回（去掉内部字段）"""
        return {
            "task_id": self.task_id,
            "prompt": self.prompt[:100],
            "session_id": self.session_id,
            "status": self.status.value,
            "result": self.result[:500] if self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED) else "",
            "progress": self.progress,
            "events": self.events[-5:],   # 最近 5 条事件
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error[:300],
        }


class TaskManager:
    """管理所有异步任务。内存存储（重启丢失）。"""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: Dict[str, AgentTask] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        # 全局依赖（由 app.py 注入）
        self._cfg_mgr = None
        self._hook_mgr = None
        self._redis = None   # 可选：Redis 客户端（不可用时走内存）

    def setup(self, cfg_mgr, hook_mgr):
        """注入全局依赖（启动时调一次）"""
        self._cfg_mgr = cfg_mgr
        self._hook_mgr = hook_mgr

    def set_redis(self, redis_client):
        """注入 Redis 客户端（任务持久化）。None=内存模式。"""
        self._redis = redis_client

    async def submit(self, prompt: str, session_id: str = "", agent_name: str = "",
                     permission: str = "yolo", agent_config_override=None,
                     max_retries: int = 2, timeout: float = 120) -> str:
        """提交任务，立即返回 task_id。后台异步执行。

        Args:
            agent_config_override: spawn 模式用——动态 agent 配置（不走 config_mgr 预定义）
            max_retries: 失败重试次数（0=不重试）
            timeout: 单次执行超时（秒）
        """
        task_id = str(uuid.uuid4())[:8]
        sid = session_id or str(uuid.uuid4())[:8]
        task = AgentTask(
            task_id=task_id,
            prompt=prompt,
            session_id=sid,
            agent_name=agent_name,
            created_at=datetime.now().isoformat(),
            _cancel_event=asyncio.Event(),
        )
        self._tasks[task_id] = task
        # Redis 持久化（不可用时跳过，内存模式）
        if self._redis and self._redis.available:
            await self._redis.hset("agentforge:tasks", task_id, {"status": "pending", "prompt": prompt[:100]})
        task._bg_task = asyncio.create_task(
            self._run(task, permission, agent_config_override, max_retries, timeout)
        )
        logger.info(f"任务提交: {task_id} prompt={prompt[:50]} retry={max_retries} timeout={timeout}s")
        return task_id

    async def _run(self, task: AgentTask, permission: str,
                   agent_config_override=None, max_retries: int = 2, timeout: float = 120):
        """实际执行（被 semaphore 限流）。每个任务独立 AgentManager。
        含失败重试（指数退避）+ 超时控制 + spawn 模式（动态 agent 配置）。"""
        from agentforge.agents.agent_manager import AgentManager
        from agentforge.cli.runtime import TurnRunner, CancellationToken
        from agentforge.tools.tool_manager import ToolManager
        from agentforge.utils.permission_manager import PermissionManager, PermissionLevel
        from .headless_cli import HeadlessCLI

        async with self._semaphore:  # 并发队列限流
            if task._cancel_event and task._cancel_event.is_set():
                task.status = TaskStatus.CANCELLED
                task.finished_at = datetime.now().isoformat()
                return

            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now().isoformat()

            last_error = ""
            for attempt in range(max_retries + 1):  # 失败重试
                if task._cancel_event and task._cancel_event.is_set():
                    break

                cli = HeadlessCLI()
                perm_level = PermissionLevel.YOLO
                if permission in ("locked", "edit_only", "planning", "yolo"):
                    perm_level = PermissionLevel(permission)
                perm = PermissionManager(perm_level)
                tool_mgr = ToolManager(perm_mgr=perm, hook_mgr=self._hook_mgr, cli=cli)
                agent_mgr = AgentManager(self._cfg_mgr, cli, tool_mgr)

                try:
                    # 初始化 agent（spawn 模式用 override 配置，普通模式走 config_mgr）
                    if agent_config_override:
                        # spawn 模式：用动态配置直接 new Agent
                        from agentforge.agents.agent import Agent
                        agent = Agent(
                            name=f"sub_{task.task_id}",
                            agent_config=agent_config_override,
                            config_mgr=self._cfg_mgr,
                            cli=cli,
                            tool_mgr=tool_mgr,
                        )
                        agent._shared.session_id = task.session_id
                        agent_mgr._current = agent
                        agent_mgr._current_name = agent.name
                    else:
                        # 普通模式：走预定义 agent
                        config = self._cfg_mgr.get_app_config()
                        if any(getattr(a, "role", "") == "coordinator" for a in config.agents):
                            await agent_mgr.init_team(session_id=task.session_id)
                        else:
                            default_name = task.agent_name or config.default_agent or "agentforge"
                            await agent_mgr.init(default_name.lower(), session_id=task.session_id)

                    # 跑 agent（超时控制）
                    runner = TurnRunner(agent_mgr=agent_mgr, hook_mgr=self._hook_mgr, cli=cli)
                    cancel_token = CancellationToken()

                    outcome = await asyncio.wait_for(
                        runner.run_once(
                            user_input=task.prompt, session_id=task.session_id, cancel_token=cancel_token
                        ),
                        timeout=timeout,
                    )

                    # 取结果
                    result_text = ""
                    for msg in reversed(agent_mgr.current.conversation_history):
                        if msg.role == "assistant" and msg.content and msg.content.strip():
                            result_text = msg.content
                            break

                    task.progress["turn"] = agent_mgr.current.current_turn_index
                    try:
                        task.progress["token_usage"] = dict(agent_mgr.current._shared.token_usage)
                    except Exception:
                        pass

                    end_status = outcome.end.value if hasattr(outcome.end, 'value') else str(outcome.end)
                    task.result = result_text or f"(completed, status={end_status})"
                    task.status = TaskStatus.COMPLETED
                    last_error = ""
                    break  # 成功，跳出重试循环

                except asyncio.TimeoutError:
                    last_error = f"超时（{timeout}s）"
                    logger.warning(f"任务 {task.task_id} 第 {attempt+1} 次超时")
                except asyncio.CancelledError:
                    task.status = TaskStatus.CANCELLED
                    task.error = "任务被取消"
                    break
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"任务 {task.task_id} 第 {attempt+1} 次失败: {e}")
                finally:
                    try:
                        await agent_mgr.close()
                    except Exception:
                        pass

                # 重试退避（最后一次不等）
                if attempt < max_retries and last_error:
                    wait = min(2 ** attempt, 10)
                    logger.info(f"任务 {task.task_id} {wait}s 后重试 ({attempt+1}/{max_retries})")
                    task.progress["retry"] = f"第 {attempt+1}/{max_retries} 次重试中"
                    await asyncio.sleep(wait)

            # 所有重试都失败
            if last_error and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                task.status = TaskStatus.FAILED
                task.error = last_error
                logger.error(f"任务 {task.task_id} 最终失败: {last_error}")

            task.finished_at = datetime.now().isoformat()

    def get(self, task_id: str) -> Optional[AgentTask]:
        """查任务状态/进度"""
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """取消任务：set cancel_event + cancel asyncio.Task"""
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False
        if task._cancel_event:
            task._cancel_event.set()
        if task._bg_task and not task._bg_task.done():
            task._bg_task.cancel()
        task.status = TaskStatus.CANCELLED
        task.finished_at = datetime.now().isoformat()
        logger.info(f"任务取消请求: {task_id}")
        return True

    def list_all(self) -> List[AgentTask]:
        """列所有任务"""
        return list(self._tasks.values())

    def get_stats(self) -> dict:
        """统计信息"""
        counts = {}
        for t in self._tasks.values():
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        return {
            "total": len(self._tasks),
            "by_status": counts,
            "max_concurrent": self._max_concurrent,
        }

# 模块级单例（spawn_tool 和 app.py 共用这一个）
_task_mgr = TaskManager(max_concurrent=3)
