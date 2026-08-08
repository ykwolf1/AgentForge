# metrics.py 核心流程：评估指标计算
from typing import List

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


def task_success_rate(results: List[dict]) -> float:
    """任务成功率：result 包含 expected 关键词 = 成功。"""
    if not results:
        return 0.0
    success = sum(1 for r in results if r.get("passed", False))
    return success / len(results)


def tool_efficiency(results: List[dict]) -> dict:
    """工具效率：平均轮数/平均 token/平均工具调用数。"""
    if not results:
        return {"avg_turns": 0, "avg_tokens": 0, "avg_tool_calls": 0}
    total_turns = sum(r.get("turns", 0) for r in results)
    total_tokens = sum(r.get("tokens", 0) for r in results)
    total_tools = sum(r.get("tool_calls", 0) for r in results)
    n = len(results)
    return {
        "avg_turns": round(total_turns / n, 1),
        "avg_tokens": round(total_tokens / n, 0),
        "avg_tool_calls": round(total_tools / n, 1),
    }


def trajectory_quality(results: List[dict]) -> dict:
    """轨迹质量：有没有循环/过度调用。"""
    if not results:
        return {"loops": 0, "over_calls": 0, "clean_rate": 0.0}
    loops = sum(1 for r in results if r.get("has_loop", False))
    over_calls = sum(1 for r in results if r.get("tool_calls", 0) > 10)
    clean = sum(1 for r in results if not r.get("has_loop", False) and r.get("tool_calls", 0) <= 10)
    n = len(results)
    return {
        "loops": loops,
        "over_calls": over_calls,
        "clean_rate": round(clean / n, 2) if n else 0.0,
    }


def by_category(results: List[dict]) -> dict:
    """按分类拆分成功率。"""
    cats = {}
    for r in results:
        cat = r.get("category", "general")
        if cat not in cats:
            cats[cat] = {"total": 0, "success": 0}
        cats[cat]["total"] += 1
        if r.get("passed", False):
            cats[cat]["success"] += 1
    for cat, v in cats.items():
        v["rate"] = round(v["success"] / v["total"], 2) if v["total"] else 0.0
    return cats


def generate_report(results: List[dict]) -> dict:
    """生成完整评估报告。"""
    return {
        "total_tasks": len(results),
        "success_rate": round(task_success_rate(results), 2),
        "efficiency": tool_efficiency(results),
        "trajectory": trajectory_quality(results),
        "by_category": by_category(results),
        "details": [
            {
                "task": r.get("task", "")[:80],
                "passed": r.get("passed", False),
                "turns": r.get("turns", 0),
                "tokens": r.get("tokens", 0),
                "tool_calls": r.get("tool_calls", 0),
                "category": r.get("category", ""),
            }
            for r in results
        ],
    }
