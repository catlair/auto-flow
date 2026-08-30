#!/bin/zsh
# 安装/更新到 /Applications（固定路径，辅助功能授权稳定不失效）
set -e
cd "$(dirname "$0")/.."

APP_SRC="dist/Auto Flow.app"
APP_DST="/Applications/Auto Flow.app"

if [ ! -d "$APP_SRC" ]; then
  echo "先构建: ./scripts/build_app.sh"
  exit 1
fi

pkill -f "Auto Flow.app/Contents/MacOS" 2>/dev/null || true
sleep 1
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true
echo "已安装: $APP_DST"
open "$APP_DST"
