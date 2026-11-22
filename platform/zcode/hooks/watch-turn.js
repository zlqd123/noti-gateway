// 轮次监视器：由 spawn-watch.js（UserPromptSubmit 钩子）分离式拉起，盯本会话的轮次命运。
// 信号（来自 ~/.zcode/cli/log/zcode-*.jsonl，全部结构化字段，不读模型输出）：
//   turn.failed 且非手动取消 → 第1次等3s、第2次等10s，调用 continue-input.ps1 注入“继续”
//   第3次起放弃；turn.completed → 正常退出；其他会话有新动静（用户切走）→ 放弃注入防打错窗口
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const SESSION = process.argv[2] || process.env.CLAUDE_SESSION_ID || 'default';
const LOG_DIR = process.argv[3] || path.join(os.homedir(), '.zcode', 'cli', 'log');
const DRY_RUN = process.env.DRY_RUN === '1';
const DELAYS = [3000, 10000];
const TTL_MS = 30 * 60 * 1000;
const POLL_MS = 500;
const CANCEL_RE = /cancel/i; // TURN_CANCELLED / model_request_cancelled → 用户主动取消

const HOOKS = path.join(os.homedir(), '.zcode', 'hooks');
const STATE_DIR = path.join(HOOKS, 'state');
const LOG = path.join(HOOKS, 'watch-turn.log');
const PID_FILE = path.join(STATE_DIR, 'monitor-' + SESSION + '.pid');
const RETRY_FILE = path.join(STATE_DIR, 'retry-' + SESSION + '.txt');

function log(msg) {
  try { fs.appendFileSync(LOG, '[' + new Date().toISOString() + '] [' + SESSION + '] ' + msg + '\n'); } catch (e) {}
}

// 单实例：替换同会话旧监视器
function singleInstance() {
  try {
    const old = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
    if (old && old !== process.pid) {
      try { process.kill(old, 0); } catch (e) { return writePid(); }
      try { spawn('taskkill', ['/PID', String(old), '/T', '/F'], { stdio: 'ignore' }); } catch (e) {}
      log('已终止旧监视器 pid=' + old);
    }
  } catch (e) {}
  function writePid() {
    try { fs.writeFileSync(PID_FILE, String(process.pid)); } catch (e) {}
  }
  writePid();
}

// 重试计数跨监视器延续：注入的“继续”会作为新 prompt 触发新监视器，
// 90 秒内的计数延续（保证 3s→10s→放弃 的序列不中断）；更早的计数视为过期、从 0 开始
let retryCount = 0;
try {
  const prev = JSON.parse(fs.readFileSync(RETRY_FILE, 'utf8'));
  if (Date.now() - prev.ts < 90000 && prev.count > 0) {
    retryCount = prev.count;
    log('延续重试计数 ' + retryCount + '（上次写入于 ' + new Date(prev.ts).toISOString() + '）');
  }
} catch (e) {}

function saveRetry() {
  try { fs.writeFileSync(RETRY_FILE, JSON.stringify({ count: retryCount, ts: Date.now() })); } catch (e) {}
}

singleInstance();
log('监视器启动' + (DRY_RUN ? '（DRY_RUN）' : ''));

let busy = false;              // 正处于“等待注入”窗口
let lastActivitySession = SESSION; // 最近发生新动静的会话（用于判断用户是否切走）
const t0 = Date.now();
let fd = null, filePath = '', pos = 0, buf = '', missNotified = false;

function todayFile() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return path.join(LOG_DIR, 'zcode-' + d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + '.jsonl');
}

// 不可自动重试的失败：续一次也是白点（鉴权/配额/余额/上下文超限），放弃并退出
const FATAL_RE = /unauthor|forbidden|api[_-]?key|quota|balance|billing|insufficient|context.*(length|overflow|exceed)/i;

