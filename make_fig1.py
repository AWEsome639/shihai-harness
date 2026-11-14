"""画 PLE harness 的架构图（Writeup 必交图 1/3）。

图上只画**真实执行路径**，对应 serve.py 与 taskbridge.py 的实际结构：
  Evaluator ──WS──> serve.py(harness) ──egress──> 模型端点
                          │
                          └──协议 4 原语──> 任务容器（无网）
                                              └─ MCP servers
不画任何没实现的东西。
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

plt.rcParams["font.family"] = ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

W, H = 16, 11.2
fig, ax = plt.subplots(figsize=(W, H))
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.axis("off")
fig.patch.set_facecolor("white")

INK = "#1a1a1a"
GREY = "#8a8a8a"
BLUE = "#2c5d8f"
ORANGE = "#c1551f"
GREEN = "#2f7d4f"
PURPLE = "#6b4a8f"


def box(x, y, w, h, label, sub="", color=INK, fill="#ffffff", lw=1.6, fs=11):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.12",
                                linewidth=lw, edgecolor=color, facecolor=fill, zorder=2))
    if sub:
        ax.text(x + w / 2, y + h * 0.68, label, ha="center", va="center",
                fontsize=fs, color=color, weight="bold", zorder=3)
        ax.text(x + w / 2, y + h * 0.30, sub, ha="center", va="center",
                fontsize=fs - 3, color=GREY, zorder=3)
    else:
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center",
                fontsize=fs, color=color, weight="bold", zorder=3)


def arrow(x1, y1, x2, y2, text="", color=INK, style="-|>", ls="-", off=0.0, fs=9):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                 mutation_scale=18, linewidth=1.5,
                                 color=color, linestyle=ls, zorder=4,
                                 shrinkA=2, shrinkB=2))
    if text:
        ax.text((x1 + x2) / 2 + off, (y1 + y2) / 2 + 0.16, text,
                ha="center", va="bottom", fontsize=fs, color=color, zorder=5)


# ── 标题 ──────────────────────────────────────────────────────────────
ax.text(W / 2, 10.7, "PLE harness architecture", ha="center", va="center",
        fontsize=18, weight="bold", color=INK)
ax.text(W / 2, 10.3, "serve.py is a WebSocket server; it never touches the task filesystem directly",
        ha="center", va="center", fontsize=10.5, color=GREY, style="italic")

# ── 左：Evaluator ─────────────────────────────────────────────────────
box(0.4, 5.2, 2.9, 2.4, "Evaluator", "Kaggle evaluation server\nsends the task, replies to\nour 4 request types", BLUE, "#eef4fa")

# ── 中：harness ───────────────────────────────────────────────────────
box(4.6, 3.9, 4.6, 4.2, "", "", INK, "#fbfbfb", lw=2.0)
ax.text(6.9, 7.65, "serve.py  (harness container)", ha="center", va="center",
        fontsize=12.5, color=INK, weight="bold")
ax.text(6.9, 7.25, "2 CPU · 4 GB · read-only root · /tmp & $HOME 1 GB",
        ha="center", va="center", fontsize=8.2, color=GREY)

box(4.95, 5.95, 3.9, 1.0, "taskbridge.Relay", "one WS session per task · cancel → 10 s to done", GREEN, "#eef7f1", fs=10)
box(4.95, 4.65, 3.9, 1.0, "Agent.run()  state machine", "reason → act → observe, until the budget closes", ORANGE, "#fdf1ea", fs=10)
box(4.95, 4.10, 3.9, 0.42, "context manager: compact / cut_unread / learn_context", "", GREY, "#f4f4f4", lw=1.0, fs=7.6)

# ── 右：模型端点 ──────────────────────────────────────────────────────
box(11.3, 5.8, 4.2, 1.9, "Model endpoint", "google-cloud/<id> via egress proxy\nonly this host is reachable", PURPLE, "#f4effa")

# ── 下：任务容器 ──────────────────────────────────────────────────────
box(4.6, 0.35, 6.4, 2.5, "", "", INK, "#fbfbfb", lw=2.0)
ax.text(7.8, 2.52, "Task container  —  no network at all", ha="center", va="center",
        fontsize=12.5, color=INK, weight="bold")
box(4.95, 1.55, 2.6, 0.8, "project files", "drawings · specs · xlsx", GREY, "#f4f4f4", fs=9)
box(7.85, 1.55, 2.9, 0.8, "the deliverable", "exact path from the prompt", GREEN, "#eef7f1", fs=9)
box(4.95, 0.65, 5.8, 0.7, "MCP servers (12 of 29 tasks)", "Streamable HTTP, reached only through the http primitive", PURPLE, "#f4effa", fs=9)

# ── 连线 ──────────────────────────────────────────────────────────────
arrow(3.3, 6.95, 4.9, 6.55, "", BLUE)
ax.text(2.55, 7.62, "task", ha="right", va="bottom", fontsize=8.8, color=BLUE, zorder=5)
ax.text(2.55, 7.36, "instruction · limits", ha="right", va="bottom", fontsize=7.2, color=BLUE, zorder=5)
arrow(4.9, 6.15, 3.3, 6.35, "", BLUE)

arrow(8.55, 6.45, 11.3, 6.75, "chat + tool_calls", PURPLE)
arrow(11.3, 6.35, 8.55, 6.05, "streamed tokens", PURPLE, off=-0.1)

arrow(6.9, 4.65, 6.9, 3.35, "", ORANGE)
ax.text(6.78, 3.72, "4 primitives", ha="right", va="center", fontsize=8.4, color=ORANGE, zorder=5)

# ── 右下角：一次任务的时间轴 ──────────────────────────────────────────
ax.text(13.25, 3.30, "one task, 6 h budget", ha="center", va="center",
        fontsize=10, color=INK, weight="bold")
ax.add_patch(Rectangle((11.35, 2.35), 4.0, 0.55, facecolor="#eeeeee", edgecolor=GREY, lw=1.0, zorder=2))
ax.add_patch(Rectangle((11.35, 2.35), 3.00, 0.55, facecolor="#fdf1ea", edgecolor=ORANGE, lw=1.2, zorder=3))
ax.add_patch(Rectangle((14.35, 2.35), 1.00, 0.55, facecolor="#eef7f1", edgecolor=GREEN, lw=1.2, zorder=3))
ax.text(12.83, 2.62, "work (75%)", ha="center", va="center", fontsize=8.4, color=ORANGE, zorder=4)
ax.text(14.85, 2.62, "prompt\nself-check", ha="center", va="center", fontsize=6.6, color=GREEN, zorder=4, linespacing=0.9)
ax.text(13.35, 1.98, "an early deliverable is written as soon as a",
        ha="center", va="center", fontsize=7.8, color=GREY)
ax.text(13.35, 1.73, "plausible shape exists, then refined in place",
        ha="center", va="center", fontsize=7.8, color=GREY)

# ── 脚注 ──────────────────────────────────────────────────────────────
ax.text(0.4, 0.06,
        "Primitives: exec (bash -c, 600 s cap) · read_file / write_file (base64, 10 MB) · http (task MCP only).  "
        "Files already on disk are graded even if the harness later fails.",
        ha="left", va="center", fontsize=8.2, color=GREY)

plt.tight_layout()
out = "/Users/a0000/.dsh/work/ple/fig1-architecture.png"
plt.savefig(out, dpi=200, bbox_inches="tight", facecolor="white")
print("已写出:", out)
