#!/bin/zsh
# 组装 Auto Flow RPC Spike.app —— P0-S 授权持久性验证用的独立 .app 包。
#
# 设计（对照 docs/迁移计划书 §12 的「Tauri 把 onedir sidecar 放进
# .app/Contents/Resources/ 固定路径 spawn」生产形态）：
# - 把 PyInstaller onedir 产物整体拷进 .app/Contents/Resources/sidecar/，
#   sidecar 可执行文件与它的 _internal 保持同级（onedir 原生布局）。
# - CFBundleExecutable 软链到该 onedir 可执行文件。由于可执行文件位于
#   Contents/MacOS 之外，PyInstaller bootloader 走「onedir 模式」，直接找
#   同级的 _internal，无需把 libpython / base_library.zip / PyObjC 绑定
#   塞进 Contents/Frameworks（那套是「sidecar 当 CFBundleExecutable 放在
#   MacOS 里」时才会触发的 .app 模式，更脆弱）。
# - 固定路径 + MacDev 自签 → 三项 TCC 授权跨重新构建保持稳定。
#
# 用法：先 ./scripts/build_sidecar.sh，再 ./scripts/build_spike_app.sh
# 产物：dist/Auto Flow RPC Spike.app
set -e
cd "$(dirname "$0")/.."

# 确保 onedir 已构建
if [ ! -x dist/autoflow-sidecar/autoflow-sidecar ]; then
  echo "未找到 onedir 产物，先运行 scripts/build_sidecar.sh"
  ./scripts/build_sidecar.sh
fi

APP="dist/Auto Flow RPC Spike.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/sidecar"

# Info.plist
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Auto Flow RPC Spike</string>
  <key>CFBundleIdentifier</key><string>com.example.autoflow.rpc-spike</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>autoflow-sidecar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

# onedir 整体拷进 Resources/sidecar（可执行 + _internal 同级）
cp -R dist/autoflow-sidecar/. "$APP/Contents/Resources/sidecar/"

# CFBundleExecutable 必须是常规文件（codesign --deep 拒绝 symlink）。
# 复制一份可执行到 MacOS/，与 Resources/sidecar 内容一致 → 同一 TCC 签名身份。
cp dist/autoflow-sidecar/autoflow-sidecar "$APP/Contents/MacOS/autoflow-sidecar"

# 签名（MacDev，保持 TCC 授权跨构建稳定）
if security find-identity -v -p codesigning | grep -q "MacDev"; then
  codesign --force --deep --sign "MacDev" "$APP"
  echo "已用 MacDev 签名 spike"
else
  codesign --force --deep --sign - "$APP"
  echo "警告：未找到 MacDev 证书，使用 ad-hoc 签名（重新构建后需重新授权）"
fi

# 清 quarantine（本地构建无下载来源，但仍清一遍以防万一）
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "完成: $APP"
echo "运行: $APP/Contents/Resources/sidecar/autoflow-sidecar"