function handleRecord(rec) {
  if (!rec) return;
  const sid = rec.sessionId;

  // v4 桌面协议路径的失败记录（无 event 字段，bootstrap.zcode_protocol）：
  // 与 core.runtime 的 turn.failed 常成对出现，busy 守卫天然去重；
  // 其 context.error 是字符串（如 "Turn was cancelled."）
  if (!rec.event && rec.message === 'v4 background turn failed' && sid === SESSION && !busy) {
    const errStr = String((rec.context && rec.context.error) || '');
    if (CANCEL_RE.test(errStr)) { log('轮次为手动取消（v4 background: ' + errStr.slice(0, 60) + '），监视器退出'); process.exit(0); }
    if (FATAL_RE.test(errStr)) { log('不可重试的失败（' + errStr.slice(0, 60) + '），放弃自动继续，监视器退出'); process.exit(0); }
    onFailure('V4_BACKGROUND', errStr);
    return;
  }
  if (!rec.event) return;

  if (rec.event === 'turn.started' && sid) {
    lastActivitySession = sid;
    if (sid === SESSION && !busy && retryCount !== 0) {
      retryCount = 0; saveRetry();
      log('新轮次开始，重试计数清零');
    }
    return;
  }
  if (rec.event === 'turn.completed' && sid === SESSION && !busy) {
    log('轮次正常完成，监视器退出');
    process.exit(0);
  }
  if (rec.event === 'turn.failed' && sid === SESSION && !busy) {
    // 2026-11 起错误对象在顶层 rec.error；10-25 旧 fixture 在 rec.context.error（可能为对象或字符串）
    const err = rec.error || (rec.context && rec.context.error) || {};
    const cause = err.cause || {};
    const code = err.code || err.type || cause.code || 'UNKNOWN';
    const msg = err.message || (typeof err === 'string' ? err : '') || '';
    if (rec.status === 'cancelled' || CANCEL_RE.test(code) || CANCEL_RE.test(cause.code || '') || CANCEL_RE.test(msg)) {
      log('轮次为手动取消（' + code + '），监视器退出'); process.exit(0);
    }
    if (FATAL_RE.test(code) || FATAL_RE.test(msg)) {
      log('不可重试的失败（' + code + ' ' + String(msg).slice(0, 60) + '），放弃自动继续，监视器退出');
      process.exit(0);
    }
    onFailure(code, msg);
  }
}

function onFailure(code, message) {
  if (retryCount >= DELAYS.length) { log('第 3 次失败，放弃自动继续，监视器退出'); process.exit(0); }
  const delay = DELAYS[retryCount];
  retryCount += 1;
  saveRetry();
  busy = true;
  log('轮次失败 code=' + code + ' msg=' + String(message).slice(0, 80) +
      ' → 第 ' + retryCount + ' 次，等 ' + delay / 1000 + 's 后注入“继续”');
  setTimeout(() => {
    if (lastActivitySession !== SESSION) {
      log('等待期间用户已切到其他会话，放弃注入（防打错窗口），退出');
      process.exit(0);
    }
    if (DRY_RUN) { log('DRY_RUN：此处应调用 continue-input.ps1 注入“继续”'); busy = false; return; }
    const child = spawn('powershell.exe',
      ['-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', path.join(HOOKS, 'continue-input.ps1')],
      { stdio: 'ignore', windowsHide: true });
    child.on('exit', c => {
      log('continue-input.ps1 退出码 ' + c + (c === 0 ? '（已注入“继续”）' : '（未注入，停止本次任务）'));
      busy = false;
      if (c !== 0) process.exit(0);
    });
  }, delay);
}

setInterval(() => {
  if (Date.now() - t0 > TTL_MS) { log('到达 30 分钟 TTL，监视器退出'); process.exit(0); }
  const f = todayFile();
  if (f !== filePath) {
    filePath = f; buf = '';
    if (fd) { try { fs.closeSync(fd); } catch (e) {} fd = null; }
  }
  if (filePath && !fd) {
    // fd 为空时每次轮询都重试打开（文件可能尚未创建/刚轮转出现）
    try { fd = fs.openSync(filePath, 'r'); pos = fs.fstatSync(fd).size; missNotified = false; }
    catch (e) { if (!missNotified) { log('日志文件暂不可读：' + filePath + '，继续等待其出现'); missNotified = true; } }
  }
  if (!fd) return;
  let st;
  try { st = fs.fstatSync(fd); } catch (e) { return; }
  if (st.size < pos) { pos = 0; buf = ''; }
  if (st.size === pos) return;
  const chunk = Buffer.alloc(st.size - pos);
  fs.readSync(fd, chunk, 0, chunk.length, pos);
  pos += chunk.length;
  buf += chunk.toString('utf8');
  const lines = buf.split('\n');
  buf = lines.pop();
  for (const ln of lines) {
    const s = ln.trim();
    if (!s) continue;
    let rec; try { rec = JSON.parse(s); } catch (e) { continue; }
    handleRecord(rec);
  }
}, POLL_MS);
