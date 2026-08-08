# AgentForge 全局复盘 —— 设计 · 功能 · 工程 · 优化

> **121 个 Python 文件 / 18301 行代码 / 22 个测试文件 / 158 个测试用例 / 21 个工具**
>
> 本文档梳理 AgentForge 从 pywen 单 agent CLI 到通用多 agent 系统的完整演进逻辑。
> 每个设计决策都回答："为什么要做、怎么做的、怎么验证的、提升了什么"。
>
> **核心主线：做正确的事 → 用最小改动解决最大痛点 → 确定性测试验证。**

---

## 目录

- [一、Agent 内核设计](#一agent-内核设计为什么-agent-是这样长的)
- [二、主循环设计](#二主循环设计agent-怎么跑)
- [三、工具系统设计](#三工具系统设计agent-的手)
- [四、多 Agent 协作设计](#四多-agent-协作设计从一个人到一个团队)
- [五、记忆系统设计](#五记忆系统设计会话内管理--跨会话记忆)
- [六、韧性设计](#六韧性设计外部调用怎么不崩)
- [七、安全治理设计](#七安全治理设计怎么管住-agent)
- [八、服务化设计](#八服务化设计从-cli-到-api-平台)
- [九、闭环设计](#九闭环设计验证纠正迭代的完整闭环)
- [十、基础设施层设计](#十基础设施层设计统一管理外部依赖)
- [设计理念贯穿线](#设计理念贯穿线)
- [各模块实现位置索引](#各模块实现位置索引)

---

## 一、Agent 内核设计（"为什么 Agent 是这样长的"）

### 设计哲学

> **Agent = LLM + 循环 + 工具。三者缺一不叫 agent。**

这个定义决定了 AgentForge 的核心架构：一个通用 Agent 类包含这三者，其余（记忆/协作/安全/服务化）都是在它之上叠加。

### 模块依赖关系图

```
                    用户输入
                       │
                       ▼
              ┌─────────────────┐
              │  AgentManager   │  ← 管运行（init/switch/close）
              │  （工厂+调度）   │
              └────────┬────────┘
                       │ new Agent(config)
                       ▼
    ┌──────────────────────────────────────┐
    │              Agent 内核               │
    │  ┌─────────┐ ┌──────┐ ┌───────────┐  │
    │  │LLMClient│ │ 工具 │ │SharedState│  │
    │  │+Adapter │ │注册表│ │(对话历史)  │  │
    │  └────┬────┘ └──┬───┘ └─────┬─────┘  │
    │       │         │           │         │
    │       ▼         ▼           ▼         │
    │   while 循环 ← 主循环设计（第二章）     │
    └──────────────────┬───────────────────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     ┌─────────┐ ┌──────────┐ ┌──────────┐
     │ 记忆系统 │ │ 多Agent  │ │ 安全治理  │
     │ (第五章) │ │ (第四章)  │ │ (第七章)  │
     └─────────┘ └──────────┘ └──────────┘
```

**一句话说清楚**：AgentManager 建一个 Agent 实例 → Agent 拿着配置（用什么模型/什么工具/什么权限）跑 while 循环 → 循环里调 LLM + 调工具 → 结果存记忆、受安全管控 → 循环直到任务完成。

### 结构说明

Agent 内核只做三件事：
1. **持有配置**：AgentConfig（模型/工具白名单/prompt/权限/预算）—— 决定"这个 agent 是谁"
2. **跑循环**：`while current_turn < max_turns` —— 决定"怎么跑"（详见第二章）
3. **持有状态**：SharedState（对话历史/token 用量/budget/后台任务）—— 决定"跑到哪了"

其余所有能力（记忆/协作/安全）都是"插件"——通过工具注册、配置开关、hook 注入进来，Agent 内核不直接依赖它们。

### 实例：短程任务——读文件

```
用户: "读一下 pyproject.toml"
  │
  ├─ AgentManager.init("pywen") → new Agent(config)
  ├─ Agent.run("读一下 pyproject.toml")
  │    ├─ 轮1: LLM → "调 read_file(path=pyproject.toml)"
  │    │         → 工具执行 → 文件内容回灌
  │    ├─ 轮2: LLM → "[stop] 文件内容如下..."
  │    └─ 循环结束（finish_reason=stop）
  └─ AgentManager.close() → 存盘 + 清理
```

整个过程 2 轮，不到 3 秒。Agent 内核的正确性体现在：LLM 说调工具 → 工具结果回灌给 LLM → LLM 决定结束。

### 实例：长程任务——行研报告

```
用户: "用 research-report skill 写行研报告"
  │
  ├─ Agent.run(任务)
  │    ├─ 轮1:  read_file(SKILL.md) → 读 skill 指令
  │    ├─ 轮2:  knowledge_search(知识库) → RAG 检索（超长结果自动卸载）
  │    ├─ 轮3:  web_search(联网搜索) → 实时数据
  │    ├─ 轮4:  bash(画图) → matplotlib 生成图表
  │    ├─ ...（压缩触发，卸载结果保留 ref_id）
  │    ├─ 轮40: write_file(报告) → 15KB 报告写入磁盘
  │    └─ 循环结束（finish_reason=stop 或 max_turns）
  └─ AgentManager.close() → 存盘 + L2 场景沉淀 + GC
```

40 轮、74 次工具调用——Agent 内核的韧性在这里体现：LLM 断流自动重连、工具失败回灌自纠正、上下文爆炸自动压缩卸载、后台 peer 并发跑。

---

### 1.1 消除三后端重复 —— 统一内核

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | pywen 有 3 个 agent 类（PywenAgent/ClaudeAgent/CodexAgent），主循环 90% 代码重复，改一处要改三处 |
| **解决什么** | 消除重复代码，让所有改进只做一遍 |
| **功能（怎么做）** | 提取公共逻辑到一个通用 `Agent` 类（`agents/agent.py`），后端差异由 `UnifiedAdapter` 消化（chat/responses/messages 三协议，不再分 openai/anthropic） |
| **怎么迭代的** | 先建通用 Agent → 切 `agent_manager` 工厂统一 `new Agent` → 删 pywen/claude/codex 三个目录 → 合并两套 LLM adapter 为 UnifiedAdapter |
| **如何验证** | 删 3 个目录后全项目 `import` 通过 + CLI 冒烟跑通 + 真实工具调用验证 |
| **提升什么** | 代码量减半；一处改全部生效；**这是所有后续改进的基础**——如果内核不统一，后面每个功能都要做三遍 |

**关键代码位置**：
- 通用 Agent：`agentforge/agents/agent.py`
- 统一适配：`agentforge/llm/llm_client.py` + `adapters/unified_adapter.py`（chat/responses/messages 三协议）
- 工厂方法：`agentforge/agents/agent_manager.py` → `_create_agent()`

---

### 1.2 per-agent 配置 —— 角色化的基础

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 所有 agent 共用全局配置（model/api_key/base_url） |
| **解决什么** | 多 agent 场景下每个 agent 需要自己的模型、工具集、prompt、权限 |
| **功能（怎么做）** | Agent 构造时接收 `agent_config: AgentConfig`，`LLMClient(agent_config)` 用传入配置而非全局 |
| **怎么迭代的** | `__init__` 加参数 → `agent_manager` 传配置 → `spawn_agent` 动态构造配置（copy + override） |
| **如何验证** | 多 agent 配置里每个 agent 用不同 model/tools 跑通；spawn 时传入自定义 instructions + tools |
| **提升什么** | 角色化的基础：不同 agent 可以有不同模型/工具/prompt/权限/预算 |

**关键代码位置**：
- AgentConfig：`agentforge/config/config.py` → `AgentConfig`
- 配置注入：`agentforge/agents/agent.py` → `__init__(agent_config)`
- 动态构造：`agentforge/tools/collaboration/spawn.py` → `SpawnAgentTool.execute()`

---

### 1.3 Agent 与 AgentManager 的职责分离

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | Agent 的运行逻辑和能力定义混在一起 |
| **解决什么** | 单一职责：Agent 定义能力，AgentManager 管运行 |
| **功能（怎么做）** | Agent 持有 config/tools/llm_client（能力）；AgentManager 管 init/switch/close（运行），只转发调用 |
| **怎么迭代的** | 设计上分离 → AgentManager.agent_run 只是 `for event in agent.run(): yield` → init/init_team 建实例 |
| **如何验证** | AgentManager.agent_run 一行转发，不包含任何 agent 逻辑 |
| **提升什么** | 解耦：未来换运行管理（如分布式 Worker）不需要动 Agent 内部 |

---

## 二、主循环设计（"agent 怎么跑"）

### 主循环数据流程图

这是整个系统的心脏——agent 每一轮的完整数据流：

```
用户输入 "帮我查下项目结构"
       │
       ▼
┌─── while turn < max_turns ────────────────────────────────────┐
│                                                                │
│  ① 组装上下文                                                  │
│     conversation_history（对话历史）                            │
│     + tools schema（工具定义，第一次缓存后续复用）               │
│     + system_prompt + skills_prompt + 长期记忆注入               │
│              │                                                 │
│              ▼                                                 │
│  ② 调 LLM（UnifiedAdapter）                                     │
│     发消息 → 流式接收                                           │
│     事件分流：                                                   │
│     ├─ ASSISTANT_DELTA → 文本片段（实时输出给用户）              │
│     ├─ TOOL_CALL_READY → "要调工具了"                           │
│     ├─ TOKEN_USAGE → token 统计（写入 session_stats）           │
│     ├─ ERROR → 错误处理（回滚半残消息 + 轮次推进）               │
│     └─ RESPONSE_FINISHED → 本轮结束                             │
│              │                                                 │
│              ▼                                                 │
│  ③ 如果要调工具：                                               │
│     重复调用检测 → asyncio.gather 并发执行                      │
│     ├─ PreToolUse hook（组织策略）                              │
│     ├─ 用户审批（权限等级决定是否问用户）                        │
│     ├─ tool.execute()（真正执行）                               │
│     └─ PostToolUse hook（结果检查）                             │
│              │                                                 │
│              ▼                                                 │
│  ④ 结果回灌                                                    │
│     ├─ 成功：工具结果 → 超长卸载（ContextManager）→ 符号进历史   │
│     ├─ 失败：错误信息 → 回灌为 tool 消息（LLM 下轮自纠正）       │
│     ├─ handoff：检测到委派信号 → 后台起 peer agent               │
│     └─ DLP：敏感信息扫描 → mask 后回灌                          │
│              │                                                 │
│              ▼                                                 │
│  ⑤ 轮末收尾                                                    │
│     ├─ checkpoint（每 3 轮存快照）                              │
│     ├─ 上下文压缩检查（>75% 触发，有 peer 跑时跳过）             │
│     ├─ L1 事实提取（每 5 轮自动沉淀长期记忆）                    │
│     └─ budget 检查（token/工具次数/费用超限则终止）              │
│              │                                                 │
│              ▼                                                 │
│     finish_reason == "stop"？                                   │
│     ├─ 是 → 任务完成，跳出循环                                  │
│     └─ 否 → 回到 ①（带着工具结果进入下一轮）                     │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### 结构说明

主循环解决的核心问题：**LLM 是"一问一答"的，不能自己推进任务。循环让 LLM 根据上一步的结果自己决定下一步——这就是 agent 和 chatbot 的根本区别。**

循环里的每个环节都和生产级改造相关：
- **② 调 LLM**：LLM 报错不处理 → 死循环（🔴1 修复）；异常击穿 → 会话死掉（🔴3 修复）
- **③ 执行工具**：四道关卡（详见第三章）；并发执行（asyncio.gather）
- **④ 结果回灌**：超长结果卸载（上下文管理）；失败回灌（"撞墙会回头"）
- **⑤ 轮末收尾**：压缩保护 ref_id（🔴3 记忆修复）；checkpoint 时机（🟡13 修复）

### 实例：短程任务——读文件（2 轮完成）

```
轮1:
  ① 上下文: system_prompt + "读一下 pyproject.toml"
  ② LLM → "调 read_file(path=pyproject.toml)"
  ③ 工具执行 → 返回文件内容
  ④ 回灌: 文件内容进 conversation_history
  ⑤ 轮末: turn=1, 不用压缩

轮2:
  ① 上下文: 历史里有文件内容了
  ② LLM → "[stop] 这是 pyproject.toml 的内容..."
  ④ 无工具调用
  ⑤ 轮末: finish_reason=stop → 循环结束
```

### 实例：长程任务——行研报告（40 轮）

```
轮1-3:  读 skill → 检索知识库 → 联网搜索（每次结果 >1500 字自动卸载）
轮4-10: 反复检索 + 搜索（上下文压缩触发，ref_id 保留在 summary 里）
轮11-20: bash 画图 + 写脚本（沙箱执行，文件列表返回给 agent）
轮21-35: 更多搜索 + recall_tool_result 按需调取卸载的原始数据
轮36-40: write_file 写报告（结果卸载 93%）
  ↓
轮末: 每轮打日志 [Turn 36/50] LLM 1.5s | ctx~8000t | finish=tool_calls
最终: task_complete 或 turn_max_reached
```

关键：40 轮里上下文从 1200t 涨到 8000t（不是 52000t——因为卸载+压缩），74 次工具调用没有崩溃。

### 核心循环（4 步精简版）

```
   while turn < max_turns:
     ① 决策: LLM 看 history + tools，决定下一步
     ② 执行: 调工具（四道关卡 + 并发）
     ③ 回灌: 结果/错误塞回 history（超长卸载）
     ④ 判定: LLM 不再调工具 = 完成；否则回 ①
```

---

### 2.1 循环本质

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | LLM 调用是"一问一答"，不能自主推进任务 |
| **解决什么** | agent 的本质是循环——不是人写控制流，是 LLM 根据结果自己决定下一步 |
| **功能（怎么做）** | `while current_turn_index < max_turns:` 循环，每轮调 LLM → 分流事件 → 执行工具 → 回灌 |
| **怎么迭代的** | 从 PywenAgent 继承核心循环 → 适配通用 Agent → 加 handoff 分支 → 改并行执行 |
| **如何验证** | 实验 1：修 bug 任务跑了 3 轮（read_file → edit → 完成），trajectory 记录了 3 轮 LLM 交互 |
| **提升什么** | agent 能自主推进多步任务，这是"agent"和"chatbot"的根本区别 |

**关键代码位置**：`agentforge/agents/agent.py` → `run()` + `_process_turn_stream()`

---

### 2.2 失败回灌 —— "撞墙会回头"

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 工具失败直接抛异常，整个任务崩溃 |
| **解决什么** | agent 需要从失败中恢复，而不是崩溃 |
| **功能（怎么做）** | 失败也回灌为 `role="tool"` 消息，LLM 下轮看到错误自己换路。所有失败路径（工具失败/hook deny/用户拒绝/验证失败/反思不达标）都走回灌 |
| **怎么迭代的** | 从 PywenAgent 继承"失败回灌" → 扩展到验证器/反思/DLP 失败也回灌 |
| **如何验证** | 实验 3：读不存在的文件 → agent 自动换 ls 查目录（LLM 看到错误后自纠正） |
| **提升什么** | "撞墙会回头"——错误变成学习信号而非崩溃原因。**这是 agent 区别于普通程序的关键设计** |

**关键代码位置**：`agentforge/agents/agent.py` → `_process_single_result()` 的所有 `if not is_success` 分支

---

### 2.3 收敛与终止

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 终止条件不明确（无限循环或过早结束） |
| **解决什么** | 需要明确的收敛机制 |
| **功能（怎么做）** | 三层终止：①LLM 不再调工具 = 完成 ②`max_turns` 兜底 ③预算超限 |
| **怎么迭代的** | 继承 `finish_reason` 检测 → 加 Budget 检查 → 加 handoff 深度限制 |
| **如何验证** | 实验 5：max_turns=1 时强制停止（只读不修）；budget 超限检查测试（150/100 → 超限） |
| **提升什么** | 防止烧 token + 确定性的收敛保障 |

---

### 2.4 并行工具执行

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | LLM 一次发多个 tool_call 时串行执行，3 个 web_search 要 3 倍时间 |
| **解决什么** | 多个无依赖的工具应该并发跑 |
| **功能（怎么做）** | `_process_tool_calls` 用 `asyncio.gather` 并发执行所有工具，结果顺序 yield |
| **怎么迭代的** | 先改成"分离执行和 yield"（`_execute_single_tool` 纯执行 + `_process_single_result` 纯处理） → gather 并发 |
| **如何验证** | 代码验证：`asyncio.gather(*[execute(tc) for tc in tool_calls])` |
| **提升什么** | 多工具场景从串行 N 倍降到并行 1 倍 |

**关键代码位置**：`agentforge/agents/agent.py` → `_process_tool_calls()` + `_execute_single_tool()` + `_process_single_result()`

---

## 三、工具系统设计（"agent 的手"）

### 数据流程图

```
LLM 决定调工具
      │
      ▼
┌─ autodiscover 扫描 tools/ 子目录 ─┐
│  filesystem/ execution/ search/    │
│  memory/ knowledge/ collaboration/ │     allowed_tools 白名单过滤
│  misc/                             │ ──→  （只给 agent 配置的工具）
└────────────────────────────────────┘
      │
      ▼  LLM 发 tool_call
┌─ 四道关卡 ────────────────────────────────┐
│                                            │
│  ① PreToolUse hook                        │
│     组织策略（如：禁止 rm -rf）             │
│         │ deny → 直接拒绝                 │
│         │ allow ↓                         │
│  ② 用户审批                               │
│     SAFE → 自动放行                        │
│     MEDIUM/HIGH → 弹确认                   │
│         │ reject → 拒绝                   │
│         │ approve ↓                       │
│  ③ tool.execute()                         │
│     真正执行（沙箱/本地/API）               │
│         │                                 │
│  ④ PostToolUse hook                       │
│     结果检查（如：DLP 扫描）                │
│         │ block → 覆盖结果                │
│         │ pass ↓                          │
└────────────┬───────────────────────────────┘
             │
             ▼
    工具结果 → 回灌 conversation_history
    （超长结果 → ContextManager 卸载 → 符号进历史）
```

### 结构说明

工具系统解决的核心问题：**agent 怎么和外部世界交互，以及怎么安全地交互。**

21 个工具按功能分 6 个子目录。每个工具继承 `BaseTool`（自动获得 `build()` 方法），只需要实现 `execute()`。工具执行要走四道关卡——hook → 审批 → 执行 → hook，任何一道关卡拦下来都会回灌"拒绝"信息给 LLM。

### 实例：短程任务——读写文件（2 个工具协作）

```
轮1: LLM → read_file("config.yaml")
  ① SAFE 工具，自动放行
  ③ 读取文件内容
  ④ 回灌: "port: 8080, debug: true"

轮2: LLM → edit("config.yaml", "port: 9090")
  ① hook 检查通过
  ② MEDIUM 风险，弹确认 → 用户按 y
  ③ 修改文件
  ④ 回灌: "✅ 已修改 port: 8080 → 9090"

轮3: LLM → "[stop] 配置已更新"
```

### 实例：长程任务——知识库检索（多工具组合）

```
轮1: LLM → knowledge_search("架构设计原则")
  → Milvus 混合检索 → 5 条结果（每条 5000 字）
  → 超长卸载：5×5000=25000 字 → 符号 500 字进历史

轮2: LLM → recall_tool_result(ref_id="ctx_xxx")
  → 按需取回第 1 条完整内容

轮3: LLM → web_search("2026 行业趋势")
  → Tavily 返回 5 条 → 超长卸载

轮4: LLM → bash("python3 generate_chart.py")
  → 沙箱执行 → 生成 PNG → 文件列表返回
```
4 个工具协作：知识库检索 → 按需调取 → 联网搜索 → 代码画图。

### 3.1 四道关卡 —— 安全执行的精髓

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | LLM 的工具调用没有安全控制，可能跑 `rm -rf /` 或 `git push production` |
| **解决什么** | LLM 是个"聪明的冒失鬼"，每步工具调用需要多层拦截 |
| **功能（怎么做）** | 四道关卡（顺序不能换）：①PreToolUse hook（组织级策略，deny 直接拒）→ ②用户审批（LOCKED/EDIT_ONLY/PLANNING/YOLO）→ ③执行 → ④PostToolUse hook（结果审查） |
| **怎么迭代的** | 继承自 pywen 四道关卡 → session_id 打通 hook payload → 危险命令黑名单（rm -rf/format/mkfs 永不自动批） |
| **如何验证** | 实验 2：LOCKED 档弹审批，用户按 n → bash 根本不执行（不是执行了再拦） |
| **提升什么** | 企业级安全：每步可控、可审计、可阻断。**这是 AgentForge 最强的差异化能力**——LangChain 的 `tool.run()` 一道关都没有 |

**为什么顺序不能换**：
- hook 在审批前：组织策略比个人意愿优先级高
- 审批在执行前：执行了就晚了（rm -rf 删不回来）
- 事后 hook 在执行后：输出含密码这种问题只有跑完才看见

**关键代码位置**：`agentforge/tools/tool_manager.py` → `execute()`

---

### 3.2 插件化工具注册

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 加新工具要改 agent 代码 |
| **解决什么** | 工具应该是插件——建文件就生效 |
| **功能（怎么做）** | `autodiscover` 递归扫 `tools/` 子目录（filesystem/execution/search/memory/knowledge/collaboration/misc），`@register_tool` 装饰器在 import 时自动入册 `TOOL_REGISTRY`。providers 统一 `"*"`，build() 在基类 |
| **怎么迭代的** | 继承自 pywen → 验证"建文件即生效" → 删 4 个死工具（apply_patch/codex_shell/update_plan/think）精简到 14 个 |
| **如何验证** | 建 my_tool.py + 装饰器 → 启动后自动注册到 TOOL_REGISTRY |
| **提升什么** | 零配置加工具，开发者体验 |

---

### 3.3 MCP 多协议支持

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | MCP 只支持 stdio，不支持远程（魔搭社区的 SSE server 用不了） |
| **解决什么** | 打通 MCP 生态（本地 + 远程） |
| **功能（怎么做）** | 支持 3 种传输：stdio（本地进程）/ http（streamablehttp）/ SSE（远程）。兼容 Claude Desktop 的 `mcpServers:{}` 配置格式 |
| **怎么迭代的** | 加 `sse_client` → 改 dispatch 支持 `type=sse` → 加 `model_validator` 转换配置格式 → 验证三种格式 |
| **如何验证** | 魔搭 chart server SSE 连通，拉到 27 个工具；npx stdio server 拉到 27 个工具 |
| **提升什么** | MCP 生态全打通，任何 MCP server 都能接入 |

**关键代码位置**：`agentforge/tools/mcp_tool.py`

---

### 3.4 验证器 —— 机器级质量门控

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 工具执行后不知道结果对不对（修了 bug 但不知道修没修对） |
| **解决什么** | 工具执行后需要独立检查 |
| **功能（怎么做）** | `ToolVerifier`：配置驱动，edit 后自动跑 `py_compile`/`pytest`。不通过走回灌让 LLM 纠正 |
| **怎么迭代的** | 新建 `verifier.py` → AgentConfig 加 `verify_after` 字段 → 失败走回灌 |
| **如何验证** | 验证命令成功 → `passed=True`；语法错误 → `passed=False` + 回灌错误 |
| **提升什么** | 机器级质量门控，防止低质量结果进入下一轮 |

**关键代码位置**：`agentforge/agents/verifier.py` → `ToolVerifier`

---

### 3.5 工具系统标准化 —— 从散乱到规整

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 21 个工具文件平铺在目录无分类、26 处重复的 build() 方法、providers 标签不统一（死代码）、旧 memory_tool 与新记忆工具功能重叠、dlp.py 不是工具却放在 tools/ 里 |
| **解决什么** | 工具系统需要标准化、可维护、符合生产规范 |
| **功能（怎么做）** | ① BaseTool 加通用 build()，21 个子类删除重复（22 处→0 处）② providers 全部统一 `"*"`（17 处）③ 按功能分 6 个子目录（filesystem/execution/search/memory/knowledge/collaboration/misc）④ 删除旧 memory_tool（功能被 remember_fact/recall_memory 覆盖）⑤ 同步更新 7 处外部引用 |
| **怎么迭代的** | 先用 Explore agent 查清全链路 11 处依赖 → 阶段一改代码不移动文件（零风险）→ 阶段二移动文件+更新引用 → 每步跑 156 测试 |
| **如何验证** | 156/156 测试零回归；21 个工具全部注册；所有 import 路径正常（main/agent/server/tests/scripts）|
| **提升什么** | 新增工具只写 execute()（build 自动继承）；文件按功能分类，一眼看清能力；providers 不再误导 |

**关键代码位置**：`tools/base_tool.py`（通用 build）+ `tools/{filesystem,execution,search,memory,knowledge,collaboration,misc}/`

---

## 四、多 Agent 协作设计（"从一个人到一个团队"）

### 数据流程图

```
                         主 Agent
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼              ▼
         delegate        spawn_agent     handoff
         (预定义委派)    (动态创建)      (转交控制)
              │             │              │
              │             │              │ 主 agent 暂停
              ▼             ▼              ▼
         ┌────────┐   ┌──────────┐   ┌──────────┐
         │ Peer A │   │ Sub Agent│   │ Peer B   │
         │(已有配 │   │(动态配置)│   │(接管任务)│
         │ 置的   │   │          │   │          │
         │ agent) │   │独立上下文│   │          │
         └───┬────┘   └────┬─────┘   └────┬─────┘
             │             │              │
     ┌───────┴──────┐      │              │
     ▼              ▼      │              │
  同步等待      后台并发    │              │
  (阻塞主循环)  (不阻塞)   │              │
     │              │      │              │
     └──────┬───────┘      │              │
            │              │              │
            ▼              ▼              ▼
     结果回灌主 Agent   结果入 TaskManager  控制权归还
     (conversation_     (异步查询结果)     (主 agent 结束)
      history 共享)
```

### 结构说明

多 Agent 协作解决的核心问题：**一个 agent 做不了所有事——需要分工。**

三种协作模式：
- **delegate（委派）**：主 agent 把子任务交给已有的 peer agent（配置里预定义的）。同步阻塞——等 peer 跑完才继续。也可以后台并发——起 asyncio.create_task，不阻塞主循环
- **spawn_agent（动态创建）**：主 agent 运行时动态创建一个全新子 agent（传自定义 instructions + tools），交给 TaskManager 管理，异步执行
- **handoff（转交）**：主 agent 把整个任务的控制权交给另一个 agent，自己暂停

安全限制：handoff 深度最多 3 层（可配置），防止 A→B→A→B 无限循环。

### 实例：多 Agent 协作——代码审查场景

```
配置: coordinator（调度）+ coder（写代码）+ reviewer（审查）

coordinator 收到任务 "修复 bug":
  │
  ├─ delegate → coder: "修复 src/app.py 的空指针"
  │    ├─ coder 轮1: read_file → 读代码
  │    ├─ coder 轮2: edit → 修改代码
  │    └─ coder 轮3: "[stop] 已修复" → 结果回灌 coordinator
  │
  ├─ delegate → reviewer: "审查 coder 的修改"
  │    ├─ reviewer 轮1: read_file → 读修改后的代码
  │    └─ reviewer 轮2: "[stop] 修改正确" → 结果回灌
  │
  └─ coordinator: "[stop] 任务完成"
```

三个 agent 各司其职，通过 SharedState 共享对话历史。coordinator 看得到 coder 和 reviewer 的全部交互。

### 4.1 共享状态 + delegate —— 预定义委派

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 单 agent 做复杂任务时上下文污染（一个 agent 干太多事 history 混乱） |
| **解决什么** | 复杂任务需要分工隔离 |
| **功能（怎么做）** | `SharedState`（所有 agent 共享同一份 history/peers/depth/budget）+ `delegate` 工具（coordinator 委派给 worker）+ 配置驱动团队（peers 连边） |
| **怎么迭代的** | 加 delegate_tool → 加 handoff 信号检测（字符串前缀编码） → 加 SharedState → 改主循环 handoff 分支 |
| **如何验证** | 确定性测试：handoff 编解码 + 共享状态写入互见（planner 写 history，coder 立刻看到） |
| **提升什么** | 从"一个人干所有"到"团队协作" |

**关键代码位置**：`agentforge/agents/shared_state.py` + `agentforge/tools/collaboration/delegate.py`

---

### 4.2 并行 delegate —— 非阻塞委派

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | delegate 时主 agent 阻塞等 peer 跑完，一个 delegate 卡住整个流程 |
| **解决什么** | 多个 delegate 应该并发跑 |
| **功能（怎么做）** | peer 后台 `asyncio.create_task` 跑，结果通过 `asyncio.Queue` 异步收集。主 agent 不阻塞，继续干别的 |
| **怎么迭代的** | 分离"执行"和"yield" → `_run_peer_background` 后台跑 → `_collect_peer_results` 轮末收集 → `_await_all_peers` task_complete 前等所有 |
| **如何验证** | 冒烟通过 + 逻辑验证（peer 非阻塞返回"已委派"，不卡住其它工具） |
| **提升什么** | 多 delegate 并发跑，总耗时从串行 N 倍降到接近 1 倍 |

**关键代码位置**：`agentforge/agents/agent.py` → `_run_peer_background()` + `_collect_peer_results()` + `_await_all_peers()`

---

### 4.3 spawn_agent —— 动态生成子 agent

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 团队只能预定义，不能按需动态生成 |
| **解决什么** | LLM 需要根据任务自己决定要什么 agent（能力 + 工具 + 指令） |
| **功能（怎么做）** | `spawn_agent` 工具：LLM 指定 task + tools + instructions → 动态构造 AgentConfig → 提交到 TaskManager 异步执行 → `check_sub_agent` 查进度 |
| **怎么迭代的** | 新建 spawn_tool → 复用 TaskManager → 加 CLI 模式降级（不在 serve 模式给友好提示） |
| **如何验证** | spawn → 返回 task_id → check 看到 pending → completed |
| **提升什么** | 从"叫人"（delegate 预定义）到"造人"（spawn 动态生成），按需自组织 |

**关键代码位置**：`agentforge/tools/collaboration/spawn.py`

---

### 4.4 handoff 深度限制 —— 防死循环

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | handoff 可能 A→B→A→B 无限循环（互相踢皮球） |
| **解决什么** | 多 agent 容易发散，需要收敛保障 |
| **功能（怎么做）** | `SharedState._handoff_depth` 计数器，上限 3 层。超过拒绝 delegate + 回灌"超限" |
| **怎么迭代的** | 加深度计数 → handoff 分支检查 → finally 递减 → run() fresh 时重置 |
| **如何验证** | 深度 ≥ 3 时拒绝继续 delegate |
| **提升什么** | 防止资源浪费和死循环 |

---

### 4.5 配置校验 —— 早报错

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 配置错误（peer 不存在/coordinator 重复）运行时才暴露 |
| **解决什么** | 用户配错了启动时报错不友好 |
| **功能（怎么做）** | `_validate_team_config`：启动时校验 peers 存在 / coordinator 唯一 / 工具注册 |
| **怎么迭代的** | 加校验方法 → init_team 里调用 → 错误信息清楚 |
| **如何验证** | 配置里写不存在的 peer → 启动时报错退出（不等到运行时 delegate 才崩） |
| **提升什么** | 错误早发现，用户体验 |

---

## 五、记忆系统设计（"会话内管理 + 跨会话记忆"）

> 记忆系统解决两个问题：同一次对话进行中上下文怎么管（会话级），新开对话怎么记住上次的事（跨会话级）。
> 会话级 5 个组件管"当前对话的生命周期"；L0~L3 四层金字塔管"跨对话的持久记忆"。

### 数据流程图

```
┌─ 跨会话记忆（L0~L3 金字塔，新对话时加载）─────────────────────┐
│                                                                │
│  会话开头加载:                                                  │
│  L3 核心画像（"用户是架构师，技术栈 Python"）  ← 全量载入 prompt │
│  L2 场景记忆（"知识库开发" 摘要）              ← 按任务匹配       │
│  L1 原子事实（"用户偏好 DeepSeek"）            ← 关键词检索       │
│                                                                │
│  会话结束时沉淀:                                                │
│  → 每 5 轮自动提取 L1 事实                                     │
│  → 会话结束自动生成 L2 场景                                    │
│  → 完整对话存 L0 原始会话                                      │
└────────────────────────────────────────────────────────────────┘
         │ 加载到 conversation_history 开头
         ▼
┌─ 会话级记忆（一次对话的生命周期）──────────────────────────────┐
│                                                                │
│  工具结果回灌时:                                                │
│  ┌─ 超 1500 字 ──→ ContextManager 卸载到磁盘                    │
│  │                  → 符号化摘要进历史（压缩率 93%）             │
│  └─ 短结果 ──────→ 直接进 conversation_history                  │
│                                                                │
│  每轮结束时:                                                    │
│  → checkpoint 每 3 轮存快照（工具回灌后，不是 LLM 响应时）       │
│  → 上下文 >75% 时压缩（LLM 摘要 + ref_id 回贴）                 │
│  → 有后台 peer 时跳过压缩（避免竞争）                            │
│                                                                │
│  对话结束时:                                                    │
│  → Redis 工作记忆写回（24h TTL）                               │
│  → SQLite 会话存盘                                              │
│  → GC 清理旧归档/checkpoint                                     │
└────────────────────────────────────────────────────────────────┘
         │ 底座: SQLite WAL + Redis + 磁盘文件（全部有 GC/TTL）
         ▼
┌─ 数据安全 ────────────────────────────────────────────────────┐
│  SQLite WAL + busy_timeout=5s（并发不撞锁）                     │
│  连接单例池（MemoryStore + LongTermMemory 共享）                │
│  归档失败不压缩（证据不丢）                                      │
│  错误打 ERROR 日志（不静默吞）                                   │
└────────────────────────────────────────────────────────────────┘
```

### 结构说明

记忆系统解决两个时间尺度的问题：

**会话级（分钟到小时）**：对话进行中，工具结果越积越多——超长结果卸载到磁盘、上下文太长自动压缩、每 3 轮存快照防崩溃。

**跨会话级（天到月）**：新开对话时，agent 不应该"失忆"——L3 画像告诉它"用户是谁"、L2 场景告诉它"在做什么项目"、L1 事实告诉它"用户告诉过什么"。

### 实例：长程任务——上下文管理全过程

```
轮1-5: 检索知识库（每次 5000 字结果）
  → 5 次卸载：25000 字 → 符号 2500 字进历史
  → 上下文: 1200t → 3700t（没爆，因为卸载了）

轮10: 联网搜索 5 次（每次 7800 字）
  → 5 次卸载：39000 字 → 符号 3900 字
  → 上下文: 3700t → 5600t

轮15: write_file 写报告（54000 字）
  → 卸载：54000 字 → 符号 300 字
  → 上下文: 5600t → 6300t（不是 52000t！）

轮20: 压缩触发（>75%）
  → 抽出所有 ref_id → LLM 摘要 → ref_id 回贴
  → 上下文: 6300t → 1200t

最终: 40 轮、74 次工具调用，上下文峰值 6300t（不到窗口 10%）
```

### 实例：跨会话记忆——第二次对话

```
第一次对话结束时:
  → L1 自动提取: "用户在做 AgentForge 项目"
  → L1 自动提取: "用户偏好 DeepSeek 模型"
  → L2 自动沉淀: 场景 "AgentForge 知识库开发"

第二次对话开始时:
  → L3 注入: "用户是后端架构师，技术栈 Python"
  → L2 匹配: "AgentForge 知识库开发" 场景摘要
  → L1 检索: "用户偏好 DeepSeek"

agent 从第一轮就知道：用户是谁、在做什么、偏好什么——不需要重新问。
```

### 记忆架构总览

```
   层次          实现                    作用
   ────          ────                    ────
   ① 工作记忆    conversation_history    当前会话所有消息（共享）
   ② 上下文压缩  memory_monitor          token 超 75% 自动摘要（三级降级）
   ③ 会话持久化  SQLite sessions         close 时存盘，重启恢复
   ④ checkpoint  SQLite checkpoints      每 3 轮存快照，崩了恢复到任意轮
   ⑤ 跨会话检索  recall + FTS5           搜索历史会话（关键词检索）
```

---

### 5.1 上下文压缩

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | LLM 上下文有上限（32k/128k），长会话必然爆 |
| **解决什么** | 必须忘一部分才能继续 |
| **功能（怎么做）** | `memory_monitor`：75% 阈值触发（所有运行模式都走），三级降级摘要（fidelity≥80/75/70），全失败保最近 30% 兜底。配合 ContextManager 工具结果卸载（>1500 字自动卸载）。压缩前抽出所有 ref_id，压缩后回贴到 summary 末尾（防止卸载的工具结果变孤儿）|
| **怎么迭代的** | 继承自 pywen → 改成原地 `clear+append`（不断共享引用）→ 加质量评分 |
| **如何验证** | 实验：45460 字符 → 243 字符，187x 压缩比；三级降级 6 次 LLM 调用 |
| **提升什么** | agent 能长跑不爆上下文 |

---

### 5.2 会话持久化

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 进程退出后历史全丢 |
| **解决什么** | 会话需要跨进程恢复 |
| **功能（怎么做）** | SQLite `sessions` 表：close 时存盘，run 开头恢复（优先 checkpoint > session > 全新装配）。生产级加固：WAL 模式 + busy_timeout=5s（并发不撞锁）+ 连接单例池 `get_memory_store()` + `cleanup_session()` 清理旧归档 + Redis 工作记忆 24h TTL 自动过期 |
| **怎么迭代的** | 加 `MemoryStore` → close 时存 → run 开头恢复 → 加 WAL + GC + 单例 → 加 checkpoint tool_calls 还原 |
| **如何验证** | 同 session_id 重跑 → "恢复 5 条历史消息" + agent 能回顾之前对话 + WAL 验证 journal_mode=wal + checkpoint 恢复后消息配对不 400 |
| **提升什么** | 断点续做，不丢上下文 |

---

### 5.3 checkpoint —— 中断/恢复

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 崩在第 5 轮只能从头开始 |
| **解决什么** | 需要精确恢复到任意中间点 |
| **功能（怎么做）** | checkpoint 表：每 3 轮存完整快照（history + turn + depth）。恢复时优先用 checkpoint（能恢复到第 N 轮）。生产级加固：① 存的时机从 RESPONSE_FINISHED 移到工具结果回灌后（避免半残消息）② 反序列化还原 tool_calls（之前丢字段导致恢复后 API 400）③ 会话结束时 cleanup_session 清理旧 checkpoint |
| **怎么迭代的** | 加 checkpoint 表 → `RESPONSE_FINISHED` 后存 → 改到工具回灌后存 → 修复 tool_calls 反序列化 → 加 cleanup 清理 |
| **如何验证** | 存 checkpoint(turn=3) → 重跑恢复到 turn=3 + 消息配对正确（tool_calls 完整还原，不 400）|
| **提升什么** | 精确恢复到中间轮次，不丢中间进度 |

---

### 5.4 跨会话检索 + 长期分层记忆（L0~L3）

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 新开对话 agent 啥都不记得——不知道用户是谁、在做什么项目。旧的跨会话检索只有关键词（FTS5），没有分层、没有画像 |
| **解决什么** | 跨会话的持久记忆 + 按重要性分层召回 |
| **功能（怎么做）** | L3 核心画像（用户是谁，全量载入）→ L2 场景记忆（在做什么项目，按任务匹配）→ L1 原子事实（用户告诉过什么，关键词检索）→ L0 原始会话（完整记录，按需取回）。每 5 轮自动提取 L1 事实，会话结束自动沉淀 L2 场景 |
| **怎么迭代的** | 加 FTS5 关键词检索 → 加 LongTermMemory L1~L3 → 删旧 UserMemory（死代码）→ 加批量插入 + 会话结束自动沉淀 L2 |
| **如何验证** | L3 画像注入生效 + L1 自动提取 34 条事实 + L2 场景匹配正确 + agent 主动调 recall_memory |
| **提升什么** | 跨会话记忆持久化；agent 一开始就知道用户是谁、在做什么 |

**关键代码位置**：`memory/longterm.py`（LongTermMemory）+ `tools/memory/longterm.py`（remember_fact/recall_memory/save_scenario）+ `tools/memory/recall_history.py`（FTS5 检索）

---

## 六、韧性设计（"外部调用怎么不崩"）

### 数据流程图

```
agent 调外部服务（LLM / 工具 / 知识库）
       │
       ▼
┌─ CircuitBreaker（熔断器）──────────────────┐
│                                            │
│  状态机:                                    │
│  closed（正常）──连续失败 5 次──→ open（熔断）
│       ↑                              │     │
│       └── 90 秒后探测成功 ── half_open ┘    │
│                                    │        │
│  open 状态: 直接返回失败，不调外部服务      │
│  half_open: 放一个请求试探                 │
│  closed: 正常调用                           │
└────────────────┬───────────────────────────┘
                 │ closed（正常）
                 ▼
┌─ 重试 + 超时 ──────────────────────────────┐
│  timeout=60s（防卡死）                      │
│  max_retries=3（429/5xx 指数退避重试）       │
│  retry → 成功 → 返回                        │
│        → 3 次都失败 → 熔断器计数 +1          │
└────────────────┬───────────────────────────┘
                 │
                 ▼
        成功 → 返回结果给 agent
        失败 → 回灌错误（LLM 下轮自纠正）
```

### 结构说明

韧性设计解决的核心问题：**外部服务一定会挂——LLM 超时、知识库断连、沙箱不可用。agent 不能因为外部服务挂了就崩溃。**

三层保护：① 超时（60 秒，防卡死）② 重试（3 次，指数退避）③ 熔断器（连续 5 次失败熔断 90 秒，避免反复撞限流）。三层都过了还失败，错误回灌给 LLM 让它换一种方法。

### 实例：LLM 服务端 502

```
轮1: 调 LLM → 502 Bad Gateway
  → 重试1: 等 1s → 还是 502
  → 重试2: 等 2s → 还是 502
  → 重试3: 等 4s → 还是 502
  → 熔断器计数 +1（累计 1/5）
  → 错误回灌: "[LLM 错误] 502 Bad Gateway"

轮2: 再调 LLM → 熔断器还没开（1/5）
  → 还是 502 → 熔断器 2/5
  → 错误回灌

轮5: 熔断器 5/5 → 触发熔断（open 状态）
  → 后续 90 秒内不再调 LLM，直接返回失败
  → 避免反复打 API 浪费配额

90 秒后: 熔断器 half_open → 试探一个请求
  → 如果 LLM 恢复了 → 熔断器 closed → 恢复正常
  → 如果还挂 → 继续 open
```

### 6.1 统一韧性层

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | LLM 429/超时直接崩（实测遇到过：魔搭社区限流导致 reviewer 崩） |
| **解决什么** | 网络调用必须容错 |
| **功能（怎么做）** | 统一韧性层：`CircuitBreaker`（closed/open/half-open 状态机）+ `resilient_call`（超时 + 指数退避重试 + 熔断检查） |
| **怎么迭代的** | 新建 `resilience.py` → LLM adapter 加 `max_retries=3, timeout=60` → MCP `call_tool` 加熔断 → anthropic 同步 → TaskManager 加重试 |
| **如何验证** | 熔断器单元测试（连续失败→open→冷却→half-open→恢复）+ 重试测试（失败 2 次后成功）+ 超时测试 |
| **提升什么** | 网络抖动不崩，自动恢复。**实测 429 不再崩溃**——之前多 agent 跑到一半 reviewer 被限流崩了，现在自动重试 |

**关键代码位置**：`agentforge/utils/resilience.py`

---

### 6.2 三类调用统一接入

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 三类调用（LLM/MCP/子 agent）各写各的容错，重复且不一致 |
| **解决什么** | 一套机制管三类调用 |
| **功能（怎么做）** | LLM（SDK 内置 max_retries + CircuitBreaker）、MCP（每 server 独立熔断 + 30s 超时）、子 agent（TaskManager max_retries + 指数退避 + 超时） |
| **怎么迭代的** | 抽公共韧性层 → 三类调用统一接入 → 每类独立熔断器（按 name 区分） |
| **如何验证** | 三类都有超时/重试/熔断参数 |
| **提升什么** | 一套机制管三类调用，维护一致 |

---

## 七、安全治理设计（"怎么管住 agent"）

### 数据流程图

```
                    agent 每一轮
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼              ▼
    ┌─ Budget ──┐ ┌─ DLP ──────┐ ┌─ 权限 ─────┐
    │           │ │             │ │            │
    │ token 量  │ │ 工具结果回灌前│ │ SAFE 工具   │
    │ 工具次数  │ │ 扫描 6 种   │ │ → 自动放行  │
    │ 费用      │ │ 敏感信息:    │ │            │
    │           │ │ API_KEY     │ │ MEDIUM/HIGH │
    │ 超任一项  │ │ AWS_KEY     │ │ → 弹确认    │
    │ → 终止    │ │ PRIVATE_KEY │ │            │
    │           │ │ EMAIL       │ │ locked 模式 │
    │ 费率按模型 │ │ PHONE_CN    │ │ → 只读工具  │
    │ 查表计算  │ │ ID_CARD_CN  │ │            │
    │           │ │             │ │ yolo 模式   │
    │           │ │ 检测到→mask │ │ → 全放行    │
    └───────────┘ └─────────────┘ └────────────┘
          │             │              │
          └─────────────┼──────────────┘
                        ▼
              agent 安全地运行
              （预算不超、敏感不泄、操作受控）
```

### 结构说明

安全治理解决的核心问题：**agent 有能力调工具、写文件、联网——怎么确保它不做危险的事？**

三道防线：① Budget（预算限制：token/工具次数/费用，按模型费率计算）② DLP（工具结果里的敏感信息自动 mask，防止泄露给 LLM）③ 权限分级（SAFE 自动放行、MEDIUM/HIGH 弹确认、locked 只读）。

### 实例：Budget 触发终止

```
配置: max_tokens=50000, max_tool_calls=30, max_cost=$0.50

轮1-25: 正常运行
  → token 累计: 35000/50000 ✅
  → 工具次数: 18/30 ✅
  → 费用: 35000/1000 * $0.002 = $0.07 ✅

轮26: LLM 返回
  → token 累计: 52000/50000 ❌ 超了！
  → "token 预算耗尽（52000/50000）"
  → task_complete → 任务终止
```

### 实例：DLP 脱敏

```
工具结果: "联系我: test@example.com, API_KEY=sk-abc123"
  ↓ DLP 扫描
检测到: EMAIL × 1, API_KEY × 1
  ↓ mask
回灌历史: "联系我: test@***.com, API_KEY=***"
  ↓
LLM 只看到脱敏后的内容（敏感信息不进上下文）
```

### 7.1 Budget Manager

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | agent 可能无限探索烧 token（search → spawn → reflection → retry → search...） |
| **解决什么** | 费用失控 |
| **功能（怎么做）** | `Budget Manager`：配置 `max_tokens/max_tool_calls/max_cost`，每轮检查，超限强制 `task_complete`。费率按模型名查表（deepseek $0.002/1K，claude-sonnet $0.015/1K，gpt-4 $0.06/1K 等），不再硬编码 GPT-3.5 费率 |
| **怎么迭代的** | SharedState 加 budget → AgentConfig 加配置 → 每轮 RESPONSE_FINISHED 后检查 → tool_calls 每次调工具后 +1 |
| **如何验证** | 超限检查测试（150/100 → 超限 → task_complete("预算超限")） |
| **提升什么** | 费用可控，防止"一个任务花 $100" |

---

### 7.2 DLP 敏感信息检测

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 工具输出可能泄露密码/API key（bash 执行 `env` 看到所有环境变量） |
| **解决什么** | 数据安全合规 |
| **功能（怎么做）** | `DLPDetector`：6 种模式（API_KEY/EMAIL/PHONE_CN/AWS_KEY/PRIVATE_KEY/ID_CARD_CN），工具结果回灌 history 前自动扫描 + mask |
| **怎么迭代的** | 新建 `dlp.py` → 工具回灌前扫描 → 自动替换 → AgentConfig 加 `enable_dlp` |
| **如何验证** | DLP 测试 8 种场景全过（API key 被替换成 `[API_KEY_MASKED]`，手机号被 mask） |
| **提升什么** | 企业数据安全合规，防止敏感信息进入 LLM 上下文 |

---

### 7.3 Agent Registry

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | agent 注册信息分散在配置里，没有统一目录 |
| **解决什么** | 需要统一的 agent 目录（谁存在、能干什么、什么状态） |
| **功能（怎么做）** | `AgentRegistry`：name/version/role/capabilities/status。启动时自动注册配置里的 agent；API 端点查询 |
| **怎么迭代的** | 新建 `registry.py` → init/init_team 自动注册 → `register_from_config` 推断能力 → API `/agent/registry` |
| **如何验证** | Registry 测试（注册/查询/搜索/删除/覆盖/配置推断） |
| **提升什么** | agent 可发现、可管理、可搜索 |

---

## 八、服务化设计（"从 CLI 到 API 平台"）

### 数据流程图

```
                    HTTP 请求
                        │
                        ▼
              ┌─ FastAPI 鉴权 ─┐
              │ hmac.compare_   │  （防时序攻击）
              │ digest(token)   │
              └───────┬─────────┘
                      │ 认证通过
          ┌───────────┼───────────┐
          ▼           ▼            ▼
    POST /agent/   POST /agent/  GET /agent/
    run (同步)     tasks (异步)  tasks/{id}
          │           │            │
          │      TaskManager      查询状态
          │      Semaphore(3)     /结果/取消
          │      最多 3 个并发
          │           │
          ▼           ▼
    ┌─ InfraManager 启动时初始化 ──┐
    │ Milvus + MinIO + Redis      │
    │ + Sandbox + Embedding       │
    │ → 注入到工具层和 agent       │
    └──────────────────────────────┘
          │
          ▼
    AgentManager → Agent.run() → 事件流 → JSON 响应
```

### 结构说明

服务化解决的核心问题：**从命令行工具变成 API 平台——多个用户/请求可以同时用 agent，任务可以异步提交和查询。**

两种调用模式：① 同步（`/agent/run`，等 agent 跑完返回）② 异步（`/agent/tasks`，提交后返回 task_id，后台跑完用 `/agent/tasks/{id}` 查结果）。异步模式用 Semaphore(3) 限制最多 3 个任务同时跑。

### 实例：异步任务全流程

```
1. 提交任务
   POST /agent/tasks  {"prompt": "分析数据"}
   → 返回 {"task_id": "task_abc123"}

2. 轮询状态
   GET /agent/tasks/task_abc123
   → {"status": "running", "turn": 5}

3. 查询结果（几分钟后）
   GET /agent/tasks/task_abc123
   → {"status": "completed", "result": "分析结果..."}

4. 取消（如果太慢）
   POST /agent/tasks/task_abc123/cancel
   → {"status": "cancelled"}
```

### 8.1 FastAPI 服务化

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 只有 CLI，业务系统调不了 |
| **解决什么** | 需要 HTTP API 供外部调用 |
| **功能（怎么做）** | FastAPI：`POST /agent/run`（同步）+ `GET /agent/health` + `GET /agent/sessions` + `GET /agent/registry` + `GET /agent/infra` |
| **怎么迭代的** | 新建 `server/app.py` + `HeadlessCLI`（不弹窗，自动批）→ 加 `--serve` 模式 → 加鉴权中间件 |
| **如何验证** | curl 提交任务 → 返回 JSON + 健康检查 OK + Swagger UI |
| **提升什么** | 从"个人工具"到"服务平台" |

---

### 8.2 异步任务系统

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | API 同步阻塞，多用户排队等 |
| **解决什么** | 需要异步任务系统（提交 → 查进度 → 取消） |
| **功能（怎么做）** | `TaskManager`：`POST /agent/tasks`（提交→task_id）→ `GET /agent/tasks/{id}`（查进度）→ `POST /cancel` → `GET /agent/tasks`（列表）。Semaphore(3) 并发限流 |
| **怎么迭代的** | 新建 `task_manager.py` → 加重试/超时 → 加 spawn 支持 → 接 Redis 持久化 |
| **如何验证** | 并发 3 个任务 → 各自独立完成 + 第 4 个排队 + 取消生效 |
| **提升什么** | 多用户并发，提交即返回不阻塞 |

---

### 8.3 并发隔离

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 多请求共享全局 ToolManager → 并发踩踏（token 统计互相覆盖） |
| **解决什么** | 每请求需要独立状态 |
| **功能（怎么做）** | 每请求建独立 `AgentManager + ToolManager + PermissionManager` |
| **怎么迭代的** | 从全局共享改每请求独立 → 验证 session_id 互不干扰 |
| **如何验证** | 两个并发请求 → 独立 session_id（440aa2f1 / 03092ce7）互不干扰 |
| **提升什么** | 真正的并发隔离 |

---

### 8.4 API 鉴权

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | API 没有鉴权，任何人都能调 |
| **解决什么** | 生产环境不能裸奔 |
| **功能（怎么做）** | Bearer token 中间件（环境变量 `AGENTFORGE_API_TOKEN` 控制开关） |
| **怎么迭代的** | 加 middleware → 设环境变量 → 不设=开发模式（无鉴权） |
| **如何验证** | 无 token → 401 Unauthorized |
| **提升什么** | 基本安全门槛 |

---

## 九、闭环设计（"验证→纠正→迭代"的完整闭环）

### 数据流程图

```
    工具执行完毕
         │
         ▼
┌─ 验证器（机器级）─────────────────────────┐
│  verify_after 配置驱动:                   │
│  edit → py_compile {file_path}            │
│  write_file → pytest {file_path}          │
│        │                                  │
│   通过？─是→ 正常回灌                      │
│        │                                  │
│       否→ 回灌 "[VERIFY FAILED] 语法错误"  │
│        │                                  │
│        ▼  LLM 下轮看到错误                 │
│   自纠正 → 再调 edit → 再验证              │
└───────────────────────────────────────────┘
         │
         ▼
┌─ Reflection（LLM 级）─────────────────────┐
│  LLM 打分 0-100:                          │
│  质量 ≥80 → 通过                          │
│  质量 <80 → 回灌改进建议                   │
│  最多反思 2 次，第 3 次强制通过             │
└───────────────────────────────────────────┘
         │
         ▼
┌─ 离线评估（数据集级）──────────────────────┐
│  JSON 数据集 → 跑 agent → 对比期望结果     │
│  指标: 成功率 / 工具效率 / 轨迹质量         │
│  用途: 改了代码后跑评估确认没退步            │
└───────────────────────────────────────────┘
```

### 结构说明

闭环设计解决的核心问题：**agent 怎么保证输出质量？不是靠感觉，是靠验证。**

三层验证：① 验证器（机器级，跑命令验证）② Reflection（LLM 级，打分+改进建议）③ 离线评估（数据集级，批量验证质量趋势）。每一层不过都回灌给 LLM 自纠正——不是直接失败，而是给 agent 机会修正。

### 实例：代码修复的验证闭环

```
轮1: LLM → edit("app.py", "在 line 42 加 null 检查")
  → 工具执行成功
  → 验证器: py_compile app.py
  → ❌ 语法错误: 缩进不对
  → 回灌: "[VERIFY FAILED] IndentationError"

轮2: LLM 看到 IndentationError
  → edit("app.py", "修正缩进")
  → 验证器: py_compile app.py
  → ✅ 通过
  → Reflection: LLM 打分 85/100
  → 通过

轮3: LLM → "[stop] 已修复并验证"
```

agent 不是写完就完——它要自己验证"改对了没有"，错了就自己改。

### 完整闭环

```
   上下文 + 工具 ──► 决策 ──► 约束 ──► 执行 ──► 结果回灌
      ✅           ✅        ✅        ✅        ✅
                                        │
                                        ▼
                              ① 验证器（机器级）✅
                              ② Reflection（LLM 级）✅
                                        │
                              不通过 → 回灌反馈 → LLM 下轮纠正
```

---

### 9.1 显式验证器

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 工具执行后没有质量检查（修了 bug 不知道对不对） |
| **解决什么** | 缺少"验证发现偏差"这一环 |
| **功能（怎么做）** | `ToolVerifier`：配置驱动，edit 后自动跑 py_compile/pytest。不通过走回灌 |
| **怎么迭代的** | 新建 verifier.py → AgentConfig 加 verify_after → 失败走回灌 |
| **如何验证** | 验证命令成功 → passed；语法错误 → failed + 回灌 |
| **提升什么** | 机器级质量门控 |

---

### 9.2 Reflection 质量评估

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 验证通过但结果质量不达标（语法没错但逻辑错了） |
| **解决什么** | 需要 LLM 级质量评估和迭代改进 |
| **功能（怎么做）** | `ReflectionEngine`：LLM 打分 0-100，不达标带反馈重试（max_reflections 限制防无限循环） |
| **怎么迭代的** | 加反思引擎 → max_reflections 限制 → reset 防累计 → 新任务时重置 |
| **如何验证** | LLM 给"加法函数"打 95 分，acceptable=True |
| **提升什么** | 质量驱动迭代改进，闭环完整 |

---

### 9.3 离线评估

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 不知道 agent 效果好不好 |
| **解决什么** | 需要数据驱动评估（不是靠感觉） |
| **功能（怎么做）** | `EvaluationRunner` + `metrics`（成功率/工具效率/轨迹质量）+ `--evaluate` CLI 入口 |
| **怎么迭代的** | 新建 `evaluation/` → 加数据集格式（JSON）→ 加指标计算 → 示例数据集 |
| **如何验证** | 评估框架 import + metrics 计算 + 数据集加载 |
| **提升什么** | 数据驱动迭代，不靠猜 |

---

## 十、基础设施层设计（"统一管理外部依赖"）

### 数据流程图

```
agent 启动
    │
    ▼
┌─ InfraManager.health_check_all() ──────────────────┐
│                                                     │
│  ┌─ Sandbox ──→ health_check() ──→ OK / FALLBACK ──┐│
│  ├─ Redis ────→ ping() ────────→ OK / FALLBACK ──┐ ││
│  ├─ MinIO ────→ bucket_exists() → OK / FALLBACK ──┐││
│  ├─ Milvus ───→ list_collections()→ OK / FALLBACK ─┐││
│  └─ Embedding → GET /health ───→ OK / FALLBACK ──┐│││
│                                                  ││││
│  每个服务: enabled（配置开关）+ available（运行时）││││
│  不通的不崩，自动降级                              ││││
└────────────────────────────────────────────────────┘│││
    │                                                 │││
    │ 注入到工具层                                      │││
    ├─→ SandboxClient → BashTool.set_sandbox()        │││
    ├─→ RedisClient → AgentManager.set_redis()        │││
    ├─→ KnowledgeBase → 全局 _kb_instance              │││
    └─→ MinIOClient → 全局（文档存储）                   ││
                                                      ││
┌─ 运行中: recheck_health() ──────────────────────────┐│
│  某服务挂了 → 标记 unavailable                       ││
│  服务恢复了 → recheck 重新探测 → 恢复 available       ││
│  Milvus 连接断了 → available 实时探测 → 自动重连      ││
└──────────────────────────────────────────────────────┘
```

### 结构说明

基础设施层解决的核心问题：**agent 依赖 5 个外部服务——沙箱、Redis、MinIO、Milvus、Embedding。不能让 agent 直接依赖每个服务的 SDK，否则一个服务改了 agent 就得改。**

InfraManager 是统一入口：启动时健康检查 → 不通的降级 → 注入到工具层。运行中服务断了自动降级，服务恢复了自动重新接入（`recheck_health()`）。Milvus 连接更特殊——每次用之前实时探测，断了自动重连。

### 实例：服务中途挂掉 + 恢复

```
启动: 5 个服务全部 OK → 注入到工具层

运行到第 20 轮: Milvus 连接被服务端断开（空闲超时）
  → knowledge_search 调用时: available 实时探测 → 发现断了
  → 自动重连: MilvusClient(uri=...) → list_collections 验证
  → 重连成功 → 继续检索 ✅
  （agent 完全无感知，不需要重启）

运行到第 30 轮: Redis 网络闪断
  → WorkingMemory.save() 失败 → 标记 _healthy=False
  → 后续操作走内存降级（不崩）
  → 一段时间后调 recheck_health() → Redis 恢复 → 重新接入 ✅
```

### 10.1 InfraManager —— 统一入口

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 多个外部服务（Sandbox/Redis/Milvus/MinIO/Embedding）零散接入，容易断裂（之前"补丁式"添加的教训） |
| **解决什么** | 统一管理 + 自动降级 |
| **功能（怎么做）** | `InfraManager`：统一初始化 + 健康检查 + 注入 + 降级。每个服务 `enabled + available` 双开关 |
| **怎么迭代的** | 建 `infra/` 统一层 → 每服务遵循同一模式 → 配置统一在 infra 段 → 注入而非全局单例 |
| **如何验证** | 五个服务同时健康检查：sandbox=OK, redis=OK, milvus=OK, minio=OK, embedding=OK（不崩，自动降级） |
| **提升什么** | "补丁式"变"统一架构"，衔接不断裂。**这是之前技术债的根治** |

**故障降级原则**：如果某个服务连不上，不崩，降级到现有行为（sandbox→本地 bash，redis→内存队列，milvus→FTS5 关键词检索，embedding→本地模型加载）

---

### 10.2 Sandbox 沙箱执行

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 代码在宿主机直接执行（`asyncio.create_subprocess_shell`），rm -rf 真删文件 |
| **解决什么** | 需要代码执行隔离 |
| **功能（怎么做）** | `SandboxClient`：HTTP 调 Docker 沙箱的 `/execute` 接口。不可用时降级到本地 bash |
| **怎么迭代的** | 建 `sandbox_client.py` → 健康检查 → 降级逻辑 |
| **如何验证** | `execute('print(42)')` → 42；不可用时 available=False |
| **提升什么** | 代码执行隔离，生产安全 |

---

### 10.3 Redis 持久化队列

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 任务队列重启就丢（内存存储） |
| **解决什么** | 需要持久化队列 |
| **功能（怎么做）** | `RedisClient`：任务状态持久化。不可用时降级到内存 |
| **怎么迭代的** | 建 `redis_client.py` → TaskManager 接入 → 降级到内存 |
| **如何验证** | Redis set/get 验证 + TaskManager.set_redis 注入 |
| **提升什么** | 重启不丢任务，多进程共享 |

---

### 10.4 知识库（Milvus + bge-m3）—— 生产级 RAG

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 跨会话检索只有关键词（FTS5），中文搜不准；无文档导入/向量化能力 |
| **解决什么** | 需要语义检索（RAG）—— 文档导入 → 切片 → 向量化 → 混合检索 |
| **功能（怎么做）** | `MilvusStore`（dense + sparse 双向量）+ `bge-m3`（本地 embedding）+ `DocumentLoader`（md/pdf/docx/html）+ `TextChunker`（4 种切片策略）+ RRF 混合检索 + bge-reranker 精排 |
| **怎么迭代的** | 替换 ChromaDB → Milvus 2.6 → bge-m3 dense+sparse → RRF 融合 → 4 种切片策略 → 元数据过滤 → chunk_id/doc_id 双 ID → 图片 URL 存 metadata |
| **如何验证** | 端到端：导入 md → section 切片 → hybrid 检索 → 命中"比亚迪 425 万辆" + 图片 URL；dense 检索 → 命中"固态电池"；按 doc_id 取整篇 |
| **提升什么** | 生产级 RAG：语义检索（不是关键词匹配）+ 多来源融合（RRF）+ 精排（reranker）+ 结构化元数据 |

**关键代码位置**：`knowledge/milvus_store.py` + `knowledge/embedding.py` + `knowledge/loader.py` + `knowledge/chunker.py`

---

### 10.5 Embedding 服务化 —— 模型独立部署

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | bge-m3 模型 2GB 内存，每个 Agent worker 都加载一份 |
| **解决什么** | 模型应该独立部署，Agent 通过 HTTP 调用 |
| **功能（怎么做）** | `embedding_server.py`：独立 FastAPI 服务（`/encode` + `/rerank` + `/health`）。`EmbeddingEngine` 支持双模式：HTTP 优先（server_url）→ 降级本地（model_path） |
| **怎么迭代的** | 建 embedding_server.py → 改 EmbeddingEngine 支持双模式 → health_check 降级 |
| **如何验证** | 7/7 import + MinIO 上传/下载 + 单 agent 冲烟 |
| **提升什么** | Agent 进程不再加载模型（省 2GB × N workers）；可独立扩容；版本隔离 |

**关键代码位置**：`knowledge/embedding_server.py` + `knowledge/embedding.py`

---

### 10.6 MinIO 文档存储 —— 多 worker 共享

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 知识库文档只存在本地文件系统，API 模式下多节点无法共享 |
| **解决什么** | 文档应该存在对象存储 |
| **功能（怎么做）** | `MinIOClient`：上传/下载/列表/删除。不可用时降级到本地路径 |
| **怎么迭代的** | 建 minio_client.py → health_check → InfraManager 统一管理 |
| **如何验证** | 上传 → documents/20260807/test.txt；列表 → 1 个文档；下载 → 21 bytes |
| **提升什么** | 多 worker 共享文档；API 模式可跨节点 |

**关键代码位置**：`infra/minio_client.py`

---

### 10.7 上下文管理 —— 短期符号化记忆

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 多工具任务时，工具结果（web_search 7800字/次、write_file 54000字）全量进上下文，22 次调用后上下文涨到 52000 tokens（81%），LLM 超时崩溃 |
| **解决什么** | 工具结果不能全量进上下文，需要卸载 + 符号化 |
| **功能（怎么做）** | 工具结果 >1500 字时自动卸载到磁盘（ToolResultStore），上下文只放符号化摘要（工具名 + 关键发现 + ref_id）。agent 需要原始数据时用 `recall_tool_result(ref_id)` 按需调取 |
| **怎么迭代的** | 发现超时 → 分析 trajectory 定位到 write_file content 54000 字 → 参照腾讯 TencentDB Agent Memory 的"外部卸载"设计 → 建 context/ 包 → 改 agent.py 回灌逻辑 |
| **如何验证** | 同一场景对比：52006 tokens → 6314 tokens（-88%），40 次工具调用不超时 |
| **提升什么** | 多工具任务不再上下文爆炸；agent 学会了按需调取（recall_tool_result 被主动调用 8 次）|

**关键代码位置**：`context/store.py` + `context/manager.py` + `agents/agent.py:_process_single_result`

**参照设计**：腾讯 TencentDB Agent Memory 的短期符号化记忆："将繁重原始数据外部卸载至存储层，上下文只保留轻量化结构化信息"

**验证细节**：详见 `场景验证/行研报告验证结果.md`（四轮迭代，上下文从 52K tokens 降到 9.7K）

---

### 10.8 长期分层记忆 —— L0~L3 金字塔

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 跨会话失忆——重启对话后 agent 丢失用户偏好、项目上下文；旧 UserMemory 依赖 Redis、无分层、无检索 |
| **解决什么** | 需要跨会话的持久记忆，且按重要性分层、按需召回 |
| **功能（怎么做）** | L0 原始会话（磁盘）→ L1 原子事实（FTS5 检索）→ L2 场景记忆（按任务匹配）→ L3 核心画像（全量载入）。渐进式召回：会话开头 L3→L2→L1，运行中每 5 轮自动提取 L1 事实 |
| **怎么迭代的** | 参照腾讯 L0~L3 设计 → 用 SQLite+FTS5（不依赖 Redis）→ 建 longterm.py → 整合到 agent（会话开头注入 + 定期提取）|
| **如何验证** | L3 画像注入生效；L1 自动提取 34 条事实；L2 场景匹配正确；agent 主动调 recall_memory |
| **提升什么** | 跨会话记忆持久化；agent 一开始就知道用户是谁、在做什么；按需召回不占上下文 |

**关键代码位置**：`memory/longterm.py` + `tools/memory/longterm.py`

**四层结构**：
```
L3 核心画像（全量载入 prompt）    ← "用户是后端架构师，技术栈 Python+FastAPI"
L2 场景记忆（按当前任务匹配）      ← "AgentForge 知识库开发" 场景摘要
L1 原子事实（FTS5 关键词检索）     ← "用户偏好 DeepSeek" / "知识库用 Milvus"
L0 原始会话（按需取回）            ← 完整工具调用记录（ToolResultStore）
```

---

### 10.9 记忆系统生产级加固

| 维度 | 内容 |
|:--|:--|
| **问题（痛点）** | 记忆系统功能搭好了但离生产上线差 5 个硬伤：无 GC 无限增长、SQLite 撞锁、压缩吃掉 ref_id、归档失败丢证据、except:pass 静默吞错 |
| **解决什么** | 数据不爆 + 并发不锁 + 证据不丢 + 错误不藏 |
| **功能（怎么做）** | ① GC（cleanup_session + gc_retention + max_entries + Redis TTL）② SQLite WAL + busy_timeout + 连接单例 ③ 压缩前抽 ref_id 压缩后回贴 ④ 归档失败不压缩 ⑤ except:pass 改 ERROR 日志 |
| **怎么迭代的** | 先用 Explore agent 做完整审计（12 个问题）→ 按 5 阻断+7 修复+2 锦上添花逐个改 → 每步跑 156 测试验证 |
| **如何验证** | 156/156 测试零回归；GC 功能验证（cleanup 删 22 行、retention 100→10）；WAL 验证（journal_mode=WAL） |
| **提升什么** | 记忆系统从"能跑"到"能上线"；数据有生命周期不会撑满；并发安全；运维可感知 |

**关键代码位置**：`memory/store.py`（GC+WAL+单例）+ `context/store.py`（max_entries GC）+ `agents/agent.py`（压缩保护 ref_id）+ `agents/agent_manager.py`（归档事务+L2自动沉淀）

**详细审计报告**：`架构文档/记忆系统生产级审计报告.md`（12 个问题逐项记录）

---

## 设计理念贯穿线

```
   做正确的事（为什么）
   ─────────────────
   ① 消除重复 → 统一内核（所有改进的基础）
   ② 让 agent 自主推进 → 循环 + 失败回灌 + 自纠正
   ③ 让 agent 安全可控 → 四道关卡 + 权限 + DLP + Budget
   ④ 让 agent 记得住 → 会话内管理（压缩+卸载+checkpoint）+ 跨会话记忆（L0~L3）
   ⑤ 让 agent 不崩 → 韧性层 + 降级
   ⑥ 让 agent 能协作 → delegate + spawn + 并行
   ⑦ 让 agent 可服务 → API + 异步任务 + 鉴权
   ⑧ 让 agent 可验证 → 验证器 + Reflection + Evaluation
   ⑨ 让 agent 可恢复 → checkpoint + SQLite + Redis
   ⑩ 让基础设施统一 → InfraManager + 自动降级
   ⑪ 让 agent 有知识 → Milvus RAG + bge-m3 + 混合检索
   ⑫ 让模型独立部署 → Embedding Server + MinIO 文档存储
   ⑬ 让上下文不爆 → 工具结果卸载 + 符号化回灌 + 按需调取
   ⑭ 让 agent 跨会话记忆 → L0~L3 金字塔 + 渐进式召回
```

---

## 各模块实现位置索引

| 模块 | 文件 | 关键方法 |
|:--|:--|:--|
| 通用 Agent | `agents/agent.py` | `run()` + `_process_turn_stream()` + `_process_tool_calls()` + `_check_context_compact()` |
| AgentManager | `agents/agent_manager.py` | `init()` + `init_team()` + `agent_run()` + `agent_context_compact()` + `_auto_persist_longterm()` |
| SharedState | `agents/shared_state.py` | conversation_history + peers + budget + background_peers + working_memory |
| AgentRegistry | `agents/registry.py` | `register()` + `find()` + `register_from_config()` |
| 验证器 + Reflection | `agents/verifier.py` | `ToolVerifier.verify()` + `ReflectionEngine.reflect()` |
| 工具基类 | `tools/base_tool.py` | `BaseTool.build()`（通用，所有工具继承）|
| 工具管线 | `tools/tool_manager.py` | `execute()`（四道关卡）+ `autodiscover()` + `TOOL_REGISTRY` |
| 文件操作工具 | `tools/filesystem/` | read_file / write_file / edit / glob / grep / ls |
| 执行工具 | `tools/execution/bash.py` | `BashTool`（沙箱优先 + 降级本地）|
| 搜索工具 | `tools/search/` | web_search / web_fetch |
| 记忆工具 | `tools/memory/` | remember_fact / recall_memory / save_scenario / recall_history / recall_context |
| 知识库工具 | `tools/knowledge/knowledge_tools.py` | `IngestDocumentTool` + `KnowledgeSearchTool` |
| 协作工具 | `tools/collaboration/` | delegate / spawn_agent / check_sub_agent |
| 其他工具 | `tools/misc/` | todo_write / read_many_files |
| DLP 安全组件 | `tools/dlp.py`（非工具） | `DLPDetector.scan()` |
| MCP | `tools/mcp_tool.py` | stdio/http/sse 三种传输 |
| LLM 统一适配 | `llm/adapters/unified_adapter.py` | `UnifiedAdapter`（chat/responses/messages 三协议合一）|
| LLM 客户端 | `llm/llm_client.py` | `LLMClient`（转发给 UnifiedAdapter）|
| 会话记忆 | `memory/store.py` | `get_memory_store()` 单例 + `save_session()` + `cleanup_session()` + `gc_retention()` |
| 工作记忆 | `memory/working_memory.py` | `WorkingMemory`（Redis 后端 + 24h TTL + 降级内存） |
| 长期分层记忆 | `memory/longterm.py` | `LongTermMemory`（L1 原子事实 + L2 场景 + L3 画像 + 渐进式召回 + 批量插入） |
| 上下文压缩 | `memory/memory_monitor.py` | `run_monitored()`（三级降级 + 阈值 75% + stats 指标） |
| 上下文管理 | `context/manager.py` | `ContextManager`（工具结果卸载 + 符号化摘要 + 按需调取） |
| 工具结果存储 | `context/store.py` | `ToolResultStore`（磁盘卸载 + max_entries GC + 写盘失败不塞缓存） |
| 知识库 | `knowledge/milvus_store.py` | `MilvusStore`（available 实时探测 + 自动重连 + RRF 混合检索）|
| Embedding | `knowledge/embedding.py` | `EmbeddingEngine`（HTTP 优先 + CrossEncoder reranker + 降级本地） |
| Embedding 服务 | `knowledge/embedding_server.py` | bge-m3 独立 HTTP 服务（`/encode` + `/rerank` + `/health`） |
| 文档解析 | `knowledge/loader.py` | `DocumentLoader`（md/pdf/docx/html + 图片 URL + 代码块跳过）|
| 切片器 | `knowledge/chunker.py` | `TextChunker`（固定/章节/递归/自定义 4 种 + section 回填）|
| 韧性层 | `utils/resilience.py` | `CircuitBreaker` + `resilient_call` |
| 轨迹记录 | `utils/trajectory_recorder.py` | `TrajectoryRecorder`（LLM 交互 + token 统计）|
| 会话统计 | `utils/session_stats.py` | `SessionStats`（token/API/工具 聚合统计）|
| 配置 | `config/config.py` | `AppConfig` + `AgentConfig`（provider=unified + wire_api + 阈值可配置）|
| 服务化 | `server/app.py` | FastAPI（hmac 鉴权 + InfraManager 启动注入）|
| 异步任务 | `server/task_manager.py` | `submit()` + `get()` + `cancel()` + Semaphore(3) |
| 基础设施 | `infra/__init__.py` | `InfraManager`（5 服务统一管理 + recheck_health 自动恢复） |
| 沙箱 | `infra/sandbox_client.py` | `SandboxClient`（execute + list/download/upload 文件）|
| Redis | `infra/redis_client.py` | `RedisClient`（操作失败自动降级）|
| MinIO | `infra/minio_client.py` | `MinIOClient`（文档上传/下载 + 降级本地）|
| Sandbox | `infra/sandbox_client.py` | `SandboxClient`（Docker 沙箱执行 + 降级本地） |
| 评估 | `evaluation/runner.py` | `EvaluationRunner.run()` |
| 配置 | `config/config.py` | AppConfig + AgentConfig（14 字段 + infra 配置） |
