# hooks/manager.py 核心流程：跑外部脚本，按脚本输出决定是否阻断 / 注入上下文
#
#   emit(event, payload)
#      ↓ 构造 payload {session_id, cwd, tool_name?, tool_input?, tool_response?}
#      ↓ 遍历该 event 配置的 hook 组（Pre/PostToolUse 先按 matcher 过滤工具名）
#      ↓ 每条 hook：subprocess 跑脚本，stdin 喂 payload JSON
#      ↓ 解析脚本输出（JSON 或 exit code）：
#         continue:false / permissionDecision:deny / decision:block / exit 2 → 阻断
#         additionalContext → 累积，回灌给 LLM
#         systemMessage → 累积，展示给用户
#      ↓ 首个阻断即短路返回 (False, msg, extra)；否则返回 (True, ...)
#
#   关键：
#     - hook 是外部子进程（shell），组织级强制策略，独立于权限档
#     - 阻断方式有多种（JSON 字段或 exit code 2），都汇聚到 (False, reason)
#     - PreToolUse 在用户审批之前（组织策略优先级更高）
#     - ⚠ hooks/integrate.py 是死代码；middleware.py 未接入 runtime
#
#   emit 调用点：
#     SessionStart      main.py:83        注入 additionalContext
#     UserPromptSubmit  runtime.py:128    可拦下这次输入
#     PreToolUse        tool_manager.py   第①道门（阻断工具）
#     PostToolUse       tool_manager.py   第④道门（覆盖结果）
#     Stop              runtime.py:143    注入 additionalContext
#
#   代码位置：
#     emit          hooks/manager.py:16  (决策引擎)
#     run_command   hooks/runner.py      (subprocess 执行)
#     matches_tool  hooks/matcher.py     (工具名匹配)
# manager.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .matcher import matches_tool
from .models import HookEvent, HooksConfig
from .runner import run_command_hook_async


class HookManager:
    def __init__(self, config: HooksConfig):
        self.config = config

    async def emit(
        self,
        event: HookEvent,
        base_payload: Dict[str, Any],
        tool_name: Optional[str] = None,
        tool_input: Optional[Dict[str, Any]] = None,
        tool_response: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        groups = self.config.hooks.get(event.value, [])
        extra: Dict[str, Any] = {}
        user_msg: Optional[str] = None
        continue_ok = True

        payload = {
            "session_id": base_payload.get("session_id", ""),
            "cwd": base_payload.get("cwd", str(Path.cwd())),
            "hook_event_name": event.value,
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        if tool_input is not None:
            payload["tool_input"] = tool_input
        if tool_response is not None:
            payload["tool_response"] = tool_response

        payload.update({k: v for k, v in base_payload.items() if k not in payload})

        for group in groups:
            if (
                event in (HookEvent.PreToolUse, HookEvent.PostToolUse)
                and (not tool_name or not matches_tool(group.matcher, tool_name))
            ):
                continue

            for cmd in group.hooks:
                res = await run_command_hook_async(
                    cmd=cmd.command,
                    payload=payload,
                    timeout=cmd.timeout,
                )
                if res.json_out:
                    cont = res.json_out.get("continue")
                    if cont is False:
                        continue_ok = False
                        user_msg = res.json_out.get("stopReason") or user_msg

                    sysmsg = res.json_out.get("systemMessage")
                    if sysmsg:
                        user_msg = (user_msg or "") + (("\n" if user_msg else "") + sysmsg)

                    hso = res.json_out.get("hookSpecificOutput", {})
                    if event == HookEvent.PreToolUse:
                        pd = hso.get("permissionDecision")
                        reason = hso.get("permissionDecisionReason")
                        if pd == "deny":
                            continue_ok = False
                            user_msg = reason or user_msg
                        elif pd == "ask":
                            continue_ok = False
                            user_msg = reason or "Tool call requires confirmation."
                    elif event == HookEvent.PostToolUse:
                        decision = res.json_out.get("decision")
                        if decision == "block":
                            continue_ok = False
                            user_msg = res.json_out.get("reason") or user_msg
                        add_ctx = hso.get("additionalContext")
                        if add_ctx:
                            extra.setdefault("additionalContext", "")
                            extra["additionalContext"] += (("\n" if extra["additionalContext"] else "") + add_ctx)
                    elif event == HookEvent.UserPromptSubmit:
                        decision = res.json_out.get("decision")
                        if decision == "block":
                            continue_ok = False
                            user_msg = res.json_out.get("reason") or user_msg
                        else:
                            add_ctx = hso.get("additionalContext")
                            if add_ctx:
                                extra.setdefault("additionalContext", "")
                                extra["additionalContext"] += (("\n" if extra["additionalContext"] else "") + add_ctx)
                    elif event in (HookEvent.Stop, HookEvent.SubagentStop):
                        decision = res.json_out.get("decision")
                        if decision == "block":
                            continue_ok = False
                            user_msg = res.json_out.get("reason") or user_msg
                    elif event == HookEvent.SessionStart:
                        add_ctx = hso.get("additionalContext")
                        if add_ctx:
                            extra.setdefault("additionalContext", "")
                            extra["additionalContext"] += (("\n" if extra["additionalContext"] else "") + add_ctx)
                else:
                    if res.exit_code == 0:
                        pass
                    elif res.exit_code == 2:
                        continue_ok = False
                        user_msg = (user_msg or "") + (("\n" if user_msg else "") + (res.stderr or "Hook blocked."))
                    else:
                        user_msg = (user_msg or "") + (("\n" if user_msg else "") + (res.stderr or "Hook error."))

                if not continue_ok:
                    return continue_ok, user_msg, extra

        return continue_ok, user_msg, extra
