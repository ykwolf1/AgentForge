# tool_manager.py 核心流程：管两件事——① 启动时登记所有工具 ② 每次调用走四道关卡
#
#   阶段一：登记（启动时，autodiscover 扫 agentforge/tools/*.py，import 触发 @register_tool 入册）
#      ↓
#   阶段二：分派（agent 每轮调 list_for_provider，按 provider/白名单/只读过滤）
#      ↓  模型说"我要调 bash"
#   阶段三：execute 四道关卡（顺序不能换）：
#      ┌─→ ① PreToolUse hook   组织级策略，deny 直接拒（不问用户）
#      │     ↓
#      │   ② ApprovalService   按 YOLO/SAFE/权限档决定自动批或弹 y/n/a
#      │     ↓
#      │   ③ tool.execute()    真正执行，无 try/except（异常冒泡到 agent）
#      │     ↓
#      │   ④ PostToolUse hook  检查结果，不过则覆盖
#      └─→ 返回 (success, result)；失败也返回（让 agent 回灌给 LLM 自纠正）
#
#   关键：
#     - 四道关顺序是踩坑加的：hook 在审批前（组织策略优先）、审批在执行前（执行了就晚了）
#     - 失败/拒绝/异常都返回 (False, reason)，由 agent 把 reason 当 tool 消息回灌
#     - ⚠ error 文本在 tuple 里丢弃，真实 error 留在 ToolCallResult 上
#
#   代码位置：
#     TOOL_REGISTRY       tool_manager.py:149  (花名册)
#     register_tool       tool_manager.py:209  (装饰器)
#     autodiscover        tool_manager.py:241  (启动扫描)
#     execute（四道关卡） tool_manager.py:execute
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Type

from agentforge.cli.cli_console import CLIConsole
from agentforge.hooks.manager import HookManager
from agentforge.hooks.models import HookEvent
from agentforge.tools.base_tool import BaseTool, ToolRiskLevel
from agentforge.utils.permission_manager import PermissionManager


# ToolEntry —— 花名册里的一条记录（工具实例 + 管理属性）
@dataclass
class ToolEntry:
    instance: BaseTool                              # 工具实例
    providers: Set[str]                             # 可用 provider；{"*"}=全部
    risk: ToolRiskLevel = ToolRiskLevel.SAFE        # 风险等级（影响是否弹审批）
    enabled: bool = True                            # 是否启用


# 全局花名册 {工具名 → ToolEntry}，import 时由装饰器填充
TOOL_REGISTRY: Dict[str, ToolEntry] = {}


# register_instance —— 运行时注册入口（静态工具经 @register_tool 走这，动态工具 MCP 直接调）
def register_instance(
    *,
    name: str,                                      # 工具唯一名（LLM 可见）
    instance: BaseTool,                             # 工具实例
    providers: Iterable[str] | str = "*",           # 可用 provider；"*"=全部
    enabled: bool = True,
    overwrite: bool = False,                        # 同名：False=报错, True=覆盖
) -> None:
    if not isinstance(instance, BaseTool):
        raise TypeError("register_instance: 'instance' must be BaseTool")

    if not name:
        raise ValueError("register_instance: 'name' is required")

    if name in TOOL_REGISTRY and not overwrite:
        raise ValueError(f"register_instance: duplicate tool name '{name}' (use overwrite=True to replace)")

    provs = {"*"} if providers == "*" else set(providers)
    risk = getattr(instance, "risk_level", ToolRiskLevel.SAFE)

    TOOL_REGISTRY[name] = ToolEntry(
        instance=instance,
        providers=provs,
        risk=risk,
        enabled=enabled,
    )

def unregister_tool(name: str) -> bool:
    """卸载工具，返回是否确实删除了某项。"""
    return TOOL_REGISTRY.pop(name, None) is not None

def is_registered(name: str) -> bool:
    """工具是否已注册（仅看名字）。"""
    return name in TOOL_REGISTRY

def list_tool_names() -> List[str]:
    """列出所有已注册工具名（调试用）。"""
    return list(TOOL_REGISTRY.keys())

def get_entry(name: str) -> ToolEntry:
    """取花名册里的整条记录（含元信息）。"""
    return TOOL_REGISTRY[name]

# replace_instance —— 热替换工具实例（保留 providers/enabled），预留 API，无调用方
def replace_instance(name: str, instance: BaseTool) -> None:
    if name not in TOOL_REGISTRY:
        raise KeyError(f"replace_instance: tool '{name}' not found")
    entry = TOOL_REGISTRY[name]
    TOOL_REGISTRY[name] = ToolEntry(
        instance=instance,
        providers=entry.providers,
        risk=getattr(instance, "risk_level", entry.risk),
        enabled=entry.enabled,
    )

