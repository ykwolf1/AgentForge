# AgentForge 核心流程：用户输入 → while 循环 → 模型回复
#
#   用户输入
#      ↓
#   ┌─→ ① 喂 LLM          把 history 全部发给模型，等流式返回
#   │      ↓
#   │   ② 看返回          文字 → 渲染到屏幕
#   │      ↓              要调工具？ → 不调 = 任务完成，跳出循环
#   │   ③ 调工具          四道关卡：组织策略 → 用户审批 → 执行 → 结果审查
#   │      ↓
#   └── ④ 结果回灌 history（成功或失败都塞回去）
#          ↓
#       回到 ①，带着新 history 再问一次 LLM
#
#   终止：LLM 不再调工具 / 到 max_turns / 出错 / 用户取消
#   关键：工具失败也回灌，不抛异常——下轮 LLM 看到错误自己换路
#
#   这条线在代码里：
#     while 循环        pywen_agent.py:70
#     ① 喂 LLM          pywen_agent.py:82  llm_client.astream_response
#     ③ 调工具          pywen_agent.py:178 tool_mgr.execute
#                         四道关卡在 tool_manager.py:execute
#     ④ 回灌 history    pywen_agent.py:186 history.append(role="tool", ...)
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from agentforge import get_version
from agentforge.agents.agent_manager import AgentManager
from agentforge.cli.cli_console import CLIConsole
from agentforge.cli.runtime import HeadlessRunner, InteractiveSession
from agentforge.config.manager import ConfigManager
from agentforge.hooks.config import load_hooks_config
from agentforge.hooks.manager import HookManager
from agentforge.hooks.models import HookEvent
from agentforge.tools.tool_manager import ToolManager
from agentforge.utils.permission_manager import PermissionLevel, PermissionManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentForge - Multi-Agent System")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {get_version()}")
    parser.add_argument("--config", type=str, default=None, help="Config file path (default: ~/.agentforge/agentforge_config.yaml)")
    parser.add_argument("--model", type=str, help="Override model name")
    parser.add_argument("--api_key", help="Qwen API key", default=None)
    parser.add_argument("--base_url", help="Qwen base URL", default=None)
    parser.add_argument("--temperature", type=float, help="Override temperature")
    parser.add_argument("--max-tokens", type=int, help="Override max tokens")
    parser.add_argument("--session-id", type=str, help="Use specific session ID")
    parser.add_argument("--permission-mode", type=str, help="Set permission mode (yolo, planning, edit-only, locked)", default="locked")
    parser.add_argument("--agent", type=str, help="Use specific agent name (default: from config)", default=None)
    parser.add_argument("-p", "--prompt", nargs="?", help="Prompt to execute")
    parser.add_argument("--serve", action="store_true", help="Start as HTTP API server (FastAPI)")
    parser.add_argument("--port", type=int, default=8000, help="API server port (default: 8000)")
    parser.add_argument("--evaluate", type=str, default=None, help="Run evaluation on a dataset JSON file")

    subparsers = parser.add_subparsers(dest="command")
    skill_parser = subparsers.add_parser("skill", help="Skill management commands")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)
    health_parser = skill_subparsers.add_parser("health-check", help="Check installed skills")
    health_parser.add_argument("--json", action="store_true", dest="use_json", help="Output JSON")
    return parser


