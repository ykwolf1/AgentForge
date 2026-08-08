# dataset.py 核心流程：评估数据集加载
import json
from pathlib import Path
from typing import List

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class EvalTask:
    """单个评估任务"""
    def __init__(self, task: str, expected: str = "", category: str = "general", max_turns: int = 10):
        self.task = task
        self.expected = expected.lower().strip()
        self.category = category
        self.max_turns = max_turns


def load_dataset(path: str) -> List[EvalTask]:
    """从 JSON 文件加载评估数据集。

    格式：
    [
      {"task": "...", "expected": "...", "category": "...", "max_turns": 5},
      ...
    ]
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"数据集不存在: {path}")

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    tasks = []
    for item in data:
        tasks.append(EvalTask(
            task=item.get("task", ""),
            expected=item.get("expected", ""),
            category=item.get("category", "general"),
            max_turns=item.get("max_turns", 10),
        ))

    logger.info(f"加载数据集: {path}, {len(tasks)} 个任务")
    return tasks
