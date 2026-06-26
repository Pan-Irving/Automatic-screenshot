#!/bin/zsh
set -e

cd "$(dirname "$0")"

echo "正在启动 yingdao_mvp 本地 Web 后台..."
echo "后台地址：http://127.0.0.1:8765"
echo "请先打开后台页面，在“账号准备”里选择账号数量并打开账号窗口。"
echo ""

python3 server.py
