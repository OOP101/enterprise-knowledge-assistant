#!/usr/bin/env bash
# 企业知识助手 - Linux/macOS 启动脚本（启动逻辑统一在 launcher.py）
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
    echo "[错误] 未找到 Python3，请安装 Python 3.10+"
    exit 1
fi

exec "$PY" launcher.py "$@"
