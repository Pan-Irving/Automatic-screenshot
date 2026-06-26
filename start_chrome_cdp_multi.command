#!/bin/zsh
set -e

cd "$(dirname "$0")"

ACCOUNTS="${YINGDAO_ACCOUNTS:-deepseek_a:9222:chrome_cdp_profile_deepseek_a:2,deepseek_b:9223:chrome_cdp_profile_deepseek_b:2,deepseek_c:9224:chrome_cdp_profile_deepseek_c:2}"

echo "正在启动多账号 Chrome CDP 实例..."
echo "账号配置：$ACCOUNTS"

IFS=',' read -A ITEMS <<< "$ACCOUNTS"
for ITEM in "${ITEMS[@]}"; do
  ACCOUNT_ID="$(echo "$ITEM" | cut -d ':' -f 1)"
  CDP_PORT="$(echo "$ITEM" | cut -d ':' -f 2)"
  PROFILE_NAME="$(echo "$ITEM" | cut -d ':' -f 3)"
  if [[ -z "$ACCOUNT_ID" ]]; then
    continue
  fi
  if [[ -z "$CDP_PORT" ]]; then
    CDP_PORT=9222
  fi
  if [[ -z "$PROFILE_NAME" ]]; then
    PROFILE_NAME="chrome_cdp_profile_${ACCOUNT_ID}"
  fi

  PROFILE="$(pwd)/$PROFILE_NAME"
  mkdir -p "$PROFILE"
  LOG="/tmp/yingdao_chrome_cdp_${ACCOUNT_ID}_${CDP_PORT}.log"

  echo "启动 $ACCOUNT_ID：端口 $CDP_PORT，Profile $PROFILE"
  open -na "Google Chrome" --args \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port="$CDP_PORT" \
    --remote-debugging-address=127.0.0.1 \
    --remote-allow-origins='*' \
    --no-first-run \
    --no-default-browser-check \
    "https://chat.deepseek.com/" \
    >"$LOG" 2>&1
done

echo "已启动。首次使用需要分别在每个 Chrome 窗口登录对应 DeepSeek 账号。"
echo "CDP 端口请保持和 Web 后台 YINGDAO_ACCOUNTS 配置一致。"
