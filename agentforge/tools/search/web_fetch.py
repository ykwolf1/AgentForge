import asyncio
import os
from typing import Any, Mapping

import aiohttp

from agentforge.tools.tool_manager import register_tool

from agentforge.tools.base_tool import BaseTool, ToolCallResult

CLAUDE_DESCRIPTION = """
- Fetches content from a specified URL and returns clean markdown
- Uses Tavily Extract API to convert web pages into readable markdown
- Handles JavaScript-rendered pages and anti-bot protection better than raw HTTP
- Use this tool when you need to retrieve and analyze web content
"""

@register_tool(name="web_fetch", providers="*")
class WebFetchTool(BaseTool):
    name="web_fetch"
    display_name="Fetch Web Content"
    description="Fetch content from web URLs using Tavily Extract and return as markdown"
    parameter_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to fetch content from"
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds (default: 30)",
                "default": 30
            }
        },
        "required": ["url"]
    }

    base_url = "https://api.tavily.com/extract"

    def _get_api_key(self) -> str:
        """从环境变量中获取 Tavily API key"""
        return os.getenv("TAVILY_API_KEY", "")

    async def execute(self, **kwargs) -> ToolCallResult:
        """Fetch web content via Tavily Extract and return markdown."""
        url = kwargs.get("url")
        timeout = kwargs.get("timeout", 30)

        if not url:
            return ToolCallResult(call_id="", error="No URL provided")

        api_key = self._get_api_key()
        if not api_key:
            return ToolCallResult(
                call_id="",
                error="Tavily API key is required. Set TAVILY_API_KEY environment variable."
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "urls": url,
            "extract_depth": "basic",
            "format": "markdown",
            "include_images": False,
            "include_favicon": False,
        }

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self.base_url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response,
            ):
                if response.status != 200:
                    error_text = await response.text()
                    return ToolCallResult(
                        call_id="",
                        error=f"Tavily Extract API error {response.status}: {error_text}"
                    )

                data = await response.json()

            results = data.get("results", [])
            failed = data.get("failed_results", [])

            if results:
                item = results[0]
                title = item.get("title", "")
                content = item.get("raw_content", "") or item.get("content", "") or ""
                header = f"Content from {url}"
                if title:
                    header += f" — {title}"
                return ToolCallResult(
                    call_id="",
                    result=f"{header}:\n\n{content}"
                )

            # 抽取失败时，Tavily 会在 failed_results 里给出原因
            if failed:
                reason = failed[0] if isinstance(failed, list) else failed
                return ToolCallResult(
                    call_id="",
                    error=f"Failed to extract content from {url}: {reason}"
                )

            return ToolCallResult(
                call_id="",
                error=f"No content returned for {url}"
            )

        except asyncio.TimeoutError:
            return ToolCallResult(call_id="", error=f"Timeout fetching {url}")
        except Exception as e:
            return ToolCallResult(call_id="", error=f"Error fetching {url}: {str(e)}")
