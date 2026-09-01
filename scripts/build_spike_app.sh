#!/bin/zsh
# 组装 Auto Flow RPC Spike.app —— P0-S 授权持久性验证用的独立 .app 包。
#
# 为什么用「Frameworks 模式」而不是把 onedir 塞进 Contents/Resources：
# 系统设置里手动给「屏幕录制 / 辅助功能 / 输入监控」授权时，`+` 只能选 .app 包本身，
# TCC 授权的是 .app 的主可执行文件（Contents/MacOS/CFBundleExecutable）。为了让「被授权的
# 二进制」与「实际运行的二进制」完全一致，这里让 Contents/MacOS/autoflow-sidecar 就是
# 真正运行的 sidecar，并补齐 PyInstaller .app 模式所需的 Frameworks 布局：
#   - Contents/Frameworks/libpython3.12.dylib
#   - Contents/Frameworks/base_library.zip -> ../Resources/base_library.zip（软链）
#   - Contents/Frameworks/{AppKit,CoreFoundation,CoreText,Foundation,HIServices,objc,Quartz,ApplicationServices}
#   - Contents/Resources/base_library.zip（真实 stdlib）
# 这与正式 Auto Flow.app（scripts/build_app.sh 产物）的布局一致。
#
# 固定路径 + MacDev 自签 → 三项 TCC 授权跨重新构建保持稳定（§12.1 待验证）。
#
# 用法：先 ./scripts/build_sidecar.sh，再 ./scripts/build_spike_app.sh
# 产物：dist/Auto Flow RPC Spike.app
set -e
cd "$(dirname "$0")/.."

# 总是重新构建 onedir（绝不复用已存在的产物）。
# 注意：编辑 Python 源码会改变嵌入的 PYZ，进而改变可执行文件 CDHash；
# 若这里「存在即跳过」，会把旧源码的旧 CDHash 二进制装进 .app，导致 TCC 授权仍指向旧
# CDHash 而排查无果。确定性 CDHash 只对「同一份源码」成立，因此必须从源码重建。
./scripts/build_sidecar.sh

APP="dist/Auto Flow RPC Spike.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Frameworks" "$APP/Contents/Resources"

# Info.plist（CFBundleExecutable = autoflow-sidecar，位于 Contents/MacOS）
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

# 主可执行文件（即 CFBundleExecutable，也是被 TCC 授权的二进制）
cp dist/autoflow-sidecar/autoflow-sidecar "$APP/Contents/MacOS/"

# .app 模式所需：libpython + PyObjC 绑定进 Frameworks
cp dist/autoflow-sidecar/_internal/libpython3.12.dylib "$APP/Contents/Frameworks/"
for f in AppKit CoreFoundation CoreText Foundation HIServices objc Quartz ApplicationServices; do
  if [ -e "dist/autoflow-sidecar/_internal/$f" ]; then
    cp -R "dist/autoflow-sidecar/_internal/$f" "$APP/Contents/Frameworks/"
  fi
done

# stdlib：真实文件在 Resources，Frameworks 用软链指向它（与正式 .app 一致）
cp dist/autoflow-sidecar/_internal/base_library.zip "$APP/Contents/Resources/"
ln -sf ../Resources/base_library.zip "$APP/Contents/Frameworks/base_library.zip"

# 签名（MacDev，保持 TCC 授权跨构建稳定）
if security find-identity -v -p codesigning | grep -q "MacDev"; then
  codesign --force --deep --sign "MacDev" "$APP"
  echo "已用 MacDev 签名 spike"
else
  codesign --force --deep --sign - "$APP"
  echo "警告：未找到 MacDev 证书，使用 ad-hoc 签名（重新构建后需重新授权）"
fi

# 清 quarantine
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "完成: $APP"
echo "运行: $APP/Contents/MacOS/autoflow-sidecar"
