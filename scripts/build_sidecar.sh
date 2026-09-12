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

# 不直接删旧 onedir（281 个文件会触发批量删除确认，且 PyInstaller --noconfirm
# 内部的清理同样会被拦）。同卷 rename 回避——rename 不是删除；旧目录留作
# dist/.autoflow-sidecar.old.*，需要时手动分批清理。
#
# 中间产物目录 build/ 同样要处理：PyInstaller 在 Analysis 阶段会清掉
# build/<name>/ 里的旧文件（约 50+ 个），那一步也会被拦下，表现为
# 「[safe-delete] SAFE_DELETE_BULK_CONFIRM_REQUIRED」后构建中止。
for d in "dist/autoflow-sidecar" "build/autoflow-sidecar"; do
  if [ -d "$d" ]; then
    parent="$(dirname "$d")"
    base="$(basename "$d")"
    mv "$d" "$parent/.$base.old.$(date +%Y%m%d-%H%M%S)"
  fi
done

# 自带 YOLO 模型必须打进包：frozen 时 models_dir() 从 sys._MEIPASS/models 播种到
# 用户数据目录（~/Library/Application Support/AutoFlow/models）。不打进去的话，
# 干净用户目录下 YOLO 节点找不到默认模型——开发机上看不出来，因为源码树里就有。
# 目标名 'models' 与 core/paths.py 的 _BUNDLED_MODEL 查找路径一致。
MODEL="models/yolo11n.onnx"
ADD_DATA=()
if [ -f "$MODEL" ]; then
  ADD_DATA=(--add-data "$MODEL:models")
else
  echo "警告：未找到 $MODEL，本次打包不含默认模型（YOLO 节点需手动指定模型）"
fi

./.venv/bin/python -m PyInstaller --noconfirm --onedir --name autoflow-sidecar \
  "${ADD_DATA[@]}" \
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

# 暂存进 Tauri 资源目录。tauri.conf.json 的 bundle.resources 是
# ["autoflow-sidecar/**/*"]（相对 src-tauri/），只跑 PyInstaller 而漏掉这一步的话，
# Tauri 打包进去的仍是**上一次**的 sidecar——表现为"改了 Python 代码却毫无变化"，
# 排查成本极高（曾因此让旧构建跑了好几天）。故此处一并完成。
# 旧目录同样走同卷 rename，避免触发批量删除确认。
STAGE="tauri/src-tauri/autoflow-sidecar"
if [ -d "$STAGE" ]; then
  mv "$STAGE" "$STAGE.old.$(date +%Y%m%d-%H%M%S)"
fi
cp -R "dist/autoflow-sidecar" "$STAGE"
if security find-identity -v -p codesigning | grep -q "MacDev"; then
  codesign --force --deep --sign "MacDev" "$STAGE/autoflow-sidecar"
fi
echo "已暂存到 Tauri 资源目录: $STAGE"

echo "=== 体积基线 ==="
du -sh dist/autoflow-sidecar
echo "可执行: $SIDECAR"
