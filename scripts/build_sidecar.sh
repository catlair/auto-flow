#!/bin/zsh
# 打包 Auto Flow 的 Python 后端 sidecar（onedir，非 .app）。
#
# 设计（对照 docs/迁移计划书 §12）：
# - 必须 onedir + 固定路径，绝不能 --onefile（onefile 每次解压到随机 _MEI 目录会让
#   TCC 授权失效）。产物是 dist/autoflow-sidecar/autoflow-sidecar 可执行文件，
#   由 Tauri 侧放进 .app/Contents/Resources/ 固定路径后 spawn。
# - 不 --windowed（sidecar 是 CLI 进程，经 stdin/stdout 与 Tauri 通信）。
# - 签名用 MacDev，保持授权跨构建稳定（与现存 .app 打包同一策略）。
#
# 注意：本脚本只收集「当前 rpc/server.py 可达」的依赖。P1 接入视觉节点
# （vision/ocr/yolo → opencv/onnxruntime）后体积会显著增大，届时体积基线需重测。
set -e
cd "$(dirname "$0")/.."

rm -rf dist/autoflow-sidecar

./.venv/bin/python -m PyInstaller --noconfirm --onedir --name autoflow-sidecar \
  --hidden-import ApplicationServices \
  --hidden-import AppKit \
  --hidden-import Foundation \
  --hidden-import CoreFoundation \
  --hidden-import Quartz \
  rpc/server.py

SIDECAR="dist/autoflow-sidecar/autoflow-sidecar"

if security find-identity -v -p codesigning | grep -q "MacDev"; then
  codesign --force --deep --sign "MacDev" "$SIDECAR"
  echo "已用 MacDev 签名 sidecar"
else
  echo "警告：未找到 MacDev 证书，使用 ad-hoc 签名（重新构建后需重新授权）"
  codesign --force --deep --sign - "$SIDECAR"
fi

echo "=== 体积基线 ==="
du -sh dist/autoflow-sidecar
echo "可执行: $SIDECAR"
