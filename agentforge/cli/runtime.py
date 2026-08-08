# runtime.py 核心流程：读输入 → 跑 agent → 渲染 → 等下一个
#
#   while True:                              # InteractiveSession.run
#      ↓
#   读一行输入
#      ↓
#   斜杠命令(/agent /clear)? → 是 → CommandRouter 处理，回到顶部
#      ↓ 否
#   ┌─→ TurnRunner.run_once(这一轮)
#   │     ↓
#   │   UserPromptSubmit hook      hook 可拦下这次输入
#   │     ↓
#   │   events = agent_mgr.agent_run(input)   拿到 agent 的事件流（async generator）
#   │     ↓
#   │   EventPump 边消费事件边渲染到终端        Ctrl+C/Esc 通过 cancel_token 生效
#   │     ↓
#   │   agent 跑完，返回终止类型（completed/cancelled/...）
#   └── 回到 while 顶部
#
#   关键：agent 是 async generator，"跑 agent"和"渲染"是并发的——
#        EventPump 用 asyncio.create_task 起一个任务消费事件，主流程 await 它
#
#   代码位置：
#     InteractiveSession.run    runtime.py:228
#     TurnRunner.run_once       runtime.py:183
#     EventPump.run             runtime.py:159
from __future__ import annotations

import asyncio
import threading
from enum import Enum
from typing import Callable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import InMemoryHistory

from agentforge.agents.agent_events import Agent_Events
from agentforge.agents.agent_manager import AgentManager
from agentforge.cli.cli_console import CLIConsole
from agentforge.cli.command_processor import CommandProcessor
from agentforge.cli.commands.base_command import CommandAction, CommandResult
from agentforge.config.manager import ConfigManager
from agentforge.hooks.models import HookEvent
from agentforge.llm.llm_basics import LLMMessage
from agentforge.memory.memory_monitor import MemoryMonitor
from agentforge.tools.tool_manager import ToolManager
from agentforge.utils.key_binding import create_key_bindings
from agentforge.utils.permission_manager import PermissionLevel, PermissionManager


# RunEndType —— 一轮跑完的几种结果。EventPump 据此决定怎么收尾、要不要继续 REPL
class RunEndType(str, Enum):
    COMPLETED = "completed"              # 流自然结束（含 agent 到 max_turns 静默退出）
    TASK_COMPLETE = "task_complete"      # agent 显式说"做完了"（LLM 不再调工具）
    TURN_MAX_REACHED = "turn_max_reached"  # 到深度上限（只有 claude 会显式发这个）
    WAITING_FOR_USER = "waiting_for_user"  # agent 等用户补充信息，不结束会话
    CANCELLED = "cancelled"              # 用户 Ctrl+C/Esc
    ERROR = "error"                      # 出错或被 hook 拦下

# RunOutcome —— 把 RunEndType + 可选异常打包，TurnRunner 返回给 InteractiveSession
class RunOutcome:
    def __init__(self, end: RunEndType, err: Exception | None = None) -> None:
        self.end = end
        self.err = err

class PromptDriver:
    def __init__(self, 
                 cli:CLIConsole, 
                 perm_mgr: PermissionManager, 
                 cancel_event_getter: Optional[Callable[[], Optional[CancellationToken]]] = None,
                 current_task_getter: Optional[Callable[[], Optional[asyncio.Task]]] = None,
                 exit_sentinel: str = "__PYWEN_QUIT__",
        ) -> None:
        self._cli:CLIConsole = cli
        self.exit_sentinel = exit_sentinel
        kbs = create_key_bindings(
            console_getter=lambda: cli,
            perm_mgr_getter=lambda: perm_mgr,
            cancel_event_getter= cancel_event_getter,
            current_task_getter=current_task_getter,
            exit_sentinel=exit_sentinel,
        )
        self._session = PromptSession(
            history=InMemoryHistory(),
            auto_suggest=AutoSuggestFromHistory(),
            key_bindings= kbs,
            multiline=True,
            wrap_lines=True,
        )

    async def read_line(self, prompt) -> str:
        return await self._session.prompt_async(prompt, multiline=False)

class CommandRouter:
    def __init__(self) -> None:
        self._impl = CommandProcessor()

    async def try_handle(self, raw: str, *, context: dict) -> CommandResult:
        return await self._impl.process_command(raw, context)

    @property
    def cmd_mgr(self):
        return self._impl

