#!/usr/bin/env bash
# UserPromptSubmit 钩子入口：经 bash 包装拉起 spawn-watch.js（轮次监视器启动器）。
# 与 qq-notify.sh 走同一套已验证的钩子进程方式，避免 node 路径带空格的引号问题。
NODE_BIN=$(command -v node || echo "/c/Program Files/nodejs/node.exe")
exec "$NODE_BIN" "$HOME/.zcode/hooks/spawn-watch.js" 2>>"$HOME/.zcode/hooks/watch-turn.log"
