#!/bin/zsh
# 部署 Tauri 构建出的 Auto Flow.app 到 /Applications（固定路径，TCC 授权稳定不失效）。
# 对应 docs §12 / P3：先 MacDev 双签（+ 可选 notarization），再拷贝、去 quarantine。
#
# 用法：
#   ./scripts/sync_app.sh                 # 签名(若未签) + 安装到 /Applications 并打开
#   APPLE_ID=... APPLE_APP_PASSWORD=... APPLE_TEAM_ID=... ./scripts/sync_app.sh   # 含公证
set -e
cd "$(dirname "$0")/.."

APP_SRC="tauri/src-tauri/target/release/bundle/macos/Auto Flow.app"
APP_DST="/Applications/Auto Flow.app"

if [ ! -d "$APP_SRC" ]; then
  echo "先构建并签名："
  echo "  cd tauri && npm run tauri build"
  echo "  ./scripts/sign_tauri_app.sh"
  exit 1
fi

# 1) 保证已签名（MacDev 双签；提供 Apple 凭证时顺带公证）
if ! codesign --verify --deep --verbose=0 "$APP_SRC" >/dev/null 2>&1; then
  echo "未签名，执行 sign_tauri_app.sh ..."
  ./scripts/sign_tauri_app.sh
fi

# 2) 停掉旧实例并安装
pkill -f "Auto Flow.app/Contents/MacOS" 2>/dev/null || true
sleep 1
# 旧 .app 移入废纸篓而非 rm -rf：整包删除不可恢复，一旦路径写错就是灾难；
# 移废纸篓既可反悔，也避免触发批量删除确认。
if [ -d "$APP_DST" ]; then
  TRASH_DST="$HOME/.Trash/Auto Flow $(date +%Y%m%d-%H%M%S).app"
  mv "$APP_DST" "$TRASH_DST"
  echo "旧版本已移入废纸篓: $TRASH_DST"
fi
cp -R "$APP_SRC" "$APP_DST"
# 去掉 quarantine 标记，避免未公证时首次打开被 Gatekeeper 拦（已公证则自动放行）
xattr -dr com.apple.quarantine "$APP_DST" 2>/dev/null || true
echo "已安装: $APP_DST"
open "$APP_DST"
