# AgentForge — 生产级多 Agent 系统

> **121 个 Python 文件 / 18301 行代码 / 22 个测试文件 / 158 个测试用例全部通过 / 52 个审计问题全部修复**

AgentForge 是一个面向工程应用场景的通用 AI Agent 框架，涵盖多智能体协作、任务规划、工具调用、知识库（RAG）、记忆管理等核心能力。它既是一个可用的编码助手，也是一个可扩展的 Agent 运行时平台。

## ✨ 核心能力

### Agent 内核
- **通用 Agent 类**：统一的 while 循环（LLM → 工具 → 回灌 → 再推理），失败回灌不崩溃，max_turns 兜底
- **统一 LLM 适配**：UnifiedAdapter 一套代码对接所有模型（chat/responses/messages 三协议），DeepSeek/Qwen/Claude/GLM 统一接入
- **角色化**：coordinator（接输入/分发）+ worker（执行），靠配置的 role/system_prompt/allowed_tools 区分
- **并行工具执行**：LLM 一次发多个 tool_call 时 asyncio.gather 并发执行

### 工具系统（21 个内置工具 + MCP）
- **四道关卡**：PreToolUse hook → 用户审批 → 执行 → PostToolUse hook（每步可控可审计）
- **内置工具**（21 个，按功能分组）：见下方项目结构
- **MCP 集成**：支持 stdio / http / SSE 三种传输，兼容 Claude Desktop 的 mcpServers:{} 配置
- **插件化**：autodiscover 扫描 + @register_tool 装饰器，建文件即生效

### 多 Agent 协作
- **delegate**（预定义委派）：配置驱动团队（planner/coder/reviewer），coordinator 委派给 worker
- **spawn_agent**（动态生成）：LLM 自己决定要什么子 agent，指定 task + tools + instructions，异步执行
- **并行 delegate**：多个 delegate 后台 asyncio.create_task 并发跑，不阻塞主循环
- **共享状态**：所有 agent 持有同一 SharedState（history / peers / budget / token_usage）
- **handoff 深度限制**：上限 3 层（可配置），防 A→B→A→B 无限循环

### 记忆系统（参照腾讯 TencentDB Agent Memory 设计）

记忆系统解决两个问题：**同一次对话中上下文越来越长怎么办**（会话级），**新开对话怎么记住上次的事**（跨会话级）。

#### 会话级记忆（一次对话的生命周期）

解决"当前对话进行中，上下文怎么管理"：

| 组件 | 实现 | 解决什么问题 |
|:--|:--|:--|
| 工作记忆 | Redis | 当前对话的 conversation_history 放哪 |
| 工具结果卸载 | `context/` 包 | web_search 返回 7800 字、write_file 写 54KB——这些超长结果自动卸载到磁盘，上下文只放符号化摘要（压缩率 93%）|
| 上下文压缩 | memory_monitor | 上下文超 75% 窗口时，用 LLM 摘要压缩（所有运行模式都走）|
| checkpoint | SQLite | 每 3 轮存快照，崩了恢复到任意轮 |
| 会话存盘 | SQLite | 对话结束时归档，可被 FTS5 检索 |

#### 跨会话记忆（L0~L3 金字塔，解决"重启不失忆"）

| 层 | 名称 | 存储 | 什么时候用 |
|:--|:--|:--|:--|
| L3 核心画像 | SQLite | "用户是后端架构师，技术栈 Python" | 新对话开头全量载入 |
| L2 场景记忆 | SQLite + FTS5 | "AgentForge 知识库开发"场景摘要 | 新对话开头按当前任务匹配 |
| L1 原子事实 | SQLite + FTS5 | "用户偏好 DeepSeek""知识库用 Milvus" | 关键词检索；运行中每 5 轮自动提取 |
| L0 原始会话 | 磁盘 + SQLite | 完整的工具调用/对话记录 | 按需取回（证据溯源）|

**工具**：`remember_fact`（存事实）/ `recall_memory`（检索记忆）/ `save_scenario`（建场景）/ `recall_tool_result`（调取卸载的工具结果）

