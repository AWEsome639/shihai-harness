#!/usr/bin/env bash
# 跑 PLE dev 集的驱动脚本。
#
# 用法：
#   ./run_dev.sh                  # 跑全部 29 题
#   ./run_dev.sh data-hall-mep-routing   # 只跑一题
#   ./run_dev.sh --list           # 列出可选任务
#
# 设计意图：等模型 key 到位后，一条命令就能出 dev 分数与 artifacts/。
# eval/run.py 自己会写出 <entry>/artifacts/，这里只负责调用与汇总。
set -uo pipefail

PLE="$HOME/.dsh/work/ple/ple"
ENTRY="$HOME/.dsh/work/ple/my-harness"
LOG_DIR="$HOME/.dsh/work/ple/runs"
mkdir -p "$LOG_DIR"

if [ "${1:-}" = "--list" ]; then
  echo "可选的 dev 任务："
  uv run --directory "$PLE" python - <<'PY' 2>/dev/null || \
    grep -oE '^\s*- id: .*' "$PLE/eval/dev_set.yaml" 2>/dev/null | sed 's/.*id: //' | sed 's/^/  /'
import yaml, pathlib
p = pathlib.Path.home() / ".dsh/work/ple/ple/eval/dev_set.yaml"
d = yaml.safe_load(p.read_text())
for t in d.get("tasks", []):
    print(" ", t.get("id"))
PY
  exit 0
fi

# 前置检查：key 必须在
if [ ! -s "$ENTRY/.env" ]; then
  echo "!! $ENTRY/.env 不存在或为空 —— 先写 MODEL_API_KEY"
  exit 1
fi
if ! grep -q "^MODEL_API_KEY=..*" "$ENTRY/.env"; then
  echo "!! .env 里没有 MODEL_API_KEY"
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
LOG="$LOG_DIR/dev-$STAMP.log"
echo "日志：$LOG"

if [ $# -ge 1 ]; then
  ARGS=(--tasks "$1")
  echo "只跑：$1"
else
  ARGS=()
  echo "跑全部 dev 任务（29 题 × 6 小时上限，会很慢）"
fi

cd "$PLE" || exit 1
# 导出 egress 需要的代理；eval/run.py 自己在 docker 里跑，宿主侧只负责模型端点连通
uv run python eval/run.py "$ENTRY" "${ARGS[@]}" 2>&1 | tee "$LOG"
RC=${PIPESTATUS[0]}

echo
echo "════ 结果汇总 ════"
echo "退出码：$RC"
if [ -f "$ENTRY/artifacts/manifest.json" ]; then
  python3 - <<PY
import json, pathlib
m = json.loads(pathlib.Path("$ENTRY/artifacts/manifest.json").read_text())
print("manifest 键：", list(m.keys())[:10])
for k in ("run_id", "created_at", "dev_set", "harness"):
    if k in m:
        v = m[k]
        print(f"  {k}: {str(v)[:120]}")
PY
else
  echo "!! 没有 artifacts/manifest.json —— 跑失败了或还没写出"
fi
echo "下一步：cd $PLE && uv run python -m kit artifacts $ENTRY"
