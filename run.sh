#!/bin/zsh
# Auto Flow 启动脚本：首次自动建 venv 装依赖
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[auto-flow] 创建虚拟环境…"
  /Users/catlair/.local/bin/python3.12 -m venv .venv || python3.12 -m venv .venv
  ./.venv/bin/pip install --upgrade pip -q
fi

./.venv/bin/python - <<'EOF' 2>/dev/null || NEED_INSTALL=1
import PySide6, pynput, ApplicationServices
EOF
if [ "$NEED_INSTALL" = "1" ]; then
  echo "[auto-flow] 安装依赖…"
  ./.venv/bin/pip install -q PySide6 pynput pyobjc-framework-ApplicationServices
fi

exec ./.venv/bin/python main.py "$@"
