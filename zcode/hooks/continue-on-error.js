// ZCode Stop 钩子：报错停止后自动“继续”
// 规则：第1次报错停止等 3 秒后自动继续，第2次等 10 秒后自动继续，第3次起不再干预；
//      用户发送新消息时计数清零（由 UserPromptSubmit 钩子以 reset 参数调用）。
// 防误续：matcher 只认“停止型报错短语”（见 config.json）；脚本再检查响应结尾，
//      如果带着完成/收尾语气（已修复、任务完成、测试通过…）就不续——宁可漏续，不可误续。
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

const MODE = process.argv[2] || 'stop';
const DELAYS = [3000, 10000];
const MAX_RETRIES = 2;
const SESSION = process.env.CLAUDE_SESSION_ID || process.env.ZCODE_SESSION_ID || 'default';
const STATE = path.join(os.tmpdir(), 'zcode-continue-retry-' + SESSION + '.txt');
const LOG = path.join(__dirname, 'continue-on-error.log');

// 结尾出现这些“完成态”用语 → 视为正常收尾，不续
const DONE_RE = /已完成|任务完成|已修复|已解决|已处理|全部通过|测试通过|构建通过|编译通过|验证通过|收尾|完成$|成功了|搞定|✅|✔|done|complete/i;
// 但结尾同时带着这些硬性报错痕迹 → 仍然续
const HARD_ERR_RE = /Traceback|ERROR:|Error:|error:|FATAL|exit code [1-9]|429|502|503/;

function readCount() {
  try { return parseInt(fs.readFileSync(STATE, 'utf8').trim(), 10) || 0; } catch (e) { return 0; }
}
function writeCount(n) {
  try { fs.writeFileSync(STATE, String(n)); } catch (e) {}
}
function log(line) {
  try { fs.appendFileSync(LOG, '[' + new Date().toISOString() + '] [' + SESSION + '] ' + line + '\n'); } catch (e) {}
}
function collectStrings(v, out) {
  if (typeof v === 'string') out.push(v);
  else if (Array.isArray(v)) for (const x of v) collectStrings(x, out);
  else if (v && typeof v === 'object') for (const k of Object.keys(v)) collectStrings(v[k], out);
}

if (MODE === 'reset') {
  writeCount(0);
  log('用户发送了新消息，重试计数已清零');
  process.exit(0);
}

// 读取 stdin（ZCode 会把事件 JSON 写进来），留档前 200 字符便于日后调参
let raw = '';
try { if (!process.stdin.isTTY) raw = fs.readFileSync(0, 'utf8'); } catch (e) {}

const count = readCount();
if (count >= MAX_RETRIES) {
  log('报错停止（第 ' + (count + 1) + ' 次），重试次数已用完，不再干预');
  process.exit(0);
}

// 完成态护栏：取所有字符串字段的拼接，看结尾 300 字符
let tail = '';
try {
  const parts = [];
  collectStrings(JSON.parse(raw), parts);
  tail = parts.join('\n').slice(-300);
} catch (e) { tail = raw.slice(-300); }
log('stdin 预览: ' + raw.slice(0, 200).replace(/\s+/g, ' ') + ' | 结尾: ' + tail.slice(-80).replace(/\s+/g, ' '));

if (tail && DONE_RE.test(tail) && !HARD_ERR_RE.test(tail)) {
  log('结尾呈完成/收尾语气，判定为正常结束，不续');
  process.exit(0);
}

const delay = DELAYS[count];
const nth = count + 1;
writeCount(count + 1);
log('报错停止（第 ' + nth + ' 次），等待 ' + delay / 1000 + 's 后自动继续');
setTimeout(() => {
  process.stdout.write(JSON.stringify({
    decision: 'block',
    reason: '检测到任务疑似出错，已等待 ' + delay / 1000 + ' 秒后自动重试（第 ' + nth +
      ' 次）。请继续完成之前的任务；如果任务其实已经完成，请直接总结收尾，不要重复已做的工作。'
  }));
  process.exit(0);
}, delay);