### 知识库（生产级 RAG）
- **向量库**：Milvus 2.6（dense + sparse 双向量）
- **混合检索**：RRF（Reciprocal Rank Fusion）融合 dense + sparse 结果
- **Embedding**：bge-m3 本地模型（独立 HTTP 服务，不在 Agent 进程加载）
- **Reranker**：bge-reranker-v2-m3 精排
- **文档格式**：Markdown / PDF / DOCX / HTML
- **切片策略**：固定切分 / 章节切分 / 递归切分 / 自定义分隔符
- **图片 URL**：不存图片本身，URL 放 metadata，召回时 LLM 看到 URL 引用
- **元数据过滤**：source / tags / doc_id / section
- **双 ID**：chunk_id（单切片查询）+ doc_id（整篇文档查询）

### 韧性层
| 能力 | LLM | MCP | 子 Agent |
|:--|:--:|:--:|:--:|
| 超时 | ✅ 60s | ✅ 30s | ✅ 120s |
| 重试 | ✅ max_retries=3 | ✅ | ✅ max_retries=2 |
| 熔断 | ✅ CircuitBreaker | ✅ 每 server 独立 | ✅ |

### 安全治理
- **DLP 敏感信息检测**：API_KEY / EMAIL / PHONE / AWS_KEY / PRIVATE_KEY / ID_CARD 自动 mask
- **Budget Manager**：max_tokens / max_tool_calls / max_cost 预算控制，超限强制停止
- **权限分级**：LOCKED / EDIT_ONLY / PLANNING / YOLO
- **危险命令拦截**：rm -rf / format / mkfs / dd 永不自动批
- **验证器**：工具执行后自动跑 py_compile / pytest
- **Reflection**：LLM 质量打分 0-100，不达标带反馈重试

### 服务化
- **FastAPI**：11 个端点（同步执行 + 异步任务 + 鉴权 + Registry + Infra）
- **异步任务系统**：submit → task_id → GET 查进度 → POST cancel
- **并发隔离**：每请求/每任务独立 AgentManager + ToolManager
- **API 鉴权**：Bearer token 中间件（环境变量控制）

### 基础设施
| 服务 | 用途 | 降级 |
|:--|:--|:--|
| Milvus | 向量知识库 | FTS5 关键词检索 |
| MinIO | 文档对象存储 | 本地文件系统 |
| Redis | 工作记忆 + 任务队列 | 内存模式 |
| Sandbox | Docker 代码执行隔离 | 本地 bash |
| Embedding Server | bge-m3 独立服务 | 进程内加载 |

## 🚀 快速开始

### 安装

```bash
git clone <repo-url>
cd AgentForge
uv sync --all-extras
```

### 配置

```bash
# 复制示例配置
cp agentforge/config/agentforge_config.example.yaml ~/.agentforge/agentforge_config.yaml

# 编辑配置（填入 API key）
vim ~/.agentforge/agentforge_config.yaml
```

### CLI 模式

```bash
# 交互模式
python -m agentforge.main

# 单次执行
python -m agentforge.main -p "用 read_file 读取 pyproject.toml 前 3 行"

# YOLO 模式（自动批准所有工具调用）
python -m agentforge.main --permission-mode yolo -p "你的任务"
```

### API 服务模式

```bash
# 启动服务
python -m agentforge.main --serve --port 8000

# 提交异步任务
curl -X POST http://localhost:8000/agent/tasks \
  -H "Content-Type: application/json" \
  -d '{"prompt": "分析 auth.py 的代码结构"}'

# 查询进度
curl http://localhost:8000/agent/tasks/{task_id}
```

### 知识库

```bash
# 启动 Embedding 服务
python -m agentforge.knowledge.embedding_server \
  --model /path/to/bge-m3 \
  --port 8002
```

```yaml
# 配置（~/.agentforge/agentforge_config.yaml）
infra:
  milvus:
    enabled: true
    host: "localhost"
    port: 19530
    collection: "agentforge_knowledge"
  embedding:
    server_url: "http://localhost:8002"
    model_path: "/path/to/bge-m3"
  minio:
    enabled: true
    endpoint: "localhost:9002"
    access_key: "admin"
    secret_key: "admin123"
```

## 📐 架构

