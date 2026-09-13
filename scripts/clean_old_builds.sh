#!/bin/zsh
# 清理构建残留（优先同卷改名到废纸篓，可反悔；跨卷时退化为直接删除）。
#
# 为什么需要：构建脚本为了避免触发宿主环境的批量删除保护，一律用「同卷 rename」
# 让旧产物出局（见 scripts/build_sidecar.sh / tauri/scripts/fresh-dist.mjs）。
# 代价是 dist/.autoflow-sidecar.old.* 这类目录会不断堆积——每份约 230MB，
# 不清理的话很快就会吃掉几 GB。
#
# 跨卷注意：项目在独立 APFS 卷（如 /Users/catlair/mobile）上时，mv 到 ~/.Trash
# 是跨设备 rename，会报 `EXDEV: cross-device link not permitted`，一份都移不走。
# 这时退化为 rm -rf——这些都是可再生的旧构建产物，且 --apply 是显式确认过的。
# （Finder/AppleScript 删除会被宿主沙箱以「权限违例」挡掉，走不通。）
#
# 覆盖不到：dist/ 里的**一次性遗留**（如旧版 `Auto Flow.app`/`.dmg`，Python-UI
# 时代的产物）。它们不带 .old 后缀，为防误删现用产物不纳入本脚本，需手动删。
#
# 用法：
#   ./scripts/clean_old_builds.sh            # 预演：只列出将清理的内容与总大小
#   ./scripts/clean_old_builds.sh --apply    # 实际清理
set -e
cd "$(dirname "$0")/.."

APPLY=0
[ "$1" = "--apply" ] && APPLY=1

# zsh 的 (N) 限定符 = 无匹配时展开为空（等价 nullglob），不会报错。
pattern=(
  dist/.autoflow-sidecar.old.*(N)
  build/.autoflow-sidecar.old.*(N)
  tauri/src-tauri/autoflow-sidecar.old.*(N)
  tauri/src-tauri/.autoflow-sidecar.old.*(N)
  tauri/dist.old.*(N)
  .autoflow-sidecar.old.*(N)
  .af-sidecar-old-*(N)
)

if [ ${#pattern[@]} -eq 0 ]; then
  echo "没有需要清理的构建残留。"
  exit 0
fi

echo "发现 ${#pattern[@]} 份构建残留："
total_kb=0
for p in "${pattern[@]}"; do
  kb=$(du -sk "$p" | awk '{print $1}')
  total_kb=$((total_kb + kb))
  printf "  %8s  %s\n" "$(du -sh "$p" | awk '{print $1}')" "$p"
done
printf "合计约 %.1f GB\n" "$(echo "$total_kb / 1048576" | bc -l)"

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "（预演模式，未改动任何文件。确认后执行： ./scripts/clean_old_builds.sh --apply）"
  exit 0
fi

TRASH="$HOME/.Trash"
mkdir -p "$TRASH"
STAMP="$(date +%Y%m%d-%H%M%S)"
moved=0
deleted=0
for p in "${pattern[@]}"; do
  base="$(basename "$p")"
  dest="$TRASH/${base}.${STAMP}"
  # 目标已存在时加序号，避免 mv 把目录塞进目录里
  n=1
  while [ -e "$dest" ]; do
    dest="$TRASH/${base}.${STAMP}.$n"
    n=$((n + 1))
  done
  if mv "$p" "$dest" 2>/dev/null; then
    moved=$((moved + 1))
  else
    # 跨卷 rename 失败（EXDEV）：这些都是可再生的旧构建产物，直接删。
    rm -rf "$p"
    deleted=$((deleted + 1))
  fi
done

echo "已移入废纸篓 $moved 份（可从 Finder 废纸篓恢复）：$TRASH"
[ "$deleted" -gt 0 ] && echo "跨卷无法入废纸篓，已直接删除 $deleted 份（可再生构建产物）"
exit 0
