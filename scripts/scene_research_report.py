#!/usr/bin/env python
# scene_research_report.py —— 场景验证：行研报告写作
#
# 验证目的：验证 agent 系统的 skill 调用 + 多工具协同能力
#   ① Skill 能被自动发现并触发
#   ② 三个工具（web_search + knowledge_search + bash 画图）协同工作
#   ③ 三种图片来源：联网搜索、RAG 检索、代码生成
#
# 这是测试 harness，不是 agent 开发。
import asyncio
import os
import sys
import time
import uuid
import json
from pathlib import Path

# 让脚本能 import agentforge
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 注入 TAVILY_API_KEY（web_search 工具需要）
os.environ["TAVILY_API_KEY"] = "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"


# ===== 验证结果收集器 =====
class VerifyTracker:
    """收集 agent 运行轨迹，用于验证报告。"""

    def __init__(self):
        self.tool_calls = []        # [{name, args, result, success, duration}]
        self.text_chunks = []       # agent 输出的文本片段
        self.skill_triggered = False
        self.start_time = None
        self.end_time = None
        self.errors = []

    def record_tool_call(self, name, args, result, success, duration):
        self.tool_calls.append({
            "name": name,
            "args": str(args)[:200],   # 截断防止太长
            "result": str(result)[:200] if result else "",
            "success": success,
            "duration_ms": round(duration * 1000),
        })
        # 检测 skill 触发（agent 用 read_file 读 SKILL.md）
        if name == "read_file" and "research-report" in str(args):
            self.skill_triggered = True

    def summary(self) -> dict:
        tool_counts = {}
        for tc in self.tool_calls:
            tool_counts[tc["name"]] = tool_counts.get(tc["name"], 0) + 1

        # 检查三种工具是否都被调用
        has_web_search = any(tc["name"] == "web_search" for tc in self.tool_calls)
        has_kb_search = any(tc["name"] == "knowledge_search" for tc in self.tool_calls)
        has_bash = any(tc["name"] == "bash" for tc in self.tool_calls)

        # 检查报告是否含图片相关内容
        full_text = "".join(self.text_chunks)
        has_chart_ref = "/tmp/chart_" in full_text or "chart_" in full_text
        has_url_ref = "http" in full_text

        return {
            "total_duration_s": round(self.end_time - self.start_time, 1) if self.end_time else 0,
            "skill_triggered": self.skill_triggered,
            "total_tool_calls": len(self.tool_calls),
            "tool_call_counts": tool_counts,
            "web_search_used": has_web_search,
            "knowledge_search_used": has_kb_search,
            "bash_used": has_bash,
            "chart_reference_found": has_chart_ref,
            "url_reference_found": has_url_ref,
            "errors": self.errors,
            "report_length": len(full_text),
        }


