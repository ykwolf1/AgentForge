# runner.py 核心流程：离线评估运行器
#
#   对一组任务跑 agent，收集结果和轨迹，计算指标，生成报告。
#   CLI: agentforge --evaluate datasets/sample.json
import asyncio
import json
from typing import Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)

from .dataset import load_dataset, EvalTask
from .metrics import generate_report


class EvaluationRunner:
    """离线评估：对数据集跑 agent，收集轨迹，计算指标。"""

    def __init__(self, cfg_mgr, hook_mgr):
        self._cfg_mgr = cfg_mgr
        self._hook_mgr = hook_mgr

    async def run(self, dataset_path: str, agent_name: str = "") -> dict:
        """跑完整评估。返回报告 dict。"""
        tasks = load_dataset(dataset_path)
        results = []

        logger.info(f"开始评估: {len(tasks)} 个任务, agent={agent_name or 'default'}")

        for i, task in enumerate(tasks, 1):
            logger.info(f"[{i}/{len(tasks)}] {task.task[:60]}")
            result = await self._run_single(task, agent_name)
            results.append(result)

        report = generate_report(results)
        logger.info(f"评估完成: 成功率={report['success_rate']}, 平均轮数={report['efficiency']['avg_turns']}")
        return report

    async def _run_single(self, task: EvalTask, agent_name: str = "") -> dict:
        """跑单个任务，返回 {task, passed, turns, tokens, tool_calls, category}。"""
        from agentforge.agents.agent_manager import AgentManager
        from agentforge.cli.runtime import TurnRunner, CancellationToken
        from agentforge.server.headless_cli import HeadlessCLI
        from agentforge.tools.tool_manager import ToolManager
        from agentforge.utils.permission_manager import PermissionManager, PermissionLevel

        cli = HeadlessCLI()
        perm = PermissionManager(PermissionLevel.YOLO)
        tool_mgr = ToolManager(perm_mgr=perm, hook_mgr=self._hook_mgr, cli=cli)
        agent_mgr = AgentManager(self._cfg_mgr, cli, tool_mgr)

        config = self._cfg_mgr.get_app_config()
        result_text = ""
        turns = 0
        tokens = 0
        tool_calls = 0

        try:
            if any(getattr(a, "role", "") == "coordinator" for a in config.agents):
                await agent_mgr.init_team()
            else:
                name = agent_name or config.default_agent or config.agents[0].agent_name
                await agent_mgr.init(name)

            runner = TurnRunner(agent_mgr=agent_mgr, hook_mgr=self._hook_mgr, cli=cli)
            cancel = CancellationToken()
            await runner.run_once(user_input=task.task, session_id="", cancel_token=cancel)

            # 从 history 取结果
            for msg in reversed(agent_mgr.current.conversation_history):
                if msg.role == "assistant" and msg.content and msg.content.strip():
                    result_text = msg.content
                    break
            if not result_text:
                for msg in agent_mgr.current.conversation_history:
                    if msg.role == "tool" and msg.content:
                        result_text += msg.content + "\n"

            turns = agent_mgr.current.current_turn_index
            try:
                tokens = agent_mgr.current._shared.token_usage.get("total", 0)
                tool_calls = agent_mgr.current._shared.budget.get("tool_calls", 0)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"任务执行失败: {e}")
            result_text = f"ERROR: {e}"
        finally:
            try:
                await agent_mgr.close()
            except Exception:
                pass

        # 判断是否通过（结果包含 expected 关键词）
        passed = False
        if task.expected:
            passed = task.expected in result_text.lower()
        else:
            passed = "ERROR" not in result_text

        # 检测循环（同一工具调用超过 5 次）
        tool_names = []
        for msg in agent_mgr.current.conversation_history:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_names.append(tc.name)
        has_loop = any(tool_names.count(t) > 5 for t in set(tool_names))

        return {
            "task": task.task,
            "result": result_text[:500],
            "expected": task.expected,
            "passed": passed,
            "turns": turns,
            "tokens": tokens,
            "tool_calls": tool_calls,
            "category": task.category,
            "has_loop": has_loop,
        }
