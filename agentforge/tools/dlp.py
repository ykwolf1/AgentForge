# dlp.py —— 安全组件（不是工具，不会被 @register_tool 注册）
#
#   本文件是 DLP（Data Loss Prevention）检测器，在工具结果回灌 history 前扫描，
#   检测到 api_key/私钥/邮箱/手机号等自动替换，防止 agent 把敏感信息写进上下文。
#   它被 agent.py 的 _process_single_result 调用，不是独立的 Agent 工具。
#   放在 tools/ 目录是因为它服务于工具层；autodiscover 不会注册它（无 @register_tool）。
import re
from typing import List, Tuple

try:
    from loguru import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


class DLPDetector:
    """Data Loss Prevention：检测工具输出中的敏感信息，自动 mask。"""

    PATTERNS = {
        "API_KEY": r'(?:sk-|ms-|tvly-|ghp_|gho_|github_pat_)[a-zA-Z0-9]{20,}',
        "AWS_KEY": r'AKIA[0-9A-Z]{16}',
        "PRIVATE_KEY": r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END',
        "EMAIL": r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        "PHONE_CN": r'\b1[3-9]\d{9}\b',
        "ID_CARD_CN": r'\b\d{17}[\dXx]\b',
    }

    def scan(self, text: str) -> Tuple[str, List[dict]]:
        """扫描文本，返回 (masked_text, findings)。

        Args:
            text: 工具输出文本
        Returns:
            masked_text: 敏感信息被替换后的文本
            findings: [{type, preview}, ...] 检测到的敏感信息列表
        """
        if not text or not isinstance(text, str):
            return text, []

        masked = text
        findings = []

        for name, pattern in self.PATTERNS.items():
            try:
                matches = list(re.finditer(pattern, masked))
                if matches:
                    for m in matches:
                        preview = m.group()[:8] + "****" if len(m.group()) > 8 else m.group()[:3] + "****"
                        findings.append({"type": name, "preview": preview})
                    masked = re.sub(pattern, f"[{name}_MASKED]", masked)
            except re.error:
                continue  # 正则编译失败跳过

        if findings:
            logger.warning(f"[DLP] 检测到 {len(findings)} 处敏感信息: {[f['type'] for f in findings]}")

        return masked, findings