```
   ┌──────────────────────────────────────────────────────────┐
   │                    FastAPI 服务层                         │
   │   同步 API + 异步任务系统（并发/重试/超时/取消/查询）      │
   └───────────────────────────┬──────────────────────────────┘
                               │
   ┌───────────────────────────▼──────────────────────────────┐
   │                    Agent 内核                             │
   │   循环（LLM→工具→history→LLM）+ 失败回灌 + 验证 + 反思    │
   │   + delegate（预定义委派）+ spawn（动态生成子 agent）     │
   │   + 并行工具 + 并行 delegate                              │
   └──────────┬──────────────┬──────────────┬─────────────────┘
              │              │              │
   ┌──────────▼────┐ ┌──────▼──────┐ ┌────▼──────────────┐
   │  工具系统      │ │  记忆系统    │ │  韧性层            │
   │  四道关卡      │ │ 分层记忆      │ │  超时/重试/熔断    │
   │  21 个工具     │ │ L0~L3+会话级 │ │  CircuitBreaker   │
   │  MCP 动态     │ │  +checkpoint │ │                   │
   └──────────┬────┘ └──────┬──────┘ └───────────────────┘
              │              │
   ┌──────────▼──────────────▼──────────────────────────────┐
   │              知识库 + 安全 + 基础设施                   │
   │  Milvus(RAG) + DLP + Budget + InfraManager             │
   └─────────────────────────────────────────────────────────┘
```

## 📁 项目结构

```
agentforge/
├── agents/          # Agent 内核 + 多 agent 管理
│   ├── agent.py          # 通用 Agent（主循环 + 工具 + handoff）
│   ├── agent_manager.py  # Agent 工厂 + 团队初始化
│   ├── shared_state.py   # 共享状态（history/peers/budget）
│   ├── registry.py       # Agent 注册中心
│   └── verifier.py       # 验证器 + Reflection
├── tools/           # 工具系统（按功能分组）
│   ├── base_tool.py        # 基类（通用 build() + 风险等级）
│   ├── tool_manager.py     # 注册 + 四道关卡 + autodiscover
│   ├── mcp_tool.py         # MCP 远程工具适配
│   ├── dlp.py              # DLP 安全组件（非工具）
│   ├── filesystem/         # 文件操作：read_file / write_file / edit / glob / ls / grep
│   ├── execution/          # 代码执行：bash
│   ├── search/             # 联网搜索：web_search / web_fetch
│   ├── memory/             # 记忆工具：recall / remember_fact / recall_memory / save_scenario / recall_tool_result
│   ├── knowledge/          # 知识库：ingest_document / knowledge_search
│   ├── collaboration/      # 多 agent 协作：delegate / spawn_agent / check_sub_agent
│   └── misc/               # 其他：todo_write / read_many_files
├── knowledge/       # 知识库（Milvus + embedding + 切片）
├── memory/          # 记忆系统（会话级 5 组件 + L0~L3 长期分层记忆）
├── context/         # 上下文管理（工具结果卸载 + 符号化回灌）
├── infra/           # 基础设施（Milvus/MinIO/Redis/Sandbox/Embedding）
├── server/          # FastAPI + 异步任务系统
├── llm/            # LLM 客户端 + adapter
├── hooks/          # Hook 系统（PreToolUse/PostToolUse/...）
├── skills/         # Skill 发现 + 注入
├── config/         # YAML 配置
├── cli/            # 交互终端
├── evaluation/     # 离线评估框架
└── utils/          # 韧性/权限/轨迹/日志
```

## 📊 能力清单

| 维度 | 能力 |
|:--|:--|
| Agent 循环 | while 循环 + 失败回灌 + 并行工具 + max_turns |
| 后端 | OpenAI chat / Anthropic（adapter 切换） |
| 多 Agent | delegate + spawn_agent + 并行 delegate + 共享状态 |
| 工具 | 21 内置 + MCP(stdio/http/sse) + autodiscover |
| 安全 | 四道关卡 + DLP + Budget + 验证器 + Reflection |
| 记忆 | Redis + SQLite + checkpoint + FTS5 + Milvus |
| 知识库 | Milvus(dense+sparse/RRF) + bge-m3 + 4 种文档 + 4 种切片 |
| 服务化 | FastAPI 11 端点 + 异步任务 + 鉴权 |
| 基础设施 | Milvus + MinIO + Redis + Sandbox + Embedding Server |
| 可观测性 | loguru + trajectory + session_stats |
| 评估 | EvaluationRunner + metrics + 数据集 |

## 📄 License

MIT
