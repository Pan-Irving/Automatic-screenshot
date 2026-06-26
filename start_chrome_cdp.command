#!/bin/zsh
set -e

echo "正在重启 Google Chrome，并开启 CDP 9222 端口..."
osascript -e 'tell application "Google Chrome" to quit' >/dev/null 2>&1 || true
sleep 3
if pgrep -x "Google Chrome" >/dev/null 2>&1; then
  echo "Chrome 未完全退出，强制结束残留进程..."
  pkill -x "Google Chrome" >/dev/null 2>&1 || true
  sleep 2
fi

cd "$(dirname "$0")"
PROFILE="$(pwd)/chrome_cdp_profile"
mkdir -p "$PROFILE"

open -na "Google Chrome" --args \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins='*' \
  --no-first-run \
  --no-default-browser-check \
  "https://chat.deepseek.com/" \
  >/tmp/yingdao_chrome_cdp.log 2>&1

echo "已启动。请确认这个专用 Chrome 里的 DeepSeek 登录态正常后，再运行 Web 后台。"
echo "专用 Chrome 用户目录：$PROFILE"
echo "CDP 日志：/tmp/yingdao_chrome_cdp.log"
