# app.py 核心流程：FastAPI 服务化 + 并发隔离 + 异步任务
#
#   两套 API：
#     同步（旧）：POST /agent/run → 阻塞等结果
#     异步（新）：POST /agent/tasks → 提交拿 task_id → GET 查进度 → cancel
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agentforge.agents.agent_manager import AgentManager
from agentforge.cli.runtime import TurnRunner, CancellationToken
from agentforge.config.manager import ConfigManager
from agentforge.hooks.config import load_hooks_config
from agentforge.hooks.manager import HookManager
from agentforge.tools.tool_manager import ToolManager
from agentforge.utils.permission_manager import PermissionManager, PermissionLevel

from .headless_cli import HeadlessCLI
from .task_manager import TaskManager, _task_mgr

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)

# ===== 全局共享（无状态）=====
import os
_cfg_mgr = ConfigManager()
_hook_mgr = HookManager(load_hooks_config(_cfg_mgr.get_default_hooks_path()))

# API 鉴权（设了 AGENTFORGE_API_TOKEN 才启用）
_API_TOKEN = os.getenv("AGENTFORGE_API_TOKEN", "")

app = FastAPI(title="AgentForge API", version="3.0.0")

# 基础设施管理器（启动时初始化 + 健康检查）
_infra = None
_redis_for_agents = None   # Redis 客户端（给 AgentManager 注入工作记忆）


# ===== 鉴权中间件 =====
@app.middleware("http")
async def auth_middleware(request, call_next):
    if _API_TOKEN and request.url.path.startswith("/agent/"):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        # 安全比较（防时序攻击）：不用 != 而用 hmac.compare_digest
        import hmac
        if not hmac.compare_digest(token, _API_TOKEN):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)


# ===== 请求/响应模型 =====
class RunRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    agent: Optional[str] = None
    permission: str = "yolo"


class RunResponse(BaseModel):
    session_id: str
    status: str
    result: str
    token_usage: dict
    turns: int


# ===== 同步端点（保留向后兼容）=====

@app.get("/agent/health")
async def health():
    return {"status": "ok", "model": _cfg_mgr.get_active_model_name() or "unknown",
            "tasks": _task_mgr.get_stats()}


@app.get("/agent/sessions")
async def list_sessions():
    from agentforge.memory.store import MemoryStore, get_memory_store
    store = get_memory_store()
    sids = store.list_sessions()
    return {"sessions": [{"session_id": s, "messages": len(store.load_session(s))} for s in sids]}