def run_skill_command(args: argparse.Namespace) -> None:
    if args.skill_command != "health-check":
        raise SystemExit(2)

    from agentforge.skills import SkillsManager, run_health_check_cli

    cfg_mgr = ConfigManager(args.config)
    skill_mgr = SkillsManager(cfg_mgr.get_agentforge_config_dir())
    outcome = skill_mgr.skills_for_cwd()

    exit_code = run_health_check_cli(outcome.skills, use_json=args.use_json)
    if outcome.errors:
        for error in outcome.errors:
            print(f"Load error: {error.path}: {error.message}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


async def async_main() -> None:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "skill":
        run_skill_command(args)

    cfg_mgr = ConfigManager(args.config)
    config = cfg_mgr.get_app_config(args)

    # 结构化日志：loguru 写文件（10MB 轮转，保留 3 份）
    from pathlib import Path as _Path
    from loguru import logger as _logger
    _logger.remove()   # 移除默认 stderr sink，避免双写
    _log_dir = _Path.home() / ".agentforge" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _logger.add(
        str(_log_dir / "agentforge.log"),
        rotation="10 MB",
        retention=3,
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
    )

    perm_level = PermissionLevel(config.permission_level)
    perm_mgr = PermissionManager(perm_level)

    cli = CLIConsole(perm_mgr)

    session_id = args.session_id or str(uuid.uuid4())[:8]

    hooks_cfg = load_hooks_config(cfg_mgr.get_default_hooks_path())
    hook_mgr = HookManager(hooks_cfg)

    tool_mgr = ToolManager(perm_mgr=perm_mgr, hook_mgr=hook_mgr, cli=cli)
    tool_mgr.autodiscover()

    # 初始化基础设施（知识库 + 沙箱 + Redis）并注入到工具层
    infra_config = config.infra if hasattr(config, "infra") and config.infra else {}
    if infra_config:
        from agentforge.infra import InfraManager
        infra = InfraManager(infra_config)
        await infra.health_check_all()
        # 注入沙箱到 BashTool
        if infra.sandbox and infra.sandbox.available:
            from agentforge.tools.execution.bash import BashTool
            BashTool.set_sandbox(infra.sandbox)
        # 注入知识库到全局（工具层通过 _get_kb() 访问）
        if infra.kb:
            import agentforge.knowledge as kb_module
            kb_module._kb_instance = infra.kb
        # 注入 Redis 到 AgentManager（agent_mgr 创建后注入）
        _infra_redis = infra.redis if (infra.redis and infra.redis.available) else None

    await hook_mgr.emit(
        HookEvent.SessionStart,
        base_payload={"session_id": session_id, "source": "startup"},
    )

    agent_mgr = AgentManager(cfg_mgr, cli, tool_mgr)
    # 注入 Redis（工作记忆后端）
    if _infra_redis:
        agent_mgr.set_redis(_infra_redis)
    # 多 agent 模式：配置里有 role=coordinator 的 agent → init_team；否则单 agent
    if any(getattr(a, "role", "") == "coordinator" for a in config.agents):
        await agent_mgr.init_team(session_id=session_id)
    else:
        await agent_mgr.init((args.agent or config.default_agent or "agentforge").lower(), session_id=session_id)

    ok, msg, _ = await hook_mgr.emit(
        HookEvent.UserPromptSubmit,
        base_payload={"session_id": session_id, "prompt": args.prompt or ""},
    )
    if not ok:
        cli.print(f"⛔ {msg or 'Prompt blocked by hook'}", "yellow")
        return

    # 非交互模式 
    if args.prompt:
        runner = HeadlessRunner(agent_mgr=agent_mgr, hook_mgr=hook_mgr, cli=cli, perm_mgr=perm_mgr)
        await runner.run(prompt=args.prompt, session_id=session_id, set_yolo=True)
        await agent_mgr.close()
        return

    # 交互模式 
    session = InteractiveSession(
        cli=cli,
        agent_mgr=agent_mgr,
        hook_mgr=hook_mgr,
        config_mgr=cfg_mgr,
        tool_mgr=tool_mgr,
        perm_mgr=perm_mgr,
        session_id=session_id,
    )
    await session.run()
    await agent_mgr.close()

def main() -> None:
    """Synchronous wrapper for the main CLI entry point."""
    # serve 模式不走 asyncio.run（uvicorn 自己管 event loop）
    parser = build_parser()
    args = parser.parse_args()
    if args.serve:
        import uvicorn
        from agentforge.server.app import app
        uvicorn.run(app, host="0.0.0.0", port=args.port)
        return

    # 离线评估模式
    if args.evaluate:
        asyncio.run(_run_evaluation(args.evaluate, args.agent))
        return

    asyncio.run(async_main())


async def _run_evaluation(dataset_path: str, agent_name: str = "") -> None:
    """运行离线评估"""
    import json
    from agentforge.config.manager import ConfigManager
    from agentforge.hooks.config import load_hooks_config
    from agentforge.hooks.manager import HookManager
    from agentforge.evaluation.runner import EvaluationRunner

    cfg_mgr = ConfigManager()
    hook_mgr = HookManager(load_hooks_config(cfg_mgr.get_default_hooks_path()))
    runner = EvaluationRunner(cfg_mgr, hook_mgr)
    report = await runner.run(dataset_path, agent_name=agent_name or "")

    print("\n" + "=" * 60)
    print("评估报告")
    print("=" * 60)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print("=" * 60)

if __name__ == "__main__":
    main()
