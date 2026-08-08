# verifier.py 核心流程：显式验证器 + Reflection 质量评估
#
#   补全"验证→纠正"闭环的后半圈：
#     ① ToolVerifier —— 机器级自动检查（edit 后跑 py_compile/pytest）
#     ② ReflectionEngine —— LLM 级质量评估（结果好不好，不达标带反馈重试）
#
#   设计：都走"回灌 + 下轮 LLM 自纠正"，不引入新循环结构
import asyncio
import json
from dataclasses import dataclass
from typing import Optional

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """验证结果"""
    passed: bool
    message: str = ""


@dataclass
class ReflectionResult:
    """反思结果"""
    acceptable: bool
    feedback: str = ""
    quality_score: float = 0.0


class ToolVerifier:
    """显式验证器：工具执行后自动检查结果。
    配置驱动——agent_config.verify_after 里配 {tool_name: "验证命令"}。
    如 verify_after: {edit: "python -m py_compile {file_path}"}"""

    def __init__(self):
        pass

    async def verify(self, tool_name: str, tool_args: dict, result: str, verify_config: dict) -> Optional[VerifyResult]:
        """跑配置的验证命令。没配置返回 None（跳过验证）。

        Args:
            tool_name: 刚执行的工具名（如 edit/write_file）
            tool_args: 工具参数（含 file_path 等，用于命令模板替换）
            result: 工具执行结果
            verify_config: agent_config.verify_after 字典
        """
        if not verify_config:
            return None

        cmd_template = verify_config.get(tool_name)
        if not cmd_template:
            return None  # 该工具没配验证命令

        # 模板替换：{file_path} → 实际参数值
        cmd = cmd_template
        for k, v in tool_args.items():
            cmd = cmd.replace("{" + k + "}", str(v))

        logger.info(f"[Verifier] {tool_name} 后验证: {cmd}")

        # 执行验证命令
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=30.0
            )
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode == 0:
                logger.info(f"[Verifier] 通过 (exit=0)")
                return VerifyResult(passed=True, message=stdout_text or "OK")
            else:
                err = stderr_text or stdout_text or f"exit code {process.returncode}"
                logger.warning(f"[Verifier] 失败: {err[:100]}")
                return VerifyResult(passed=False, message=err)
        except asyncio.TimeoutError:
            return VerifyResult(passed=False, message="验证命令超时（30s）")
        except Exception as e:
            return VerifyResult(passed=False, message=f"验证执行异常: {e}")


class ReflectionEngine:
    """质量评估：用 LLM 评估工具结果质量，不达标带反馈重试。

    闭环：执行 → 评估 → 不达标 → 回灌反馈 → LLM 改进 → 再评估
    限制：max_reflections 次后接受当前结果（防无限循环）
    """

    REFLECTION_PROMPT = """You are a quality evaluator. Evaluate whether the tool result satisfies the task requirement.

Task: {task}
Tool result: {result}

Evaluate on these criteria:
1. Does the result address the task?
2. Is the result correct/complete?
3. Are there obvious errors?

Respond in JSON format ONLY:
{{"quality_score": 0-100, "acceptable": true/false, "feedback": "brief feedback if not acceptable"}}

Score guide: 80+ = acceptable, 60-79 = needs improvement, <60 = failed.
If acceptable, feedback should be empty.
"""

    def __init__(self, llm_client, max_reflections: int = 2):
        self.llm = llm_client
        self.max = max_reflections
        self._count = 0

    def reset(self):
        """每个新任务重置计数"""
        self._count = 0

    @property
    def count(self):
        return self._count

    @property
    def exhausted(self):
        return self._count >= self.max

    async def reflect(self, task: str, result: str, model: str = "") -> Optional[ReflectionResult]:
        """让 LLM 评估结果质量。"""
        if not self.llm or not result:
            return None

        prompt = self.REFLECTION_PROMPT.format(
            task=task[:500],
            result=result[:1000],
        )

        messages = [{"role": "user", "content": prompt}]

        try:
            from agentforge.llm.llm_events import LLM_Events
            buf = []
            async for event in self.llm.astream_response(messages=messages, tools=[], api="chat"):
                if event.type == LLM_Events.ASSISTANT_DELTA and event.data:
                    buf.append(str(event.data))
                elif event.type in (LLM_Events.RESPONSE_FINISHED, LLM_Events.ERROR):
                    break

            response = "".join(buf).strip()
            self._count += 1
            logger.info(f"[Reflection] 第 {self._count}/{self.max} 次评估")

            # 解析 JSON 响应
            # LLM 可能返回带 markdown 代码块的 JSON
            if "```" in response:
                # 提取代码块内的 JSON
                import re
                match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
                if match:
                    response = match.group(1)

            parsed = json.loads(response)
            score = float(parsed.get("quality_score", 0))
            acceptable = parsed.get("acceptable", False)
            feedback = parsed.get("feedback", "")

            # 80 分以上算达标
            if score >= 80:
                acceptable = True

            logger.info(f"[Reflection] score={score}, acceptable={acceptable}")
            return ReflectionResult(
                acceptable=acceptable,
                feedback=feedback,
                quality_score=score,
            )
        except json.JSONDecodeError:
            logger.warning(f"[Reflection] LLM 响应不是有效 JSON，跳过评估")
            return None
        except Exception as e:
            logger.warning(f"[Reflection] 评估异常: {e}")
            return None
