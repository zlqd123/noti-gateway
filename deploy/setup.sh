#!/bin/bash
# 一次性部署：项目内 venv（规避 PEP 668）+ 依赖 + crontab 开机自启
# 在服务器项目根目录执行：./deploy/setup.sh
set -e
cd "$(dirname "$0")/.."

# 残缺 venv（有 python 没 pip，常见于 python3-venv 未装时的半成品）直接重建
if [ ! -x venv/bin/python ] || [ ! -x venv/bin/pip ]; then
    echo "[1/3] 重建 venv ..."
    rm -rf venv
    python3 -m venv venv || {
        echo "venv 创建失败：先执行  apt install -y python3-venv  再重试"
        exit 1
    }
    [ -x venv/bin/pip ] || { echo "venv 仍不完整（缺 pip），请检查 python3-venv 是否装好"; exit 1; }
else
    echo "[1/3] venv 已存在"
fi

echo "[2/3] 安装依赖 ..."
venv/bin/pip install --quiet -r requirements.txt

echo "[3/3] 配置 crontab 开机自启 ..."
./deploy/setup-cron.sh

echo
echo "完成。手动启动: ./deploy/start.sh   （crontab 已配置 @reboot 自启）"
