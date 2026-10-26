#!/bin/bash
# 安装 crontab 开机自启：在 tmux 会话 gate 内运行网关（幂等，可重复执行）
# 开机后用  tmux attach -t gate  查看实时输出（Ctrl+B 再按 D 退出会话，不影响运行）
# 网关路径默认 /root/noti-gateway，不同路径用 GW_ROOT=/your/path ./setup-cron.sh
GW_ROOT="${GW_ROOT:-/root/noti-gateway}"

if [ ! -d "$GW_ROOT" ]; then
    echo "目录不存在: $GW_ROOT（用 GW_ROOT=/your/path ./setup-cron.sh 指定）"
    exit 1
fi

TMUX_BIN="$(command -v tmux)" || {
    echo "缺少 tmux：先执行  apt install -y tmux  再重试"
    exit 1
}

LINE="@reboot $TMUX_BIN has-session -t gate 2>/dev/null || $TMUX_BIN new-session -d -s gate $GW_ROOT/deploy/start.sh >> $GW_ROOT/log/boot.log 2>&1"

( crontab -l 2>/dev/null | grep -vF "$LINE" ; echo "$LINE" ) | crontab - \
    && echo "已安装开机自启(tmux 会话 gate):" && echo "  $LINE" \
    && echo "当前 crontab:" && crontab -l
