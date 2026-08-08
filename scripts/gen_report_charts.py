# -*- coding: utf-8 -*-
"""企业级 AI Agent 系统架构设计 —— 报告图表生成脚本
运行: python3 scripts/gen_report_charts.py  (输出到 /app/workspace)
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

# ---- 中文字体 ----
plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

C_BLUE = "#2563EB"
C_CYAN = "#06B6D4"
C_ORANGE = "#F59E0B"
C_RED = "#EF4444"
C_GREEN = "#10B981"
C_GRAY = "#94A3B8"

# ============ 图1: Agentic AI 全球市场规模预测 ============
years = [2025, 2026, 2027, 2028, 2029, 2030, 2031]
market = [5.34, 10.86, 17.8, 26.5, 38.0, 47.0, 57.42]  # 十亿美元

fig, ax = plt.subplots(figsize=(9, 5.2), dpi=130)
bars = ax.bar([str(y) for y in years], market, color=C_BLUE, alpha=0.85, width=0.62, zorder=3)
for b, v in zip(bars, market):
    ax.text(b.get_x() + b.get_width() / 2, v + 0.8, f"{v:.1f}", ha="center", va="bottom",
            fontsize=9.5, fontweight="bold", color="#1E293B")
ax.plot([str(y) for y in years], market, color=C_ORANGE, marker="o", lw=2, zorder=4,
        label="规模趋势")
# 标注 2026 检索区间
ax.annotate("2026 预测区间 $9.14\\sim10.86B$", xy=(1, 10.86), xytext=(1.35, 16),
            fontsize=9, color=C_RED, arrowprops=dict(arrowstyle="->", color=C_RED, lw=1.2))
# CAGR 标注
cagr = (market[-1] / market[0]) ** (1 / 6) - 1
ax.text(0.03, 0.95, f"2025–2031 CAGR ≈ {cagr*100:.1f}%", transform=ax.transAxes,
        fontsize=11, fontweight="bold", color=C_GREEN,
        bbox=dict(boxstyle="round,pad=0.35", fc="#ECFDF5", ec=C_GREEN))
ax.set_title("图1  Agentic AI 全球市场规模预测（单位：十亿美元）", fontsize=13, pad=12)
ax.set_ylabel("市场规模（十亿美元）", fontsize=10.5)
ax.grid(axis="y", ls="--", alpha=0.35, zorder=0)
ax.set_axisbelow(True)
ax.legend(loc="upper left", fontsize=9)
ax.text(0.0, -0.16, "数据来源：MarketsandMarkets 预测（2031 年 $57.42B$）、Tavily 联网检索（2026 年区间 $9.14\\sim10.86B$）；2027–2030 为基于公开 CAGR 的插值。",
        transform=ax.transAxes, fontsize=7.5, color=C_GRAY)
plt.tight_layout()
plt.savefig("chart_agentic_market.png", bbox_inches="tight")
plt.close()
print("saved chart_agentic_market.png")

# ============ 图2: 2026 企业 AI Agent 采用现状 ============
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.5, 4.8), dpi=130,
                               gridspec_kw={"width_ratios": [1, 1.35]})

# 左: 采用阶段 donut
stages = ["生产环境运行", "试点 / POC", "规划 / 评估"]
vals = [51, 29, 20]
colors = [C_GREEN, C_ORANGE, C_GRAY]
wedges, _ = ax1.pie(vals, colors=colors, startangle=90, counterclock=False,
                    wedgeprops=dict(width=0.38, edgecolor="white"))
ax1.text(0, 0.06, "51%", ha="center", fontsize=22, fontweight="bold", color=C_GREEN)
ax1.text(0, -0.22, "生产运行", ha="center", fontsize=11, color="#334155")
ax1.legend(wedges, [f"{s} {v}%" for s, v in zip(stages, vals)],
           loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8.5, frameon=False)
ax1.set_title("(a) 采用阶段分布（示意）", fontsize=11.5)

# 右: 关键指标条形
metrics = ["已部署 AI 应用\n的企业 (2026)", "生产环境运行\nAgent 的企业", "2027 年底前将被取消\n的 Agent 项目", "2028 年由 AI 自主决策\n的日常工作占比"]
mvals = [80, 51, 40, 15]
mcolors = [C_BLUE, C_GREEN, C_RED, C_CYAN]
ypos = np.arange(len(metrics))[::-1]
ax2.barh(ypos, mvals, color=mcolors, height=0.55, zorder=3)
for y, v in zip(ypos, mvals):
    ax2.text(v + 1.5, y, f"{v}%", va="center", fontsize=9.5, fontweight="bold", color="#1E293B")
ax2.set_yticks(ypos)
ax2.set_yticklabels(metrics, fontsize=8.5)
ax2.set_xlim(0, 100)
ax2.set_xlabel("%", fontsize=9)
ax2.grid(axis="x", ls="--", alpha=0.35, zorder=0)
ax2.set_axisbelow(True)
ax2.set_title("(b) 关键指标（Gartner / 行业调研）", fontsize=11.5)

fig.suptitle("图2  2026 年企业 AI Agent 采用现状与关键指标", fontsize=13, y=1.0)
plt.tight_layout()
plt.savefig("chart_agent_adoption.png", bbox_inches="tight")
plt.close()
print("saved chart_agent_adoption.png")

# ============ 图3: 企业级 AI Agent 参考架构分层图 ============
fig, ax = plt.subplots(figsize=(11, 7.2), dpi=130)
ax.set_xlim(0, 10); ax.set_ylim(0, 10); ax.axis("off")

layers = [
    ("L5  应用层", "智能客服 / 研发助手 / 营销 Agent / 运维 Agent / 数据分析 Agent", C_BLUE),
    ("L4  Agent 编排层", "任务规划 · 反思 · 工具调用 · 多智能体协作 · 人机协同(HITL)", C_CYAN),
    ("L3  模型网关层", "多模型路由 · 统一 API · 成本控制 · 模型评测 · 安全护栏", C_ORANGE),
    ("L2  知识层", "企业知识库(RAG) · 向量数据库(Milvus) · 知识图谱 · 语义缓存", C_GREEN),
    ("L1  基础设施层", "算力 / GPU · 数据平台 · 可观测性(OTel) · 鉴权与审计 · 配置与发布", C_GRAY),
]
y = 9.0
box_h = 1.15
gap = 0.22
for i, (title, desc, color) in enumerate(layers):
    yy = y - i * (box_h + gap)
    ax.add_patch(plt.Rectangle((0.35, yy), 9.3, box_h, facecolor=color, alpha=0.16,
                               edgecolor=color, lw=1.8, zorder=2))
    ax.text(0.6, yy + box_h * 0.62, title, fontsize=12.5, fontweight="bold", color=color, zorder=3)
    ax.text(0.6, yy + box_h * 0.25, desc, fontsize=9.5, color="#334155", zorder=3)

# 层间箭头
for i in range(len(layers) - 1):
    y_top = y - i * (box_h + gap) - gap * 0.35
    ax.annotate("", xy=(5.0, y_top - 0.18), xytext=(5.0, y_top + 0.18),
                arrowprops=dict(arrowstyle="<->", color="#64748B", lw=1.1))

# 侧边标注: 横向能力
ax.text(9.75, 7.6, "横 向\n能 力", ha="center", fontsize=10, fontweight="bold", color="#475569", rotation=0)
ax.text(9.78, 6.9, "安全与合规\n可观测性\n评测与治理\nDevOps", ha="center", fontsize=8, color="#64748B")
ax.add_patch(plt.Rectangle((9.5, 5.1), 0.5, 3.4, facecolor="none", edgecolor="#64748B", lw=1.0, ls="--"))

ax.set_title("图3  企业级 AI Agent 系统参考架构（分层设计）", fontsize=14, pad=10)
plt.tight_layout()
plt.savefig("chart_agent_architecture.png", bbox_inches="tight")
plt.close()
print("saved chart_agent_architecture.png")