@app.post("/agent/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    """同步执行（阻塞等结果）。每请求独立 AgentManager。"""
    session_id = req.session_id or str(uuid.uuid4())[:8]
    cli = HeadlessCLI()
    perm = PermissionManager(
        PermissionLevel(req.permission) if req.permission in ("yolo", "locked", "edit_only", "planning")
        else PermissionLevel.YOLO
    )
    tool_mgr = ToolManager(perm_mgr=perm, hook_mgr=_hook_mgr, cli=cli)
    agent_mgr = AgentManager(_cfg_mgr, cli, tool_mgr)
    if _redis_for_agents:
        agent_mgr.set_redis(_redis_for_agents)

    config = _cfg_mgr.get_app_config()
    try:
        if any(getattr(a, "role", "") == "coordinator" for a in config.agents):
            await agent_mgr.init_team(session_id=session_id)
        else:
            await agent_mgr.init(
                (req.agent or config.default_agent or "agentforge").lower(),
                session_id=session_id,
            )
    except Exception as e:
        return RunResponse(session_id=session_id, status="error", result=f"init failed: {e}", token_usage={}, turns=0)

    result_text = ""
    status = "completed"
    turns = 0

    try:
        runner = TurnRunner(agent_mgr=agent_mgr, hook_mgr=_hook_mgr, cli=cli)
        cancel = CancellationToken()
        outcome = await runner.run_once(user_input=req.prompt, session_id=session_id, cancel_token=cancel)
        status = outcome.end.value if hasattr(outcome.end, 'value') else str(outcome.end)

        for msg in reversed(agent_mgr.current.conversation_history):
            if msg.role == "assistant" and msg.content and msg.content.strip():
                result_text = msg.content
                break
        if not result_text:
            for msg in agent_mgr.current.conversation_history:
                if msg.role == "tool" and msg.content:
                    result_text += msg.content + "\n"
        turns = agent_mgr.current.current_turn_index
    except Exception as e:
        status = "error"
        result_text = f"execution failed: {e}"
    finally:
        await agent_mgr.close()

    token_usage = {}
    try:
        token_usage = dict(agent_mgr.current._shared.token_usage) if agent_mgr.current else {}
    except Exception:
        pass

    return RunResponse(session_id=session_id, status=status, result=result_text or "(no output)",
                       token_usage=token_usage, turns=turns)


# ===== 异步任务端点（新增）=====

@app.post("/agent/tasks")
async def submit_task(req: RunRequest):
    """提交异步任务。立即返回 task_id，后台执行。"""
    task_id = await _task_mgr.submit(
        prompt=req.prompt,
        session_id=req.session_id,
        agent_name=req.agent or "",
        permission=req.permission,
    )
    return {"task_id": task_id, "status": "pending", "message": "任务已提交，用 GET /agent/tasks/{task_id} 查进度"}


# ===== Agent Registry 端点 =====

@app.get("/agent/registry")
async def list_registry(capability: str = "", role: str = ""):
    """列出所有注册的 agent，可按 capability/role 过滤。"""
    from agentforge.agents.registry import get_registry
    reg = get_registry()
    if capability or role:
        records = reg.find(capability=capability, role=role)
        return {"agents": [r.to_dict() for r in records]}
    return reg.to_dict()


@app.get("/agent/registry/{name}")
async def get_registry_entry(name: str):
    """查某个 agent 的注册信息。"""
    from agentforge.agents.registry import get_registry
    record = get_registry().get(name)
    if not record:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' 未注册")
    return record.to_dict()


@app.get("/agent/tasks/{task_id}")
async def get_task(task_id: str):
    """查任务进度/结果。"""
    task = _task_mgr.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return task.to_dict()


@app.post("/agent/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """取消任务。"""
    ok = _task_mgr.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或已完成")
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/agent/tasks")
async def list_tasks():
    """列所有任务。"""
    return {"tasks": [t.to_dict() for t in _task_mgr.list_all()], "stats": _task_mgr.get_stats()}


@app.on_event("startup")
async def startup():
    global _infra
    _task_mgr.setup(_cfg_mgr, _hook_mgr)

    # 基础设施健康检查
    from agentforge.infra import InfraManager
    infra_config = (_cfg_mgr.get_app_config().infra or {})
    _infra = InfraManager(infra_config)
    infra_status = await _infra.health_check_all()

    # 注入 Redis 到 TaskManager（不可用时降级到内存）
    if _infra.redis and _infra.redis.available:
        _task_mgr.set_redis(_infra.redis)
        logger.info("[Startup] TaskManager 已接入 Redis")

    # 记录 Redis 可用性（run_agent / submit_task 时给 AgentManager 注入工作记忆）
    global _redis_for_agents
    _redis_for_agents = _infra.redis if (_infra.redis and _infra.redis.available) else None

    # 注入沙箱到 BashTool（不可用时降级到本地 bash）
    if _infra.sandbox and _infra.sandbox.available:
        from agentforge.tools.execution.bash import BashTool
        BashTool.set_sandbox(_infra.sandbox)
        logger.info("[Startup] BashTool 已接入沙箱（代码执行隔离）")

    auth_status = "ON" if _API_TOKEN else "OFF"
    logger.info(f"AgentForge API 启动 | model={_cfg_mgr.get_active_model_name()} | auth={auth_status} | infra={infra_status}")


@app.get("/agent/infra")
async def infra_status():
    """基础设施状态。"""
    if _infra:
        return {"infra": _infra.status()}
    return {"infra": {"sandbox": False, "redis": False, "chromadb": False}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
