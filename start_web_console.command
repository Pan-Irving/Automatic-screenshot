#!/bin/zsh
set -e

cd "$(dirname "$0")"

echo "正在启动 yingdao_mvp 本地 Web 后台..."
echo "后台地址：http://127.0.0.1:8765"
echo "如需采集，请先确认 start_chrome_cdp.command 已启动，且 DeepSeek 已登录。"
echo ""

python3 server.py
