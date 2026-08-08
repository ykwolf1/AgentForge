# headless_cli.py 核心流程：API 模式的无头 CLI
#
#   和 CLIConsole 的区别：
#     - confirm_tool_call 直接返回 True（不弹窗，API 模式默认自动批）
#     - print 记日志（不输出到终端）
#     - 不实现交互 UI 相关方法（API 模式不需要）
#
#   agent.py 调用的 CLI 方法全在这里 stub 掉
try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class HeadlessCLI:
    """API/headless 模式的 CLI 替身。
    所有终端 UI 方法降级为 no-op 或日志，审批全部自动通过。"""

    def __init__(self):
        self._current_tokens = 0
        self._max_tokens = 0

    # ===== agent.py 用到的方法 =====
    async def confirm_tool_call(self, tool_name, tool_args, tool=None) -> bool:
        # API 模式不弹窗，全部自动批
        return True

    def print(self, message: str, color: str = "blue", bold: bool = False):
        logger.info(f"[headless] {message}")

    def set_current_tokens(self, value: int):
        self._current_tokens = value

    def set_max_context_tokens(self, max_tokens: int):
        self._max_tokens = max_tokens

    # ===== runtime/cli_console 可能调的方法（stub）=====
    def start_interactive_mode(self):
        pass

    def show_status_bar(self, **kwargs):
        pass

    def show_interactive_banner(self):
        pass

    def prompt_prefix(self, session_id: str):
        return ""

    async def handle_events(self, event):
        # API 模式事件由 app.py 自己消费，这里不处理
        pass
