#!/bin/zsh
# 对 Tauri 构建出的 Auto Flow.app 做 MacDev 双签 + 强化运行时 + 安全时间戳，
# 并（在提供 Apple 凭证时）走 notarytool 公证。对应 docs §12 / P3。
#
# 设计要点：
# - 与现存 scripts/build_spike_app.sh 同一 MacDev 身份，保证 TCC 授权跨构建稳定（§12.1）。
# - 嵌入式 Python sidecar(onedir)已用 MacDev 签过；这里对整个 .app 用 `--deep` 递归重签，
#   覆盖为同一身份 + runtime 选项，授权口径一致、不会打乱。
# - 强化运行时(--options runtime) + 时间戳(--timestamp) 是 notarization 的硬性要求。
# - 【关键】sidecar 签名必须带 entitlements-sidecar.plist 豁免库校验：PyInstaller 的
#   _internal/*.dylib 是 ad-hoc 签名、无 Team ID，而强化运行时默认要求进程加载的 dylib
#   由同一 Team 或 Apple 签名，否则 dlopen 直接报 "different Team IDs"、Python 共享库加载
#   失败、sidecar 静默起不来（表现为界面权限横幅三项全 false，极易误判成权限问题）。
#
# 用法：
#   ./scripts/sign_tauri_app.sh                 # 仅本地 MacDev 签名 + 校验
#   环境变量填齐后含公证：
#   APPLE_ID=you@icloud.com APPLE_APP_PASSWORD=xxxx APPLE_TEAM_ID=XXXXXXXXXX \
#     ./scripts/sign_tauri_app.sh
set -e
cd "$(dirname "$0")/.."

APP="tauri/src-tauri/target/release/bundle/macos/Auto Flow.app"
if [ ! -d "$APP" ]; then
  echo "先构建：cd tauri && npm run tauri build"
  exit 1
fi

IDENTITY="MacDev"
ENTITLEMENTS="tauri/src-tauri/entitlements-sidecar.plist"
if [ ! -f "$ENTITLEMENTS" ]; then
  echo "缺少授权文件：$ENTITLEMENTS（sidecar 需豁免库校验才能加载 ad-hoc dylib）"
  exit 1
fi

echo ">>> 1/2 先递归重签嵌入 sidecar（含 _internal dylibs），带 runtime + 时间戳 + 库校验豁免"
# 必须自底向上：--deep 的 --options runtime 不会传播到嵌套二进制，
# 而 notarization 要求所有可执行文件都带强化运行时，故 sidecar 单独签。
# 且必须带 --entitlements：见脚本头部「关键」一条，否则 sidecar 起不来。
codesign --force --deep --sign "$IDENTITY" \
  --options runtime --timestamp \
  --entitlements "$ENTITLEMENTS" \
  "$APP/Contents/Resources/autoflow-sidecar/autoflow-sidecar"

echo ">>> 2/2 再签 .app 外壳 + 主二进制（不带 --deep，嵌套已在第 1 步签好）"
codesign --force --sign "$IDENTITY" \
  --options runtime --timestamp \
  "$APP"

echo ">>> 校验签名"
codesign --verify --deep --verbose=2 "$APP"
echo "--- 主体身份 ---"
codesign -dvv "$APP" 2>&1 | grep -iE "authority|identifier|flags|teamidentifier|signature" || true
echo "--- 嵌入 sidecar 身份 ---"
codesign -dvv "$APP/Contents/Resources/autoflow-sidecar/autoflow-sidecar" 2>&1 | grep -iE "authority|flags|teamidentifier" || true

# ---- 启动冒烟：签名有效 ≠ 真能运行 ----
# 库校验失败时 codesign 照样显示 "valid on disk"，但一 dlopen 就崩、sidecar 静默退出。
# 故实发一帧 app.info 并取首行，拿到 JSON 结果才算真通过（这道检查本可提前发现该问题）。
echo "--- sidecar 启动冒烟 ---"
SIDECAR="$APP/Contents/Resources/autoflow-sidecar/autoflow-sidecar"
PROBE=$(printf '{"jsonrpc":"2.0","id":1,"method":"app.info"}\n' | "$SIDECAR" 2>/dev/null | head -1)
if [ -z "$PROBE" ]; then
  echo "✗ sidecar 未能启动或无响应：多半是库校验/签名问题，见脚本头部「关键」一条。"
  exit 1
fi
echo "✓ sidecar 启动正常"
echo "  $PROBE"

# ---- 公证（可选，需 Apple 开发者凭证）----
if [ -n "$APPLE_ID" ] && [ -n "$APPLE_APP_PASSWORD" ] && [ -n "$APPLE_TEAM_ID" ]; then
  echo ">>> 打包 .app 为 zip 并提交公证"
  ZIP="/tmp/AutoFlow-signed.zip"
  /usr/bin/ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" \
    --apple-id "$APPLE_ID" \
    --password "$APPLE_APP_PASSWORD" \
    --team-id "$APPLE_TEAM_ID" \
    --wait
  echo ">>>  stapling 公证票据到 .app"
  xcrun stapler staple "$APP"
  echo "公证完成：可直接分发，无需用户手动授权弹窗。"
else
  echo "（跳过公证：未提供 APPLE_ID / APPLE_APP_PASSWORD / APPLE_TEAM_ID）"
  echo "本地 MacDev 签名已完成；要分发需补公证步骤（见 docs/权限引导.md 或本脚本头部）。"
fi
