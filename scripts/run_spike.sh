#!/bin/zsh
# 运行已安装的 spike sidecar，喂入一行 JSON-RPC（默认 app.info），打印 stdout 帧。
# 用法：
#   ./scripts/run_spike.sh                      # app.info（看三项权限）
#   ./scripts/run_spike.sh app.diagnose
#   ./scripts/run_spike.sh app.requestPermissions
#   ./scripts/run_spike.sh '{"jsonrpc":"2.0","id":1,"method":"app.shutdown"}'
set -e
# 运行的是 .app 的主可执行文件（Contents/MacOS），它正是被 TCC 授权（屏幕录制等）的二进制。
SIDECAR="/Applications/Auto Flow RPC Spike.app/Contents/MacOS/autoflow-sidecar"
if [ ! -x "$SIDECAR" ]; then
  echo "未找到 spike，请先运行 scripts/build_spike_app.sh 并安装到 /Applications" >&2
  exit 1
fi
if [ -z "$1" ]; then
  FRAME='{"jsonrpc":"2.0","id":1,"method":"app.info"}'
elif [ "${1:0:1}" = '{' ]; then
  FRAME="$1"
else
  FRAME="{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\"}"
fi
printf '%s\n' "$FRAME" | "$SIDECAR" 2>/dev/null
