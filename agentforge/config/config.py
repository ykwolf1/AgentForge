from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    model_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    class ConfigDict:
        extra = "allow"

class AgentConfig(BaseModel):
    agent_name: str
    provider: Literal["unified", "openai", "anthropic", "compatible", None] = None  # 统一用 unified；旧值兼容
    wire_api : Literal["chat", "responses", "messages", None] = None  # chat=OpenAI兼容 | responses=OpenAI o系列 | messages=Anthropic兼容
    model: ModelConfig
    # 多 agent / 角色化字段（全部可选，不写 = 普通单 agent）
    role: str = "worker"                          # coordinator | worker
    system_prompt: Optional[str] = None           # 角色 prompt；不写用默认
    allowed_tools: Optional[List[str]] = None     # 工具白名单；不写 = 全部工具
    peers: List[str] = []                         # 能 delegate 给哪些 agent（按 name）
    # 验证器 + Reflection（闭环后半圈）
    verify_after: Optional[Dict[str, str]] = None # {"edit": "python -m py_compile {file_path}"}
    enable_reflection: bool = False                # 是否启用 LLM 质量评估
    max_reflections: int = 2                       # 最大反思次数（防无限循环）
    # Budget Manager（预算治理）
    budget: Optional[Dict[str, Any]] = None        # {max_tokens, max_tool_calls, max_cost}
    # DLP 敏感信息检测
    enable_dlp: bool = True                        # 默认开（工具输出自动 mask）
    class ConfigDict:
        extra = "allow"

class MCPServerConfig(BaseModel):
    name: str
    type: str = "stdio"
    command: str
    args: List[str] = Field(default_factory=list)
    enabled: bool = True
    include: List[str] = Field(default_factory=list)
    save_images_dir: Optional[str] = None
    isolated: bool = False
    class ConfigDict:
        extra = "allow"

class MCPConfig(BaseModel):
    enabled: bool = True
    isolated: bool = False
    servers: List[MCPServerConfig] = Field(default_factory=list)
    class ConfigDict:
        extra = "allow"

    @model_validator(mode="before")
    @classmethod
    def _convert_mcp_servers(cls, data):
        """兼容 Claude Desktop 风格的 mcpServers:{} 配置。
        把 {mcpServers: {name: {command, args, type}}} 转成 servers: [...] 列表。"""
        if not isinstance(data, dict):
            return data
        raw = data.get("mcpServers")
        if not isinstance(raw, dict) or not raw:
            return data
        servers = list(data.get("servers") or [])
        for name, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            # stdio: {command, args}；sse/http: {type, url}（url 存进 command 字段）
            srv_type = cfg.get("type", "stdio")
            command = cfg.get("command") or cfg.get("url") or ""
            args = cfg.get("args") or []
            servers.append({
                "name": name,
                "type": srv_type,
                "command": command,
                "args": args,
                "enabled": cfg.get("enabled", True),
            })
        data["servers"] = servers
        data.pop("mcpServers", None)
        return data

class MemoryMonitorConfig(BaseModel):
    check_interval: int = 5       # 每隔几轮检查一次（自适应：越接近上限查越频）
    maximum_capacity: int = 4096
    rules: List[Tuple[float, int]] = Field(default_factory=list)
    model: Optional[str] = None
    # 上下文管理阈值（修复⑨：从硬编码改为可配置）
    offload_threshold: int = 1500       # 工具结果超过这个字符数就卸载
    l1_extract_interval: int = 5        # 每隔几轮自动提取 L1 原子事实
    checkpoint_interval: int = 3        # 每隔几轮存一次 checkpoint
    context_store_max_entries: int = 500  # ToolResultStore 磁盘文件上限
    class ConfigDict:
        extra = "allow"

class AppConfig(BaseModel):
    default_agent: Optional[str] = None
    agents: List[AgentConfig]
    permission_level: str = "locked"
    max_turns: int = 20
    enable_logging: bool = True
    log_level: str = "INFO"

    mcp: Optional[MCPConfig] = None
    memory_monitor: Optional[MemoryMonitorConfig] = None
    infra: Optional[Dict[str, Any]] = None     # 基础设施（sandbox/redis/chromadb）

    runtime: Dict[str, Any] = Field(default_factory=dict)

    class ConfigDict:
        extra = "allow"
