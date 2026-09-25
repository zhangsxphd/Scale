#!/bin/bash
# MVT-485 智能称重系统 - Mac 一键启动脚本

# 切换到脚本所在的目录
cd "$(dirname "$0")"

echo "=================================================="
echo "    启动 MVT-485 智能称重系统 (端口: 5050)"
echo "=================================================="
if lsof -nP -iTCP:5050 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "错误：端口 5050 已被占用。请先正常退出已有服务。"
    exit 1
fi

if ! python3 -c 'import flask, serial' >/dev/null 2>&1; then
    echo "缺少依赖，请先执行：python3 -m pip install -r requirements.txt"
    exit 1
fi

echo "正在启动 Flask 服务器（仅本机访问）..."
exec python3 app.py
