import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import aiohttp

from agentforge.tools.tool_manager import register_tool

from agentforge.tools.base_tool import BaseTool, ToolCallResult

CLAUDE_DESCRIPTION = """
- Allows Claude to search the web and use the results to inform responses
- Provides up-to-date information for current events and recent data
- Returns search result information formatted as search result blocks
- Use this tool for accessing information beyond Claude's knowledge cutoff
- Searches are performed automatically within a single API call
"""

@dataclass
class SearchResult:
    """Search result item."""
    title: str
    link: str
    snippet: str
    position: int = 0

@register_tool(name="web_search", providers="*")
class WebSearchTool(BaseTool):
    name="web_search"
    display_name="Web Search"
    description="Performs a web search using Tavily API and returns the results. This tool is useful for finding current information on the internet."
    parameter_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to find information on the web."
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of search results to return (default: 10, max: 20)",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 20
                    }
                },
                "required": ["query"]
            }
    api_key = ""
    base_url = "https://api.tavily.com/search"

    def _get_api_key(self) -> str:
        """从环境变量中获取 Tavily API key"""
        api_key = os.getenv("TAVILY_API_KEY")
        if api_key:
            return api_key

        # 兼容旧配置：若用户仍持有 Serper key，给出明确指引
        return ""

    def validate_params(self, **kwargs) -> Optional[str]:
        """Validate the parameters for the WebSearchTool."""
        query = kwargs.get("query")
        num_results = kwargs.get("num_results", 10)

        if not query:
            return "The 'query' parameter is required."

        if not isinstance(query, str):
            return "The 'query' parameter must be a string."

        if not query.strip():
            return "The 'query' parameter cannot be empty."

        if not isinstance(num_results, int) or num_results < 1 or num_results > 20:
            return "The 'num_results' parameter must be an integer between 1 and 20."

        if not self._get_api_key():
            return "Tavily API key is required. Set TAVILY_API_KEY environment variable."

        return None
    
    def get_description(self, **kwargs) -> str:
        """Get description of the search operation."""
        query = kwargs.get("query", "")
        num_results = kwargs.get("num_results", 10)
        return f'Searching the web for: "{query}" (returning {num_results} results)'
    
    async def execute(self, **kwargs) -> ToolCallResult:
        """Perform web search using Tavily API."""
        # Validate parameters
        validation_error = self.validate_params(**kwargs)
        if validation_error:
            return ToolCallResult(
                call_id=kwargs.get("call_id", ""),
                error=f"Invalid parameters provided. Reason: {validation_error}"
            )

        query = kwargs["query"]
        num_results = kwargs.get("num_results", 10)

        try:
            headers = {
                "Authorization": f"Bearer {self._get_api_key()}",
                "Content-Type": "application/json"
            }

            payload = {
                "query": query,
                "max_results": num_results,
                "search_depth": "basic",
                "include_answer": True,
                "topic": "general",
            }

            async with aiohttp.ClientSession() as session, session.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:

                if response.status != 200:
                    error_text = await response.text()
                    return ToolCallResult(
                        call_id=kwargs.get("call_id", ""),
                        error=f"Tavily API error {response.status}: {error_text}"
                    )

                data = await response.json()

            search_results = self._parse_search_results(data)

            if not search_results:
                return ToolCallResult(
                    call_id=kwargs.get("call_id", ""),
                    result=f'No search results found for query: "{query}"'
                )

            formatted_results = self._format_search_results(query, search_results, data.get("answer"))

            return ToolCallResult(
                call_id=kwargs.get("call_id", ""),
                result=formatted_results,
                metadata={
                    "query": query,
                    "num_results": len(search_results),
                    "results": [
                        {
                            "title": result.title,
                            "url": result.link,
                            "snippet": result.snippet,
                            "position": result.position
                        }
                        for result in search_results
                    ]
                }
            )

        except asyncio.TimeoutError:
            return ToolCallResult(
                call_id=kwargs.get("call_id", ""),
                error=f"Search request timed out for query: {query}"
            )
        except Exception as e:
            error_message = f'Error during web search for query "{query}": {str(e)}'
            try:
                from loguru import logger
                logger.error(error_message)
            except Exception:
                print(f"❌ {error_message}")
            return ToolCallResult(
                call_id=kwargs.get("call_id", ""),
                error=error_message
            )

    def _parse_search_results(self, data: Dict[str, Any]) -> List[SearchResult]:
        """Parse Tavily API response into SearchResult objects."""
        results = []

        # Tavily 返回顶层 answer 字段，作为直接答案放在最前
        answer = data.get("answer")
        if answer:
            results.append(SearchResult(
                title="Answer (Tavily)",
                link="",
                snippet=str(answer),
                position=0,
            ))

        # Tavily 的 results 数组即常规搜索结果
        for i, result in enumerate(data.get("results", [])):
            results.append(SearchResult(
                title=result.get("title", "No Title"),
                link=result.get("url", ""),
                snippet=result.get("content", "No description available"),
                position=i + 1,
            ))

        return results
    
    def _format_search_results(self, query: str, results: List[SearchResult], answer: Optional[str] = None) -> str:
        """Format search results into a readable string."""
        formatted = f'Web search results for "{query}":\n\n'
        for result in results:
            if result.position == 0:
                # Tavily 直接答案
                formatted += f"[Answer] {result.title}\n"
                formatted += f"📝 {result.snippet}\n\n"
                continue
            formatted += f"[{result.position}] {result.title}\n"
            formatted += f"🔗 {result.link}\n"
            formatted += f"📝 {result.snippet}\n\n"
        formatted += f"Found {len(results)} results for your search query."

        return formatted
