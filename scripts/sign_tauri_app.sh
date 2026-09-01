#!/bin/zsh
# 对 Tauri 构建出的 Auto Flow.app 做 MacDev 双签 + 强化运行时 + 安全时间戳，
# 并（在提供 Apple 凭证时）走 notarytool 公证。对应 docs §12 / P3。
#
# 设计要点：
# - 与现存 scripts/build_spike_app.sh 同一 MacDev 身份，保证 TCC 授权跨构建稳定（§12.1）。
# - 嵌入式 Python sidecar(onedir)已用 MacDev 签过；这里对整个 .app 用 `--deep` 递归重签，
#   覆盖为同一身份 + runtime 选项，授权口径一致、不会打乱。
# - 强化运行时(--options runtime) + 时间戳(--timestamp) 是 notarization 的硬性要求。
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

echo ">>> 1/2 先递归重签嵌入 sidecar（含 _internal dylibs），带 runtime + 时间戳"
# 必须自底向上：--deep 的 --options runtime 不会传播到嵌套二进制，
# 而 notarization 要求所有可执行文件都带强化运行时，故 sidecar 单独签。
codesign --force --deep --sign "$IDENTITY" \
  --options runtime --timestamp \
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
