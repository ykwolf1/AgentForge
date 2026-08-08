# context/manager.py —— 上下文管理器（短期符号化记忆）
#
#   核心职责：
#     1. 工具结果卸载：完整结果 → ToolResultStore，上下文只放符号化摘要
#     2. 符号生成：把工具结果压缩成"存了什么 + 关键结论 + ref_id"
#     3. 按需调取：agent 需要原始数据时，用 ref_id 从存储层取回
#
#   设计参照：
#     腾讯 TencentDB Agent Memory 的"符号化记忆"：
#     "上下文只保留轻量化结构化任务画布，记录任务流程、状态、关键结论。
#      Agent 如需原始证据，可按需触发检索调取完整日志。"
import json
from typing import Any, Dict, List, Optional, Tuple

from .store import ToolResultStore, ToolResultEntry

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


# 默认阈值（可被配置覆盖）
_DEFAULT_OFFLOAD_THRESHOLD = 1500


class ContextManager:
    """上下文管理器：工具结果卸载 + 符号化回灌。"""

    def __init__(self, store: Optional[ToolResultStore] = None, offload_threshold: int = None):
        self.store = store or ToolResultStore()
        # 从配置读阈值，降级到默认值（修复⑨：不再硬编码）
        self._offload_threshold = offload_threshold or _DEFAULT_OFFLOAD_THRESHOLD

    def should_offload(self, result_str: str) -> bool:
        """判断工具结果是否需要卸载（超过阈值就卸载）。"""
        return len(result_str) > self._offload_threshold

    def offload_result(
        self, tool_name: str, call_id: str, arguments: Dict[str, Any],
        result: Any, success: bool,
    ) -> Tuple[str, str]:
        """卸载工具结果到存储层，返回 (上下文用的符号化内容, ref_id)。

        返回的符号化内容会替代原始结果进入 conversation_history。
        格式：
          ✅ web_search("2026趋势") → 5 条结果。
          关键发现:
            1. 2026 AI Agent 市场规模 120 亿美元
            2. ...
          📎 完整结果已保存 (ref: ctx_xxx)，需要详情时调 recall_tool_result(ref_id="ctx_xxx")
        """
        result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

        # 生成符号化摘要
        summary, key_facts = self._generate_symbol(tool_name, arguments, result_str, success)

        # 卸载到存储层
        ref_id = self.store.store(
            tool_name=tool_name, call_id=call_id, arguments=arguments,
            result=result, success=success, summary=summary, key_facts=key_facts,
        )

        # 构造上下文用的符号化内容
        status_icon = "✅" if success else "❌"
        lines = [f"{status_icon} {summary}"]
        if key_facts:
            lines.append("  关键发现:")
            for i, fact in enumerate(key_facts, 1):
                lines.append(f"    {i}. {fact}")
        lines.append(f"  📎 完整结果已卸载 (ref: {ref_id})，需要原始数据时调 recall_tool_result(ref_id=\"{ref_id}\")")

        symbol = "\n".join(lines)
        logger.info(f"[ContextMgr] 工具结果卸载: {tool_name} {len(result_str)}字→{len(symbol)}字 (ref={ref_id})")
        return symbol, ref_id

    def recall(self, ref_id: str) -> Optional[str]:
        """按 ref_id 取回完整工具结果（agent 按需调取）。"""
        entry = self.store.retrieve(ref_id)
        if entry is None:
            return f"未找到 ref_id={ref_id} 的工具结果"
        if isinstance(entry.result, str):
            return entry.result
        return json.dumps(entry.result, ensure_ascii=False)

    def _generate_symbol(
        self, tool_name: str, args: Dict[str, Any], result_str: str, success: bool,
    ) -> Tuple[str, List[str]]:
        """为工具结果生成符号化摘要 + 关键事实。

        这是不调 LLM 的轻量摘要——用启发式规则从结果里提取关键信息。
        理由：调 LLM 做摘要会增加延迟和成本，而且摘要本身也需要上下文（死循环）。
        启发式规则对结构化结果（web_search 的编号列表、knowledge_search 的分段）效果很好。
        """
        summary = ""
        key_facts: List[str] = []

        if not success:
            reason = result_str[:100] if result_str else "未知错误"
            return f"{tool_name} 执行失败: {reason}", []

        # 提取查询参数（如果有）
        query = args.get("query", args.get("command", args.get("path", "")))
        query_preview = str(query)[:60] if query else ""

        if tool_name == "web_search":
            # web_search 结果格式："Web search results...\n[1] title\n[2] title..."
            num = result_str.count("[Answer]") + result_str.count("\n[")
            num = max(num, result_str.count("\n[1]"), result_str.count("\n[2]"))
            # 提取前 3 条标题
            lines = result_str.split("\n")
            titles = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("[") and "]" in stripped and "http" not in stripped[:20]:
                    title = stripped.split("]", 1)[1].strip()[:80] if "]" in stripped else ""
                    if title:
                        titles.append(title)
                elif stripped.startswith("📝") or stripped.startswith("Answer"):
                    content = stripped[:80]
                    if content:
                        titles.append(content)
            summary = f"web_search(\"{query_preview}\") → {len(titles)} 条结果"
            key_facts = titles[:3]

        elif tool_name == "knowledge_search":
            # knowledge_search 结果格式："找到 N 条结果:\n--- 结果 1 ---
            num_results = result_str.count("--- 结果")
            # 提取 section 和文本片段
            sections = []
            parts = result_str.split("--- 结果")
            for part in parts[1:4]:  # 前 3 条
                lines = part.strip().split("\n")
                for line in lines:
                    if "section" in line.lower() or "章节" in line:
                        sections.append(line.strip()[:80])
                        break
                    elif "来源" in line:
                        sections.append(line.strip()[:80])
                        break
            summary = f"knowledge_search(\"{query_preview}\") → {num_results} 条知识库结果"
            key_facts = sections[:3]

        elif tool_name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            lines_count = len(str(content).splitlines())
            summary = f"write_file → {path} ({lines_count} 行, {len(str(content))} 字符)"
            key_facts = [f"文件已写入: {path}"]

        elif tool_name == "bash":
            command = args.get("command", "")
            # 取输出的前几行作为关键事实
            output_lines = result_str.strip().split("\n")
            meaningful = [l.strip() for l in output_lines if l.strip() and "WARNING" not in l][:3]
            cmd_preview = str(command)[:60].replace("\n", " ")
            summary = f"bash(\"{cmd_preview}\") → 执行完成"
            key_facts = meaningful

        elif tool_name == "read_file":
            path = args.get("path", "")
            summary = f"read_file → {path} ({len(result_str)} 字符)"
            # 取前 3 行作为预览
            preview_lines = result_str.strip().split("\n")[:3]
            key_facts = [l[:80] for l in preview_lines if l.strip()]

        else:
            # 通用：取结果的前 200 字符作为摘要
            preview = result_str[:200].replace("\n", " ")
            summary = f"{tool_name}(\"{query_preview}\") → {len(result_str)} 字符结果"
            key_facts = [preview] if preview else []

        return summary, key_facts


# 全局单例（agent 启动时初始化）
_ctx_manager: Optional[ContextManager] = None


def get_context_manager() -> ContextManager:
    global _ctx_manager
    if _ctx_manager is None:
        _ctx_manager = ContextManager()
    return _ctx_manager


def set_context_manager(mgr: ContextManager) -> None:
    global _ctx_manager
    _ctx_manager = mgr
