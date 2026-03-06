#!/bin/bash
# 启动服务（默认启用 9 点定时任务）
cd "$(dirname "$0")"
exec python server.py "$@"
