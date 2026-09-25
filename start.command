#!/bin/bash
# MVT-485 智能称重系统 - Mac 一键启动脚本

# 切换到脚本所在的目录
cd "$(dirname "$0")"

echo "=================================================="
echo "    启动 MVT-485 智能称重系统 (端口: 5050)"
echo "=================================================="
echo "如果端口被占用，将尝试自动清理旧进程..."
lsof -i :5050 | grep LISTEN | awk '{print $2}' | xargs kill -9 2>/dev/null

echo "正在启动 Flask 服务器..."
python3 app.py
