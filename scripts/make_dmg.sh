#!/bin/zsh
# 从【已签名】的 .app 制作可分发的 dmg（对应 docs §12 / P3）。
#
# 为什么不用 Tauri 内置 dmg：
#   ① 内置 dmg 在【签名之前】打包——产出的 dmg 里是未签名的 .app，而我们的签名
#      （MacDev 双签 + 强化运行时 + 库校验豁免）是 tauri build 之后的外部步骤，顺序对不上；
#   ② 实测内置 dmg 会失败：bundle/dmg/support/ 目录缺失（内含 template.applescript 与
#      eula-resources-template.xml），bundle_dmg.sh 第 336 行直接报错退出。
# 故 tauri.conf.json 的 bundle.targets 只保留 "app"，dmg 由本脚本在签名之后自建。
#
# 用法：
#   bash scripts/make_dmg.sh          # .app 未签名时会自动先签名
set -e
cd "$(dirname "$0")/.."

APP="tauri/src-tauri/target/release/bundle/macos/Auto Flow.app"
if [ ! -d "$APP" ]; then
  echo "先构建：cd tauri && npm run tauri build"
  exit 1
fi

# dmg 里必须是签名后的 app，否则分发件等于白签
if ! codesign --verify --deep --verbose=0 "$APP" >/dev/null 2>&1; then
  echo "未签名，先执行 sign_tauri_app.sh ..."
  ./scripts/sign_tauri_app.sh
fi

VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
VOLNAME="Auto Flow ${VERSION}"
OUT="tauri/src-tauri/target/release/bundle/dmg/Auto Flow_${VERSION}_aarch64.dmg"

# staging：.app + 指向 /Applications 的软链（经典拖拽安装布局）
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

echo ">>> 生成 dmg（UDZO 只读压缩）"
hdiutil create \
  -volname "$VOLNAME" \
  -srcfolder "$STAGING" \
  -ov -format UDZO \
  "$OUT"

echo "已生成：$OUT"
echo "--- 镜像信息 ---"
hdiutil imageinfo "$OUT" 2>/dev/null | grep -iE "^Format|Compressed Size|Checksum Type|Partition Type" || true