# CancellationToken —— 取消信号。Ctrl+C/Esc 调 .set()，EventPump 在下次循环检查 .is_set 时响应
# 同时持有 current_task，set() 时直接 cancel 掉它，不必等下一次轮询
class CancellationToken:
    def __init__(self) -> None:
        self._flag = threading.Event()
        self.current_task: asyncio.Task | None = None

    def set(self) -> None:
        self._flag.set()
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()      # 立即取消，不等下次循环检查

    def clear(self) -> None:
        self._flag.clear()
        self.current_task = None

    @property
    def is_set(self) -> bool:
        return self._flag.is_set()

# EventPump —— 把 agent 的 async 事件流消费掉，逐个渲染到终端
class EventPump:
    """把 agent 事件流 → CLI 渲染"""
    def __init__(self, cli : CLIConsole) -> None:
        self._cli = cli

    async def run(self, agent_run_aiter, cancel_token: CancellationToken) -> str:
        async for event in agent_run_aiter:
            if cancel_token.is_set:                       # 用户按了取消
                self._cli.print("\n⚠️ Operation cancelled by user", "yellow")
                return Agent_Events.CANCEL

            await self._cli.handle_events(event)          # 渲染这一条事件

            if event.type in {                            # 终止事件，结束本轮
                Agent_Events.TASK_COMPLETE,
                Agent_Events.TURN_MAX_REACHED,
                Agent_Events.WAITING_FOR_USER,
            }:
                return event.type
        return RunEndType.COMPLETED                        # 流自然耗尽

# TurnRunner —— 跑一轮：拿到 agent 事件流 → 起 EventPump 任务消费 → 等它结束
class TurnRunner:
    """ 单次Turn """
    def __init__(self, agent_mgr, hook_mgr, cli) -> None:
        self.agent_mgr = agent_mgr
        self.hook_mgr = hook_mgr
        self.cli = cli
        self._pump = EventPump(cli)

    async def run_once(self, *, user_input: str, session_id: str, cancel_token: CancellationToken) -> RunOutcome:
        try:
            # UserPromptSubmit hook：可拦下这次输入（block → 本轮直接 ERROR）
            ok, msg, _ = await self.hook_mgr.emit(
                HookEvent.UserPromptSubmit,
                base_payload={"session_id": session_id, "prompt": user_input},
            )
            if not ok:
                self.cli.print(f"⛔ {msg or 'Prompt blocked by hook'}", "yellow")
                return RunOutcome(RunEndType.ERROR)

            cancel_token.clear()
            events = self.agent_mgr.agent_run(user_input)         # 拿事件流（还没开始跑）
            task = asyncio.create_task(self._pump.run(events, cancel_token))  # 起任务消费
            cancel_token.current_task = task                       # 让 Ctrl+C 能取消到它
            result = await task                                    # 阻塞到 agent 跑完

            # 后置 Hook
            ok2, msg2, extra = await self.hook_mgr.emit(
                HookEvent.Stop,
                base_payload={"session_id": session_id, "prompt": user_input},
            )
            if msg2:
                self.cli.print(msg2, "yellow")
            if not ok2:
                self.cli.print(f"⛔ {msg2 or 'Prompt blocked by hook'}", "yellow")

            if extra.get("additionalContext"):
                self.agent_mgr.append_context(extra["additionalContext"])

            if result == Agent_Events.WAITING_FOR_USER:
                return RunOutcome(RunEndType.WAITING_FOR_USER)
            if result == Agent_Events.CANCEL:
                return RunOutcome(RunEndType.CANCELLED)
            if result == RunEndType.COMPLETED:
                return RunOutcome(RunEndType.COMPLETED)
            if result == Agent_Events.TURN_MAX_REACHED:
                return RunOutcome(RunEndType.TURN_MAX_REACHED)
            if result == Agent_Events.TASK_COMPLETE:
                return RunOutcome(RunEndType.TASK_COMPLETE)
            return RunOutcome(RunEndType.COMPLETED)

        except asyncio.CancelledError:
            self.cli.print("\n⚠️ Task was cancelled", "yellow")
            return RunOutcome(RunEndType.CANCELLED)
        except KeyboardInterrupt:
            self.cli.print("\n⚠️ Operation interrupted by user", "yellow")
            return RunOutcome(RunEndType.CANCELLED)
        except Exception as e:
            self.cli.print(f"\nError: {e}", "red")
            return RunOutcome(RunEndType.ERROR, e)

