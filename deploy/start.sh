#!/bin/bash
# noti-gateway 启动脚本（crontab / 手动启动共用）
# 自动切换到项目根目录：config.yaml、log/、noti-gateway.db 都按项目根解析
# 优先使用项目内 venv（deploy/setup.sh 创建），没有则回退系统 python3
cd "$(dirname "$0")/.." || exit 1
if [ -x "venv/bin/python" ]; then
    exec venv/bin/python -m noti_gateway.server
fi
exec python3 -m noti_gateway.server
