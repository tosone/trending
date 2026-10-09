#!/bin/bash
# 双击本文件即可在本地起一个静态服务并打开 index.html
# （浏览器不允许 file:// 页面读取同目录的 JSON，所以需要走 HTTP）
cd "$(dirname "$0")" || exit 1
PORT=8137
sleep 1 && open "http://localhost:$PORT/" &
echo "http://localhost:$PORT/  ·  Ctrl-C 停止"
exec python3 -m http.server "$PORT"
