# context/__init__.py —— 上下文管理模块
#
#   参照腾讯 TencentDB Agent Memory 的设计思路：
#     ① 短期符号化记忆：工具结果卸载到存储层，上下文只保留轻量符号
#     ② 长期分层记忆：原始会话（L0）→ 原子事实（L1）→ 场景记忆（L2）→ 核心画像（L3）
#
#   本次实现（短期符号化记忆）：
#     ToolResultStore  —— 工具结果卸载到磁盘（L0 原始证据层）
#     ContextManager   —— 管理工具结果的符号化回灌 + 按需检索
#
#   核心原则：
#     - 原始工具结果（web_search 返回、write_file 内容等）不进 conversation_history
#     - 上下文只保留符号化摘要："结果存了什么、存在哪、关键结论是什么"
#     - agent 需要原始数据时，通过 recall_tool_result 按需调取
from .store import ToolResultStore, ToolResultEntry
from .manager import ContextManager, get_context_manager

__all__ = ["ToolResultStore", "ToolResultEntry", "ContextManager", "get_context_manager"]