# register_tool —— 静态工具注册装饰器，import 时触发自动入册
def register_tool(*, name: str, providers: Iterable[str] | str = '*', enabled: bool = True):
    def deco(cls: Type[BaseTool]):
        if not issubclass(cls, BaseTool):
            raise TypeError("@register_tool must decorate BaseTool subclasses")
        tool = cls()
        tool.name = name

        register_instance(
            name=name,
            instance=tool,
            providers=providers,
            enabled=enabled,
            overwrite=False,
        )
        return cls
    return deco


# ToolManager —— 工具调度员，持有 perm/hook/cli 三个依赖，提供注册扫描、按名/按 provider 取工具、四道关卡执行
class ToolManager:
    def __init__(
        self,
        perm_mgr: PermissionManager | None = None,    # 权限管理（YOLO/LOCKED 等档位）
        hook_mgr: HookManager | None = None,          # hook 管理器
        cli: CLIConsole | None = None,                # CLI（弹审批框）
    ):
        self.perm_mgr = perm_mgr
        self.hook_mgr = hook_mgr
        self.cli = cli

    @staticmethod
    def autodiscover(package: str = "agentforge.tools") -> None:
        """扫描 agentforge/tools/ 下所有文件 import，触发 @register_tool 自动入册。"""
        pkg = importlib.import_module(package)
        for _, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
            if ispkg:
                ToolManager.autodiscover(f"{package}.{modname}")
                continue
            if modname in {"base_tool", "tool_manager", "mcp_tool"}:
                continue
            importlib.import_module(f"{package}.{modname}")

    @staticmethod
    def get_tool(name: str) -> BaseTool:
        """按名字取工具实例。找不到 KeyError；被禁用 RuntimeError。"""
        entry = TOOL_REGISTRY.get(name)
        if not entry:
            raise KeyError(f"Tool '{name}' not found")
        if not entry.enabled:
            raise RuntimeError(f"Tool '{name}' is registered but disabled")
        return entry.instance

    @staticmethod
    def list_for_provider(provider: str, allowlist: Optional[Iterable[str]] = None, safe_mode: bool = False,) -> List[BaseTool]:
        """列出指定 provider 可用的工具，三层过滤：enabled / provider 匹配 / 白名单+只读模式。"""
        allowset = set(allowlist) if allowlist else None
        out: List[BaseTool] = []
        for name, entry in TOOL_REGISTRY.items():
            if not entry.enabled:
                continue
            if '*' not in entry.providers and provider not in entry.providers:
                continue
            if allowset and name not in allowset:
                continue
            if safe_mode and entry.risk != ToolRiskLevel.SAFE:
                continue
            out.append(entry.instance)
        return out

    # execute —— 工具执行管线，四道关卡：① PreToolUse hook ② 用户审批 ③ 执行 ④ PostToolUse hook
    # 返回 (success, result)；⚠ error 文本丢弃，真实 error 在 res 上
    async def execute(self, tool_name: str,  tool_args: Dict[str, Any], tool: BaseTool, **kwargs ) -> Tuple[bool, Optional[str | Dict]]:
        # session_id 从 kwargs 取（agent 调用时传），用于 hook payload
        session_id = kwargs.get("session_id", "")

        # ① PreToolUse hook —— 组织级策略，deny 直接拒
        if self.hook_mgr:
            pre_ok, pre_msg, _ = await self.hook_mgr.emit(
                HookEvent.PreToolUse,
                base_payload={"session_id": session_id},
                tool_name=tool_name,
                tool_input=tool_args,
            )
            if not pre_ok:
                blocked_reason = pre_msg or "Tool call blocked by PreToolUse hook"
                return False, blocked_reason

        # ② 用户审批 —— 内部按 YOLO/SAFE/权限档决定自动放行或弹 y/n/a
        if self.cli:
            is_approved = await self.cli.confirm_tool_call(tool_name, tool_args, tool)
            if not is_approved:
                return False, f"'{tool_name}' was rejected by the user."

        # ③ 执行 —— 无 try/except，异常冒泡到 agent 让模型下轮自纠正；kwargs 透传 agent=self 给 TaskTool
        res = await tool.execute(**tool_args, **kwargs)

        # ④ PostToolUse hook —— 检查结果（如输出含密码），不过则覆盖
        if self.hook_mgr:
            post_ok, post_msg, _ = await self.hook_mgr.emit(
                HookEvent.PostToolUse,
                base_payload={"session_id": session_id},
                tool_name=tool_name,
                tool_input=tool_args,
                tool_response={"result": res.result, "success": res.success, "error": res.error},
            )
            if not post_ok:
                reason = post_msg or "PostToolUse hook blocked further processing"
                res.error = reason
                res.result = None

        return res.success, res.result
