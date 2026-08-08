# -*- coding: utf-8 -*-
"""生成《企业级 AI Agent 系统架构设计》行研报告图表（3 张）"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np
import os

plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'Heiti TC', 'STHeiti', 'Hiragino Sans GB']
plt.rcParams['axes.unicode_minus'] = False

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
os.makedirs(OUT, exist_ok=True)

C1, C2, C3 = '#1f77b4', '#ff7f0e', '#2ca02c'

# ============ 图 1：全球 AI Agent 市场规模增长趋势（折线图） ============
# 数据来源：Research and Markets《AI Agents Market Report 2026》(2026-02)
# 2026 年 $12.06B → 2030 年 $53.2B，CAGR 44.9%（中间年份按 CAGR 推算）
years = [2025, 2026, 2027, 2028, 2029, 2030]
size  = [8.32, 12.06, 17.47, 25.31, 36.67, 53.13]

fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(years, size, 'o-', color=C1, linewidth=2.5, markersize=9, markerfacecolor='white', markeredgewidth=2.5)
ax.fill_between(years, size, 0, alpha=0.08, color=C1)
for x, v in zip(years, size):
    ax.annotate(f'${v:.1f}B', (x, v), textcoords='offset points', xytext=(0, 12), ha='center',
                fontsize=11, fontweight='bold', color=C1)
ax.set_title('全球 AI Agent 市场规模增长趋势（2025–2030）', fontsize=16, pad=15)
ax.set_xlabel('年份', fontsize=12)
ax.set_ylabel('市场规模（十亿美元 USD B）', fontsize=12)
ax.set_xticks(years)
ax.grid(True, alpha=0.3, linestyle='--')
ax.text(0.5, -0.16, '数据来源：Research and Markets《AI Agents Market Report 2026》；2025 年按 CAGR 44.9% 反推，2027–2029 为推算值',
        transform=ax.transAxes, ha='center', fontsize=9, color='#666666')
plt.tight_layout()
p1 = os.path.join(OUT, 'chart_market.png')
plt.savefig(p1, dpi=150)
plt.close()
print('图1已保存:', p1)

# ============ 图 2：企业 AI Agent 落地进程分层（水平漏斗柱状图） ============
# 数据来源：麦肯锡 2026 专项洞察（引用自腾讯/QQ 新闻 2026-01）
labels = ['已部署 AI Agent\n（覆盖客服/营销/运营等）', '已启动 Agent 试验', '单一业务职能内规模化', '全公司级规模化应用']
values = [70, 39, 23, 7]
colors = ['#1f77b4', '#5b9bd5', '#8fb8de', '#c3d6ec']

fig, ax = plt.subplots(figsize=(10, 5.6))
ypos = np.arange(len(labels))[::-1]
bars = ax.barh(ypos, values, color=colors, edgecolor='white', height=0.62)
for y, v in zip(ypos, values):
    ax.text(v + 1.2, y, f'{v}%', va='center', fontsize=13, fontweight='bold', color='#333333')
ax.set_yticks(ypos)
ax.set_yticklabels(labels, fontsize=11)
ax.set_xlim(0, 82)
ax.set_xlabel('企业占比（%）', fontsize=12)
ax.set_title('企业 AI Agent 落地进程：从部署到规模化（麦肯锡，2026）', fontsize=15, pad=12)
ax.grid(True, axis='x', alpha=0.3, linestyle='--')
ax.text(0.5, -0.14, '注：各层级为递进关系，即“已部署”包含“试验”，“试验”包含“规模化”等子集；全公司级占比不足 7%',
        transform=ax.transAxes, ha='center', fontsize=9, color='#666666')
plt.tight_layout()
p2 = os.path.join(OUT, 'chart_adoption.png')
plt.savefig(p2, dpi=150)
plt.close()
print('图2已保存:', p2)

# ============ 图 3：企业级 AI Agent 参考架构图（分层框图） ============
fig, ax = plt.subplots(figsize=(11, 8.2))
ax.set_xlim(0, 10)
ax.set_ylim(0, 11.2)
ax.axis('off')

def layer(y, h, title, color, items, textcolor='white'):
    box = FancyBboxPatch((0.55, y), 8.9, h, boxstyle='round,pad=0.04',
                         linewidth=1.6, edgecolor=color, facecolor=color, alpha=0.92)
    ax.add_patch(box)
    ax.text(0.9, y + h - 0.28, title, fontsize=12.5, fontweight='bold', color=textcolor, va='top')
    ax.text(5.0, y + h/2 - 0.18, items, fontsize=10.5, color=textcolor, ha='center', va='center')

layer(9.30, 1.30, '① 交互层（入口）', '#1f77b4',
      'Web / 移动 App / IM 会话 / 语音客服 / 企业门户')
layer(7.35, 1.30, '② 编排层（Orchestration）', '#ff7f0e',
      '任务规划 / 拆解 / 调度  ·  Supervisor / Manager Agent  ·  上下文共享策略')
layer(5.40, 1.30, '③ 智能体层（行业/职能 Agent）', '#2ca02c',
      '客服 Agent · 投研 Agent · 风控 Agent · 合规审查 Agent · 数据分析 Agent…')
layer(3.45, 1.30, '④ 工具与集成层（Tools & Integration）', '#9467bd',
      'MCP / A2A 协议  ·  API Gateway  ·  RAG 知识库  ·  ERP/CRM/HIS 等存量系统')
layer(1.50, 1.30, '⑤ 基础设施层（Foundation）', '#d62728',
      'LLM 模型服务（本地化/云）  ·  记忆与 KV Cache  ·  评估体系')

# 层间箭头
for yy in [9.22, 7.27, 5.32, 3.37]:
    ax.annotate('', xy=(5.0, yy), xytext=(5.0, yy + 1.28),
                arrowprops=dict(arrowstyle='<->', color='#888888', lw=1.4))

# 右侧治理纵条
box = FancyBboxPatch((9.7, 1.5), 0.30, 9.10, boxstyle='round,pad=0.02',
                     linewidth=0, facecolor='#333333', alpha=0.12)
ax.add_patch(box)
ax.text(9.85, 6.05, 'AgentOps\n监控·审计·权限\n安全治理·Guardrails',
        fontsize=9.5, ha='center', va='center', color='#333333', rotation=90)

ax.text(5.0, 0.55, '参考架构综合自：知识库《深入理解 AI Agent》（李博杰）与 2026 年行业趋势（MCP/A2A、AgentOps、多 Agent 编排）',
        ha='center', fontsize=9, color='#666666')
ax.set_title('企业级 AI Agent 系统参考架构（2026）', fontsize=16, pad=10)
plt.tight_layout()
p3 = os.path.join(OUT, 'chart_architecture.png')
plt.savefig(p3, dpi=150)
plt.close()
print('图3已保存:', p3)

print('全部图表生成完成 ->', OUT)