async def run_scene():
    """主验证流程。"""
    print("=" * 70)
    print("场景验证：行研报告写作 —— 验证 agent 系统 skill + 多工具协同")
    print("=" * 70)

    # ===== 1. 前置检查 =====
    print("\n📋 前置检查...")

    # embedding 服务
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:8002/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                emb_ok = resp.status == 200
        print(f"  Embedding 服务 (8002): {'✅' if emb_ok else '❌'}")
    except Exception:
        emb_ok = False
        print("  Embedding 服务 (8002): ❌ 未启动")

    # Milvus
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    milvus_ok = sock.connect_ex(("localhost", 19530)) == 0
    sock.close()
    print(f"  Milvus (19530): {'✅' if milvus_ok else '❌'}")

    # Tavily key
    tavily_ok = bool(os.environ.get("TAVILY_API_KEY"))
    print(f"  TAVILY_API_KEY: {'✅' if tavily_ok else '❌'}")

    if not all([emb_ok, milvus_ok, tavily_ok]):
        print("\n❌ 前置检查未通过，请先启动依赖服务")
        return

    # ===== 2. 装配 agent =====
    print("\n🔧 装配 agent...")
    from agentforge.config.manager import ConfigManager
    from agentforge.agents.agent_manager import AgentManager
    from agentforge.cli.cli_console import CLIConsole
    from agentforge.tools.tool_manager import ToolManager
    from agentforge.hooks.config import load_hooks_config
    from agentforge.hooks.manager import HookManager
    from agentforge.utils.permission_manager import PermissionLevel, PermissionManager

    cfg_mgr = ConfigManager()
    args = type("Args", (), {"config": None, "agent": None, "permission_mode": None})()
    config = cfg_mgr.get_app_config(args)

    perm_mgr = PermissionManager(PermissionLevel.YOLO)
    cli = CLIConsole(perm_mgr)

    hooks_cfg = load_hooks_config(cfg_mgr.get_default_hooks_path())
    hook_mgr = HookManager(hooks_cfg)

    tool_mgr = ToolManager(perm_mgr=perm_mgr, hook_mgr=hook_mgr, cli=cli)
    tool_mgr.autodiscover()
    from agentforge.tools.tool_manager import TOOL_REGISTRY
    print(f"  工具注册: {len(TOOL_REGISTRY)} 个")

    # 初始化基础设施（知识库 + embedding）
    from agentforge.infra import InfraManager
    infra_config = config.infra if hasattr(config, "infra") and config.infra else {}
    infra = InfraManager(infra_config)
    infra_status = await infra.health_check_all()
    print(f"  基础设施: {infra_status}")

    # 注入沙箱到 BashTool（让 bash 工具走沙箱执行，不用主机 Python）
    if infra.sandbox and infra.sandbox.available:
        from agentforge.tools.execution.bash import BashTool
        BashTool.set_sandbox(infra.sandbox)
        print(f"  沙箱已注入 BashTool")

    # 注入知识库到全局（工具层通过 _get_kb() 访问）
    if infra.kb:
        from agentforge.knowledge import _kb_instance
        import agentforge.knowledge as kb_module
        kb_module._kb_instance = infra.kb
        print(f"  知识库已注入: available={infra.kb.milvus.available if infra.kb.milvus else False}")

    agent_mgr = AgentManager(cfg_mgr, cli, tool_mgr)
    session_id = f"scene-{str(uuid.uuid4())[:8]}"
    await agent_mgr.init(config.default_agent.lower(), session_id=session_id)
    print(f"  Agent 已就绪: {config.default_agent} (session={session_id})")

    # ===== 3. 发送任务 =====
    tracker = VerifyTracker()
    tracker.start_time = time.time()

    prompt = (
        "请用 research-report skill 写一份《企业级 AI Agent 系统架构设计》行研报告。"
        "要求：① 从知识库检索架构设计相关内容 ② 联网搜索 2026 最新趋势 "
        "③ 用代码生成至少 2 张图表 ④ 报告要包含三种来源的图片。"
    )
    print(f"\n📤 任务 prompt:\n  {prompt}\n")
    print("⏳ agent 运行中（可能需要 3-5 分钟）...\n")

    # ===== 4. 消费事件流，收集验证数据 =====
    current_tool = {}  # call_id → start_time
    turn_count = 0

    async for event in agent_mgr.agent_run(prompt):
        etype = event.type
        data = event.data or {}

        if etype == "tool.call":
            name = data.get("name", "")
            call_id = data.get("call_id", "")
            args = data.get("args", {})
            current_tool[call_id] = {"name": name, "args": args, "start": time.time()}
            print(f"  🔧 工具调用: {name}({str(args)[:80]}...)")

        elif etype == "tool.result":
            call_id = data.get("call_id", "")
            name = data.get("name", "")
            result = data.get("result", "")
            success = data.get("success", True)
            if call_id in current_tool:
                duration = time.time() - current_tool[call_id]["start"]
                args = current_tool[call_id]["args"]
                tracker.record_tool_call(name, args, result, success, duration)
                status = "✅" if success else "❌"
                print(f"  {status} 工具结果: {name} ({duration:.1f}s)")
                del current_tool[call_id]
            else:
                tracker.record_tool_call(name, {}, result, success, 0)

        elif etype == "text.delta":
            chunk = data.get("content", "")
            if chunk:
                tracker.text_chunks.append(chunk)

        elif etype == "text.done":
            content = data.get("content", "")

        elif etype == "reasoning.delta":
            pass  # 不收集思考过程

        elif etype == "turn.complete":
            turn_count += 1
            summary = data.get("summary", "")
            print(f"\n  --- 轮次 {turn_count} 完成 ---")

        elif etype == "task.complete":
            print(f"\n  🎯 任务完成")
            tracker.end_time = time.time()

        elif etype == "error":
            err = data.get("message", str(data))
            tracker.errors.append(err)
            print(f"\n  ❌ 错误: {err[:100]}")

        elif etype == "turn.max_reached":
            print(f"\n  ⚠️ 达到最大轮次限制")
            tracker.end_time = time.time()

    if tracker.end_time is None:
        tracker.end_time = time.time()

    # ===== 5. 输出验证结果 =====
    await agent_mgr.close()

    summary = tracker.summary()
    full_report = "".join(tracker.text_chunks)

    print("\n" + "=" * 70)
    print("验证结果")
    print("=" * 70)

    checks = [
        ("Skill 触发", summary["skill_triggered"]),
        ("knowledge_search 调用", summary["knowledge_search_used"]),
        ("web_search 调用", summary["web_search_used"]),
        ("bash 调用", summary["bash_used"]),
        ("图表引用", summary["chart_reference_found"]),
        ("URL 引用", summary["url_reference_found"]),
    ]

    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")

    print(f"\n  总耗时: {summary['total_duration_s']}s")
    print(f"  工具调用总数: {summary['total_tool_calls']}")
    print(f"  工具调用分布: {summary['tool_call_counts']}")
    print(f"  报告长度: {summary['report_length']} 字符")
    if summary["errors"]:
        print(f"  错误: {summary['errors']}")

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print(f"\n  验证通过: {passed}/{total}")

    # 保存完整报告
    report_dir = Path(__file__).resolve().parent.parent / "场景验证"
    report_dir.mkdir(exist_ok=True)

    # 保存 agent 生成的报告
    if full_report.strip():
        report_path = report_dir / "agent_输出报告.md"
        report_path.write_text(full_report, encoding="utf-8")
        print(f"\n  报告已保存: {report_path}")

    # 保存验证数据（JSON）
    verify_data = {
        "summary": summary,
        "tool_calls": tracker.tool_calls,
        "checks": {label: ok for label, ok in checks},
    }
    data_path = report_dir / "验证数据.json"
    data_path.write_text(json.dumps(verify_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  验证数据: {data_path}")

    # 检查 /tmp/ 下生成的图片
    charts = list(Path("/tmp").glob("chart_*.png"))
    if charts:
        print(f"\n  📊 生成的图表:")
        for c in charts:
            print(f"     {c} ({c.stat().st_size} bytes)")

    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(run_scene())
    sys.exit(0 if ok else 1)
