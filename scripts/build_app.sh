#!/bin/zsh
# 打包 Auto Flow.app：图标 + MacDev 签名（保持 TCC 授权跨构建有效）+ 可选 DMG
set -e
cd "$(dirname "$0")/.."

./.venv/bin/python -m PyInstaller --noconfirm --windowed --name "Auto Flow" \
  --icon assets/icon.icns \
  --osx-bundle-identifier com.catlair.autoflow \
  --collect-all pynput \
  --hidden-import ApplicationServices \
  --hidden-import AppKit \
  --hidden-import Foundation \
  --hidden-import CoreFoundation \
  --hidden-import Vision \
  --hidden-import Quartz \
  --add-data "workflows:workflows" \
  --add-data "models:models" \
  main.py

APP="dist/Auto Flow.app"

# 自签证书签名：稳定签名身份 → 辅助功能/输入监控授权不因重新构建失效
if security find-identity -v -p codesigning | grep -q "MacDev"; then
  codesign --force --deep --sign "MacDev" "$APP"
  echo "已用 MacDev 签名"
else
  echo "警告：未找到 MacDev 证书，使用 ad-hoc 签名（重新构建后需重新授权）"
  codesign --force --deep --sign - "$APP"
fi

# DMG（--dmg 时生成）
if [ "$1" = "--dmg" ]; then
  rm -f "dist/Auto Flow.dmg"
  hdiutil create -volname "Auto Flow" -srcfolder "$APP" -ov -format UDZO "dist/Auto Flow.dmg"
  echo "完成: dist/Auto Flow.dmg"
fi
echo "完成: $APP"