class HeadlessRunner:
    """ 非交互模式 """
    def __init__(self, *, agent_mgr, hook_mgr, cli, perm_mgr: PermissionManager) -> None:
        self.agent_mgr = agent_mgr
        self.hook_mgr = hook_mgr
        self.cli = cli
        self.perm_mgr = perm_mgr
        self._runner = TurnRunner(agent_mgr, hook_mgr, cli)

    async def run(self, *, prompt: str, session_id: str, set_yolo: bool = True) -> RunOutcome:
        if set_yolo:
            self.perm_mgr.set_permission_level(PermissionLevel.YOLO)   # headless 没人能按 y/n，强制全自动批
        cancle = CancellationToken()
        outcome = await self._runner.run_once(
            user_input=prompt, session_id=session_id, cancel_token=cancle
        )
        return outcome

# InteractiveSession —— 交互模式（默认）。while True 读行，分发命令/输入
# ⚠ 唯一触发上下文压缩的地方在 run() 末尾的 agent_context_compact，headless 不走这
class InteractiveSession:
    """ 交互模式 """
    def __init__(self, 
                 *, 
                 config_mgr: ConfigManager, 
                 agent_mgr: AgentManager, 
                 hook_mgr, cli:CLIConsole, 
                 perm_mgr: PermissionManager, 
                 tool_mgr: ToolManager,
                 session_id: str
                 ) -> None:
        self.agent_mgr = agent_mgr
        self.hook_mgr = hook_mgr
        self.cli = cli
        self.perm_mgr = perm_mgr
        self.session_id = session_id
        self.cancel_event = CancellationToken()
        self.config_mgr = config_mgr
        self.tool_mgr = tool_mgr

        self._prompt = PromptDriver(
                cli, 
                perm_mgr, 
                cancel_event_getter=lambda: self.cancel_event,
                current_task_getter=lambda: self.cancel_event.current_task,
            )

        self._router = CommandRouter()
        self._runner = TurnRunner(agent_mgr, hook_mgr, cli)
        self.mm = MemoryMonitor(config_mgr)

    async def run(self) -> None:
        self.cli.start_interactive_mode()
        sid = self.session_id
        turn :int= 0
        while True:
            perm_level = self.perm_mgr.get_permission_level()
            model_name = self.config_mgr.get_active_model_name() or "N/A"
            self.cli.show_status_bar(model_name = model_name,  permission_level= perm_level.value)
            try:
                line = await self._prompt.read_line(self.cli.prompt_prefix(sid))
            except EOFError:
                self.cli.print("Goodbye!", "yellow")
                break
            except KeyboardInterrupt:
                self.cli.print("\nUse Ctrl+C again to quit, or type 'exit'", "yellow")
                continue

            if not line or not line.strip():
                continue

            if line == self._prompt.exit_sentinel:
                self.cli.print("Goodbye!", "yellow")
                break

            low = line.strip().lower()
            if low in {"exit", "quit", "q"}:
                self.cli.print("Goodbye!", "yellow")
                break

            ctx = {
                "console": self.cli,
                "agent_mgr": self.agent_mgr,
                "config_mgr": self.config_mgr,
                "hook_mgr": self.hook_mgr,
                "tool_mgr": self.tool_mgr,
                "cmd_mgr": self._router.cmd_mgr,
            }
            cmd_res = await self._router.try_handle(line, context=ctx)
            if cmd_res.action == CommandAction.EXIT:
                break
            if cmd_res.action == CommandAction.HANDLED:
                continue
            if cmd_res.action == CommandAction.REWRITE:
                effective_input = cmd_res.text or ""
            else:
                effective_input = line  # FORWARD
            if not effective_input.strip():
                continue
            
            self.cancel_event.clear()
            outcome = await self._runner.run_once(
                user_input=effective_input, session_id=sid, cancel_token=self.cancel_event
            )

            # 正常结束才压缩 + 累加 turn；取消/出错/等用户输入都不压
            if outcome.end in (RunEndType.COMPLETED, RunEndType.TASK_COMPLETE, RunEndType.TURN_MAX_REACHED):
                turn += 1
                await self.agent_mgr.agent_context_compact(self.mm, turn=turn)   # 唯一压缩入口
                continue
            if outcome.end is RunEndType.WAITING_FOR_USER:
                continue
            if outcome.end in (RunEndType.CANCELLED, RunEndType.ERROR):
                continue
