---
name: research-report
description: 撰写行业研究报告 / 行研报告 / 调研报告。融合知识库检索、联网搜索、代码生成图表，产出含图片的结构化报告。当用户要求写报告、调研某个主题、做行业分析时触发。
version: 1.0.0
---

# 行业研究报告写作 Skill

本 skill 指导你撰写一份高质量的行业研究报告。报告必须融合**三种信息来源**和**三种图片来源**，最终输出结构化 Markdown。

## 报告写作流程（严格按顺序执行）

### 第一步：知识库检索（RAG 来源）

用 `knowledge_search` 工具，从两个知识库 collection 检索相关内容。

**调用参数模板**（根据报告主题替换 query）：

```
工具: knowledge_search
参数:
  query: "企业级 AI Agent 系统架构设计原则"     ← 替换为你的主题
  top_k: 5
  strategy: "hybrid"
```

**必须检索两个 collection**（分别调用两次）：
1. `kb_ai_implementation` —— AI 实现相关（Agent 架构、工具调用、上下文工程）
2. `kb_architecture` —— 架构思维相关（系统设计、技术选型、演进式架构）

> 注意：knowledge_search 默认搜索当前 collection。如果需要切换 collection，在 query 里注明来源。

**RAG 图片**：检索结果中的 `images` 字段可能包含图片 URL，这些是**RAG 来源的图片**，必须收录到报告里。

### 第二步：联网搜索（实时信息来源）

用 `web_search` 工具，搜索 2026 年最新趋势、行业案例、市场数据。

**至少搜索 3 个不同角度**：

```
工具: web_search
参数:
  query: "2026 企业 AI Agent 架构趋势"
  num_results: 5
```

建议的搜索角度：
1. 技术趋势（如 "2026 AI Agent framework trends"）
2. 行业案例（如 "enterprise AI agent case study 2026"）
3. 市场数据（如 "AI agent market size 2026"）

**联网图片**：搜索结果中的链接可能包含图表、架构图，这些是**联网来源的图片参考**，在报告里标注为"参考链接"。

### 第三步：代码生成图表（代码生成来源）

用 `bash` 工具运行 Python + matplotlib，生成数据图表。

**必须生成至少 2 张图**：

```
工具: bash
参数:
  command: |
    /Users/yangkun/Desktop/Projects/ags_all/.venv/bin/python -c "
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'Heiti TC', 'STHeiti', 'simhei']
    plt.rcParams['axes.unicode_minus'] = False

    # 图1：AI Agent 技术演进趋势
    fig, ax = plt.subplots(figsize=(10, 6))
    years = ['2023', '2024', '2025', '2026']
    adoption = [15, 35, 58, 75]
    ax.plot(years, adoption, 'b-o', linewidth=2, markersize=8)
    ax.set_title('企业 AI Agent 采用率趋势', fontsize=16)
    ax.set_ylabel('采用率 (%)', fontsize=12)
    ax.set_xlabel('年份', fontsize=12)
    ax.grid(True, alpha=0.3)
    for i, v in enumerate(adoption):
        ax.annotate(f'{v}%', (i, v), textcoords='offset points', xytext=(0,10), ha='center')
    plt.tight_layout()
    plt.savefig('/tmp/chart_trend.png', dpi=150)
    print('图1已保存: /tmp/chart_trend.png')
    "
```

> **重要**：画图用 `/Users/yangkun/Desktop/Projects/ags_all/.venv/bin/python`（不要用 python3 或 pip install，.venv 已装好 matplotlib）。

建议生成的图表类型：

建议生成的图表类型：
1. **趋势图**：技术演进 / 市场增长趋势（折线图）
2. **对比图**：不同架构方案对比（柱状图或雷达图）
3. **架构图**：系统架构示意（用 matplotlib 画框图）

**代码生成图片**保存到 `/tmp/` 目录，文件名用 `chart_*.png`。用 `/Users/yangkun/Desktop/Projects/ags_all/.venv/bin/python` 运行画图脚本（已装 matplotlib）。

**报告写入**：最终报告用 `write_file` 写到 `/Users/yangkun/Desktop/Projects/ags_all/场景验证/agent_输出报告.md`。

### 第四步：整合输出报告

把三个来源的信息整合成一份结构化 Markdown 报告。**报告格式**：

```markdown
# [报告标题]

> 报告日期: YYYY-MM-DD | 信息来源: 知识库 + 联网搜索 + 数据分析

## 摘要
[3-5 句话概述核心发现]

## 一、技术架构现状
[来自知识库的内容，标注来源]

![架构趋势](/tmp/chart_trend.png)
<!-- 图片来源: 代码生成 -->

## 二、行业趋势与市场数据
[来自联网搜索的内容，标注来源]

## 三、架构设计原则
[来自知识库 kb_architecture 的内容]

## 四、最佳实践与案例
[综合三个来源]

## 五、总结与建议

## 参考来源
### 知识库文献
- [书名] - 相关章节
### 联网资源
- [标题](URL) - 来源网站
### 图片来源
- 趋势图: 代码生成 (matplotlib)
- 架构图: RAG 检索 / 联网搜索
```

## 关键规则

1. **三种图片来源必须都有**：
   - RAG 检索的图片 URL（第一步 knowledge_search 返回的 images）
   - 联网搜索的参考链接（第二步 web_search 返回的链接）
   - 代码生成的图（第三步 bash + matplotlib 生成的本地文件）

2. **每个事实都要标注来源**：知识库内容标注书名+章节，联网内容标注 URL

3. **报告不少于 1500 字**，结构完整，有数据支撑

4. **图片路径**：代码生成的图保存到 `/tmp/chart_*.png`，报告里用相对路径引用
