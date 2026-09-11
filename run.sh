#!/bin/zsh
# Auto Flow 启动脚本：首次自动建 venv 并按 requirements.txt 装依赖
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[auto-flow] 创建虚拟环境…"
  python3.12 -m venv .venv
  ./.venv/bin/pip install --upgrade pip -q
fi

# 依赖检查用 importlib.util.find_spec（不真正 import）：
# 视觉/推理那几个包 import 起来要 1 秒以上，每次启动都付这个代价不值当。
# 检查范围与 requirements.txt 对齐，缺哪个就报哪个——此前只查 3 个包，
# 缺 opencv/onnxruntime 时会一路启动到真用到那个节点时才报错。
MISSING="$(./.venv/bin/python - <<'EOF'
import importlib.util as u

MODULES = [
    "PySide6", "pynput", "numpy", "cv2", "mss", "onnxruntime",
    "ApplicationServices", "Quartz", "AppKit", "Foundation", "Vision",
]
print(" ".join(m for m in MODULES if u.find_spec(m) is None))
EOF
)" || MISSING="__check_failed__"

if [ -n "$MISSING" ]; then
  if [ "$MISSING" = "__check_failed__" ]; then
    echo "[auto-flow] 依赖检查失败，按 requirements.txt 重新安装…"
  else
    echo "[auto-flow] 缺少依赖：$MISSING"
  fi
  ./.venv/bin/pip install -q -r requirements.txt
fi

exec ./.venv/bin/python main.py "$@"
