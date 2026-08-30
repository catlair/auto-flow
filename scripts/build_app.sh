#!/bin/zsh
# 打包 Auto Flow.app（pyinstaller onedir）
set -e
cd "$(dirname "$0")/.."
./.venv/bin/python -m PyInstaller --noconfirm --windowed --name "Auto Flow" \
  --osx-bundle-identifier com.catlair.autoflow \
  --collect-all pynput \
  --hidden-import ApplicationServices \
  --hidden-import AppKit \
  --hidden-import Foundation \
  --hidden-import CoreFoundation \
  --add-data "workflows:workflows" \
  main.py
echo "完成: dist/Auto Flow.app"
