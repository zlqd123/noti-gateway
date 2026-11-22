// UserPromptSubmit 钩子入口：任务启动时拉起/接续本会话的轮次监视器。
// 分离式启动（detached + unref），钩子立即返回，不阻塞提交流程。
// 停用开关：在 ~/.zcode/hooks/ 下新建空文件 DISABLE-AUTOCONTINUE 即可整体停用。
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const SESSION = process.env.CLAUDE_SESSION_ID || process.env.ZCODE_SESSION_ID
  // 钩子环境变量缺失时，从 stdin 的事件 JSON 兜底提取 session_id
  || (() => { try { if (!process.stdin.isTTY) return JSON.parse(fs.readFileSync(0, 'utf8')).session_id || ''; } catch (e) {} return ''; })()
  || '';
const HOOKS = path.join(os.homedir(), '.zcode', 'hooks');

if (!SESSION) process.exit(0);
if (fs.existsSync(path.join(HOOKS, 'DISABLE-AUTOCONTINUE'))) process.exit(0);

try {
  const child = spawn(process.execPath, [path.join(HOOKS, 'watch-turn.js'), SESSION], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true
  });
  child.unref();
} catch (e) {}
process.exit(0);
