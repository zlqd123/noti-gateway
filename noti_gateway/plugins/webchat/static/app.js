/* WebChat 消息面板前端逻辑 */

const API = "/webchat/api";
const $ = (sel) => document.querySelector(sel);
const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 会话仅存内存：刷新/关闭页面即失效，每次打开网页都需重新验证 TOTP
let token = "";
localStorage.removeItem("webchat_session");
sessionStorage.removeItem("webchat_session");
let aliases = JSON.parse(localStorage.getItem("webchat_aliases") || "{}");
let chats = [];
let current = null;          // 当前会话 key
let currentMsgs = [];        // 当前会话内存中的消息
let oldestSeq = null;
let hasMore = false;
let loadingHistory = false;  // 历史分页加载中（防重复触发）
let unread = {};             // chat -> 未读数（仅本地）
let replyTarget = null;      // {msg_id, user, nickname, text}
let ws = null;
let wsRetry = 0;
let botOnline = null;
let pendingFile = null;      // 待发送附件
let pendingFileUrl = null;
let memberMap = {};          // openid -> 昵称（当前会话已见过的成员）
let sending = false;         // 发送请求进行中（防连点）

/* -------------------------------------------------------------- 工具 */

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDay(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  if (sameDay) return "今天";
  const yesterday = new Date(today.getTime() - 86400000);
  if (d.toDateString() === yesterday.toDateString()) return "昨天";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function avatarColor(str) {
  let h = 0;
  for (const ch of String(str)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 55%, 45%)`;
}
function shortId(s) { return String(s || "").slice(0, 10); }
function displayName(chat, type, name) {
  // 用户手动改的别名优先，其次官方群名，最后 openid 短格式
  const base = aliases[chat] || name;
  if (base) return base;
  return type === "group" ? `群聊 ${shortId(chat.slice(6))}` :
    type === "friend" ? `好友 ${shortId(chat.slice(7))}` :
    type === "dms" ? `私信 ${shortId(chat.slice(4))}` :
    `频道 ${shortId(chat.slice(6))}`;
}
function setStatus(text, cls = "") {
  const el = $("#statusLine");
  el.textContent = text || "";
  el.className = "status-line " + cls;
}
function toast(text, cls = "") {
  const div = document.createElement("div");
  div.className = "toast " + cls;
  div.textContent = text;
  $("#toasts").appendChild(div);
  setTimeout(() => div.remove(), 6000);
}
function updateTitle() {
  const n = Object.values(unread).reduce((a, b) => a + b, 0);
  document.title = n > 0 ? `(${n}) WebChat 消息面板` : "WebChat 消息面板";
}

/* -------------------------------------------------------------- 成员与 @ */

function rebuildMemberMap() {
  const map = {};
  for (const m of currentMsgs) {
    if (m.dir === "in" && m.user && m.user !== "bot") {
      if (m.nickname) map[m.user] = m.nickname;
      else if (!(m.user in map)) map[m.user] = "";
    }
  }
  memberMap = map;
}

function memberName(id) {
  const nick = memberMap[id];
  return escapeHtml(nick || shortId(id));
}

// 展示文本：<@openid> 发送标记与后端渲染的 @openid 都解析成昵称
function renderText(text) {
  let html = escapeHtml(text);
  html = html.replace(/&lt;@(\w{8,})&gt;/g, (m, id) => "@" + memberName(id));
  html = html.replace(/@([0-9A-Fa-f]{12,})/g, (m, id) => "@" + memberName(id));
  return html;
}

function openMentionPopup() {
  const popup = $("#mentionPopup");
  const entries = Object.entries(memberMap);
  popup.innerHTML = "";
  if (!entries.length) {
    popup.innerHTML = `<div class="mention-empty">暂无已知成员<br>需先收到过该成员在会话中的发言</div>`;
    popup.hidden = false;
    return;
  }
  entries.sort((a, b) => (b[1] ? 1 : 0) - (a[1] ? 1 : 0));
  for (const [id, nick] of entries) {
    const div = document.createElement("div");
    div.className = "mention-item";
    div.textContent = nick || shortId(id);
    div.title = id;
    div.addEventListener("click", () => {
      insertMentionToken(id);
      closeMentionPopup();
    });
    popup.appendChild(div);
  }
  popup.hidden = false;
}

function closeMentionPopup() {
  $("#mentionPopup").hidden = true;
}

function insertMentionToken(id) {
  const input = $("#input");
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.setRangeText(`<@${id}>`, start, end, "end");
  input.focus();
  autosize();
}

/* -------------------------------------------------------------- API */

async function api(path, opts = {}) {
  const resp = await fetch(API + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Webchat-Token": token,
      ...(opts.headers || {}),
    },
  });
  if (resp.status === 401) { showTokenOverlay("访问令牌不正确"); throw new Error("unauthorized"); }
  if (resp.status === 403) { showTokenOverlay("跨站请求被拒绝"); throw new Error("forbidden"); }
  return resp.json();
}

/* -------------------------------------------------------------- 侧栏 */

async function fetchChats() {
  const data = await api("/chats");
  chats = data.chats || [];
  renderChatList();
}

function renderChatList() {
  const kw = $("#search").value.trim().toLowerCase();
  const list = $("#chatList");
  list.innerHTML = "";
  const filtered = chats.filter((c) => {
    if (!kw) return true;
    const alias = (aliases[c.chat] || "").toLowerCase();
    return c.chat.toLowerCase().includes(kw) || alias.includes(kw) ||
      (c.last_text || "").toLowerCase().includes(kw);
  });
  if (!filtered.length) {
    list.innerHTML = `<div class="chat-empty">${kw ? "没有匹配的会话" : "暂无会话<br>收到消息后自动出现"}</div>`;
    return;
  }
  for (const c of filtered) {
    const div = document.createElement("div");
    div.className = "chat-item" + (c.chat === current ? " active" : "");
    const name = displayName(c.chat, c.type, c.name);
    const preview = (c.last_dir === "out" ? "【bot】" : "") + (c.last_text || "");
    const n = unread[c.chat] || 0;
    div.innerHTML = `
      <div class="chat-avatar" style="background:${avatarColor(c.chat)}">${escapeHtml(name.slice(0, 1))}</div>
      <div class="chat-meta">
        <div class="chat-name">${escapeHtml(name)}</div>
        <div class="chat-preview">${escapeHtml(preview)}</div>
      </div>
      <div class="chat-side">
        <span class="chat-time">${c.last_ts ? fmtTime(c.last_ts) : ""}</span>
        ${n ? `<span class="chat-badge">${n > 99 ? "99+" : n}</span>` : ""}
      </div>`;
    div.querySelector(".chat-name").addEventListener("dblclick", () => {
      const input = prompt("重命名会话（留空恢复默认）：", aliases[c.chat] || "");
      if (input === null) return;
      if (input.trim()) aliases[c.chat] = input.trim();
      else delete aliases[c.chat];
      localStorage.setItem("webchat_aliases", JSON.stringify(aliases));
      renderChatList();
      if (c.chat === current) renderChatHead();
    });
    div.addEventListener("click", () => openChat(c.chat));
    list.appendChild(div);
  }
}

/* -------------------------------------------------------------- 消息区 */

async function openChat(chat) {
  const chatInfo = chats.find((c) => c.chat === chat);
  current = chat;
  document.body.classList.add("in-chat");  // 移动端切到对话视图
  unread[chat] = 0;
  updateTitle();
  renderChatList();
  renderChatHead(chatInfo);
  setStatus("");
  currentMsgs = [];
  memberMap = {};
  oldestSeq = null;
  hasMore = false;
  setPendingFile(null);  // 切换会话必须清空附件，防止发错目标
  closeMentionPopup();
  $("#loadMoreWrap").hidden = true;
  await reloadHistory();
  await refreshReplyTarget();
}

function renderChatHead(chatInfo) {
  if (!current) { $("#chatTitle").textContent = "选择一个会话"; $("#chatSub").textContent = ""; $("#renameBtn").hidden = true; return; }
  const type = chatInfo ? chatInfo.type : current.split("_", 1)[0];
  $("#chatTitle").textContent = displayName(current, type, chatInfo && chatInfo.name);
  const typeText = { group: "QQ 群聊", friend: "好友私聊", dms: "群私信", guild: "频道" }[type] || type;
  $("#chatSub").textContent = `${typeText} · ${current}`;
  $("#renameBtn").hidden = false;
}

async function reloadHistory() {
  const data = await api(`/messages?chat=${encodeURIComponent(current)}&limit=100`);
  currentMsgs = data.messages || [];
  rebuildMemberMap();
  hasMore = !!data.has_more;
  if (currentMsgs.length) oldestSeq = currentMsgs[0].seq;
  renderMessages();
  $("#loadMoreWrap").hidden = !hasMore;
}

async function loadMore() {
  if (!current || oldestSeq === null || loadingHistory) return;
  loadingHistory = true;
  const btn = $("#loadMoreBtn");
  btn.disabled = true;
  btn.textContent = "加载中…";
  const area = $("#msgArea");
  const prevHeight = area.scrollHeight;
  const prevTop = area.scrollTop;
  try {
    const data = await api(`/messages?chat=${encodeURIComponent(current)}&limit=100&before=${oldestSeq}`);
    const older = data.messages || [];
    if (older.length) {
      oldestSeq = older[0].seq;
      currentMsgs = older.concat(currentMsgs);
      rebuildMemberMap();
      renderMessages(false);
      // 前插内容后保持视口停在原消息上
      area.scrollTop = area.scrollHeight - prevHeight + prevTop;
    }
    hasMore = !!data.has_more && older.length > 0;
    $("#loadMoreWrap").hidden = !hasMore;
  } finally {
    btn.disabled = false;
    btn.textContent = "加载更早的消息";
    loadingHistory = false;
  }
}

function renderMessages(scrollBottom = true) {
  const list = $("#msgList");
  list.innerHTML = "";
  let lastDay = "";
  for (const m of currentMsgs) {
    const day = fmtDay(m.ts);
    if (day !== lastDay) {
      const divider = document.createElement("div");
      divider.className = "day-divider";
      divider.textContent = day;
      list.appendChild(divider);
      lastDay = day;
    }
    list.appendChild(renderMsg(m));
  }
  if (scrollBottom) scrollBottomIfNear(true);
}

// 消息文本里的 QQ 图片直链（聊天记录/合并转发会把图片展平成 URL 文本）
function extractImageUrls(text) {
  return (String(text).match(/https:\/\/multimedia\.nt\.qq\.com\.cn\/download\?[^\s"']+/g) || []);
}

function renderMsg(m) {
  const out = m.dir === "out";
  const div = document.createElement("div");
  div.className = "msg " + (out ? "out" : "in");
  const who = out ? "bot" : (m.nickname || shortId(m.user));
  const meta = `${escapeHtml(who)} · ${fmtTime(m.ts)}` +
    (m.msg_id ? `<span class="msg-id">#${escapeHtml(shortId(m.msg_id))}</span>` : "");
  const replyFlag = out && m.reply_to ? `<div class="reply-flag">↩ 被动回复</div>` : "";
  div.innerHTML = `
    <div class="msg-avatar" style="background:${avatarColor(out ? "bot" : m.user)}">${out ? "机" : escapeHtml(who.slice(0, 1))}</div>
    <div class="msg-body">
      <div class="msg-meta">${meta}</div>
      <div class="bubble">${replyFlag}${renderText(m.text || "")}</div>
    </div>`;
  // 图片：附件 URL + 文本内嵌 URL 合并去重，经面板代理展示（本地转存优先）
  const urls = [...new Set([...(m.media_urls || []), ...extractImageUrls(m.text || "")])];
  if (urls.length) {
    const mediaBox = document.createElement("div");
    mediaBox.className = "msg-media";
    for (const u of urls) {
      const img = document.createElement("img");
      img.className = "msg-img";
      img.src = `${API}/media?token=${encodeURIComponent(token)}&u=${encodeURIComponent(u)}`;
      img.loading = "lazy";
      img.alt = "[图片]";
      img.title = "点击查看原图";
      img.addEventListener("error", () => {
        const ph = document.createElement("span");
        ph.className = "img-broken";
        ph.textContent = "[图片已过期或无法加载]";
        img.replaceWith(ph);
      });
      img.addEventListener("click", () =>
        window.open(`${API}/media?token=${encodeURIComponent(token)}&u=${encodeURIComponent(u)}`, "_blank"));
      mediaBox.appendChild(img);
    }
    div.querySelector(".msg-body").appendChild(mediaBox);
  }
  // 悬停操作：收到的消息可设为回复目标；bot 消息可复用文本
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  if (!out && m.msg_id) {
    const btn = document.createElement("button");
    btn.textContent = "↩ 回复";
    btn.addEventListener("click", () => {
      setReplyTarget({ msg_id: m.msg_id, user: m.user, nickname: m.nickname, text: m.text });
      setMode("passive");
    });
    actions.appendChild(btn);
  }
  if (out && m.text) {
    const btn = document.createElement("button");
    btn.textContent = "✎ 复用编辑";
    btn.addEventListener("click", () => { $("#input").value = m.text; $("#input").focus(); autosize(); });
    actions.appendChild(btn);
  }
  div.querySelector(".msg-body").appendChild(actions);
  return div;
}

function appendMessage(m) {
  const area = $("#msgArea");
  const nearBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 80;
  const lastDay = currentMsgs.length ? fmtDay(currentMsgs[currentMsgs.length - 1].ts) : "";
  const day = fmtDay(m.ts);
  if (day !== lastDay) {
    const divider = document.createElement("div");
    divider.className = "day-divider";
    divider.textContent = day;
    $("#msgList").appendChild(divider);
  }
  currentMsgs.push(m);
  if (m.dir === "in" && m.user && m.user !== "bot" && m.nickname) {
    memberMap[m.user] = m.nickname;
  }
  $("#msgList").appendChild(renderMsg(m));
  scrollBottomIfNear(nearBottom);
}

function scrollBottomIfNear(force) {
  const area = $("#msgArea");
  if (force) area.scrollTop = area.scrollHeight;
}

function systemLine(text, ok = false) {
  const div = document.createElement("div");
  div.className = "system-line" + (ok ? " ok" : "");
  div.textContent = text;
  $("#msgList").appendChild(div);
  scrollBottomIfNear(true);
}

/* -------------------------------------------------------------- 回复目标 */

async function refreshReplyTarget() {
  if (!current) return;
  const data = await api(`/reply_target?chat=${encodeURIComponent(current)}`);
  if (data.msg) setReplyTarget(data.msg);
  else setReplyTarget(null);
}

function setReplyTarget(target) {
  replyTarget = target;
  updateModeUI();
  if (target) {
    const who = target.nickname || shortId(target.user);
    $("#replyChipText").textContent = `${who}：${(target.text || "").slice(0, 60)}`;
  }
}

function setMode(mode) {
  document.querySelector(`input[name="mode"][value="${mode}"]`).checked = true;
  updateModeUI();
}

function currentMode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

// 回复目标提示条仅在被动模式下有意义
function updateModeUI() {
  $("#replyChip").hidden = currentMode() === "active" || !replyTarget;
}

// 发送按钮状态由内容驱动：有会话且有文本/附件才可点
function updateSendBtn() {
  $("#sendBtn").disabled = sending || !current ||
    (!pendingFile && !$("#input").value.trim());
}

/* -------------------------------------------------------------- 发送 */

function fmtSize(n) {
  return n > 1048576 ? (n / 1048576).toFixed(1) + "MB" : Math.ceil(n / 1024) + "KB";
}

function setPendingFile(file) {
  if (pendingFileUrl) { URL.revokeObjectURL(pendingFileUrl); pendingFileUrl = null; }
  pendingFile = file;
  const chip = $("#attachChip");
  if (!file) { chip.hidden = true; updateSendBtn(); return; }
  const thumb = $("#attachChipThumb");
  if (file.type.startsWith("image/")) {
    pendingFileUrl = URL.createObjectURL(file);
    thumb.src = pendingFileUrl;
    thumb.hidden = false;
  } else {
    thumb.hidden = true;
  }
  $("#attachChipName").textContent = file.name;
  $("#attachChipSize").textContent = fmtSize(file.size);
  chip.hidden = false;
  updateSendBtn();
}

async function sendMedia() {
  if (!pendingFile || !current) return;
  const active = currentMode() === "active";
  let replyMsgId = null;
  if (!active) {
    if (!replyTarget || !replyTarget.msg_id) {
      setStatus("暂无可被动回复的消息，请先等收到消息，或切换为主动发送", "err");
      return;
    }
    replyMsgId = replyTarget.msg_id;
  }
  const fd = new FormData();
  fd.append("chat", current);
  fd.append("file", pendingFile);
  fd.append("caption", $("#input").value.trim());
  fd.append("reply_msg_id", replyMsgId || "");
  fd.append("active", active ? "true" : "false");
  sending = true;
  updateSendBtn();
  setStatus(`上传中（${fmtSize(pendingFile.size)}）…`);
  try {
    const resp = await fetch(API + "/send_media", {
      method: "POST",
      headers: { "X-Webchat-Token": token },
      body: fd,
    });
    if (resp.status === 401) { showTokenOverlay("会话已过期，请重新登录"); return; }
    const data = await resp.json().catch(() => ({}));
    if (data.ok) {
      $("#input").value = "";
      autosize();
      setPendingFile(null);
      setStatus("已发送", "ok");
    } else {
      setStatus(data.error || "发送失败", "err");
      toast(`发送失败：${data.error || "未知错误"}`, "err");
    }
  } catch (err) {
    setStatus("网络错误：" + err.message, "err");
  } finally {
    sending = false;
    updateSendBtn();
  }
}

async function send() {
  if (pendingFile) return sendMedia();
  const input = $("#input");
  const text = input.value.trim();
  if (!text || !current || sending) return;
  const active = currentMode() === "active";
  let replyMsgId = null;
  if (!active) {
    if (!replyTarget || !replyTarget.msg_id) {
      setStatus("暂无可被动回复的消息，请先等收到消息，或切换为主动发送", "err");
      return;
    }
    replyMsgId = replyTarget.msg_id;
  }
  sending = true;
  updateSendBtn();
  try {
    const resp = await api("/send", {
      method: "POST",
      body: JSON.stringify({ chat: current, text, reply_msg_id: replyMsgId, active }),
    });
    if (resp.ok) {
      input.value = "";
      autosize();
      setStatus(ws ? "已发送" : "已发送（WebSocket 未连接，显示可能延迟）", "ok");
      if (!ws) toast("WebSocket 未连接，消息可能延迟显示", "err");
    } else {
      setStatus(resp.error || "发送失败", "err");
      toast(`发送失败：${resp.error || "未知错误"}`, "err");
    }
  } catch (err) {
    if (err.message !== "unauthorized" && err.message !== "forbidden") {
      setStatus("网络错误：" + err.message, "err");
    }
  } finally {
    sending = false;
    updateSendBtn();
    input.focus();
  }
}

/* -------------------------------------------------------------- WebSocket */

function connectWS() {
  if (!token) return;
  // 重复登录时避免叠加旧连接；先摘掉旧 onclose 防止触发重连定时器
  if (ws) {
    ws.onclose = null;
    try { ws.close(); } catch { /* 已关闭则忽略 */ }
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/webchat/ws?token=${encodeURIComponent(token)}`);
  ws.onopen = () => {
    wsRetry = 0;
    setBotStatus(botOnline);
    if (current) { reloadHistory(); }
    fetchChats();
  };
  ws.onmessage = (ev) => {
    let record;
    try { record = JSON.parse(ev.data); } catch { return; }
    handleRecord(record);
  };
  ws.onclose = () => {
    setBotStatus(null);
    const delay = Math.min(1000 * 2 ** wsRetry, 15000);
    wsRetry += 1;
    setTimeout(connectWS, delay);
  };
  ws.onerror = () => ws.close();
}

function handleRecord(record) {
  if (record.type === "hello" || record.type === "status") {
    botOnline = record.bots ? Object.values(record.bots).some(Boolean) : null;
    setBotStatus(botOnline);
    return;
  }
  if (record.type === "chat_name") {
    const c = chats.find((x) => x.chat === record.chat);
    if (c) c.name = record.name;
    renderChatList();
    if (record.chat === current) renderChatHead(chats.find((x) => x.chat === current));
    return;
  }
  if (record.type === "send_error") {
    if (record.chat === current) systemLine(`发送失败：${record.error}`);
    toast(`发送失败：${record.error || record.text || ""}`, "err");
    return;
  }
  if (!record.chat) return;  // 普通消息记录
  const isCurrent = record.chat === current;
  if (isCurrent) {
    appendMessage(record);
  } else {
    unread[record.chat] = (unread[record.chat] || 0) + 1;
    updateTitle();
  }
  // 更新侧栏预览与排序（新会话则整表刷新）
  const existing = chats.find((c) => c.chat === record.chat);
  if (existing) {
    existing.last_text = record.text || "";
    existing.last_ts = record.ts;
    existing.last_dir = record.dir;
    chats.sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));
    renderChatList();
  } else {
    fetchChats();
  }
}

function setBotStatus(online) {
  const dot = $("#botDot");
  const sub = $("#botSub");
  dot.className = "brand-dot" + (online ? " on" : online === false ? " off" : "");
  sub.textContent = online ? "机器人在线" : online === false ? "机器人离线" : "正在连接机器人…";
}

/* -------------------------------------------------------------- 令牌 */

function showTokenOverlay(errText = "") {
  $("#tokenOverlay").hidden = false;
  $("#tokenErr").textContent = errText;
  if (!errText) $("#tokenInput").value = "";
  $("#tokenInput").focus();
}

async function tryLogin(code) {
  const resp = await fetch(API + "/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  const data = await resp.json().catch(() => ({}));
  if (data.ok) {
    token = data.token;  // 仅存内存，刷新即失效
    return true;
  }
  $("#tokenErr").textContent = data.error || "登录失败";
  return false;
}

/* -------------------------------------------------------------- 输入框 */

function autosize() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

/* -------------------------------------------------------------- 启动 */

function bindEvents() {
  $("#search").addEventListener("input", renderChatList);
  // 移动端返回按钮：回到会话列表
  $("#backBtn").addEventListener("click", () => {
    closeMentionPopup();
    document.body.classList.remove("in-chat");
  });
  // 滚动到顶部自动加载更早的历史
  $("#msgArea").addEventListener("scroll", () => {
    const area = $("#msgArea");
    if (area.scrollTop < 80 && hasMore && !loadingHistory && current) loadMore();
  });
  $("#sendBtn").addEventListener("click", send);
  $("#input").addEventListener("input", () => {
    autosize();
    updateSendBtn();
    // @提及：官方接口 <at:openid> 个人提及实测未生效，先下线
    // const pos = e.target.selectionStart;
    // if (pos > 0 && e.target.value[pos - 1] === "@") openMentionPopup();
  });
  // Ctrl+Enter / Cmd+Enter 发送，Enter 换行
  $("#input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      send();
    }
    if (e.key === "Escape") closeMentionPopup();
  });
  // $("#atBtn").addEventListener("click", () => {
  //   if ($("#mentionPopup").hidden) openMentionPopup();
  //   else closeMentionPopup();
  // });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#mentionPopup") &&
        e.target !== $("#input")) closeMentionPopup();
  });
  document.querySelectorAll('input[name="mode"]').forEach((r) =>
    r.addEventListener("change", updateModeUI));
  $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) setPendingFile(file);
    e.target.value = "";
  });
  $("#attachClear").addEventListener("click", () => setPendingFile(null));
  $("#loadMoreBtn").addEventListener("click", loadMore);
  $("#replyClear").addEventListener("click", () => setReplyTarget(null));
  $("#renameBtn").addEventListener("click", () => {
    if (!current) return;
    const input = prompt("重命名会话（留空恢复默认）：", aliases[current] || "");
    if (input === null) return;
    if (input.trim()) aliases[current] = input.trim();
    else delete aliases[current];
    localStorage.setItem("webchat_aliases", JSON.stringify(aliases));
    renderChatList();
    renderChatHead(chats.find((c) => c.chat === current));
  });
  $("#resetToken").addEventListener("click", (e) => {
    e.preventDefault();
    token = "";
    showTokenOverlay();
  });
  $("#tokenSave").addEventListener("click", async () => {
    const value = $("#tokenInput").value.trim();
    if (!value) { $("#tokenErr").textContent = "请输入 6 位动态码"; return; }
    // 登录成功后原地进入面板：页面不能刷新，否则内存会话会被清掉
    if (await tryLogin(value)) {
      $("#tokenOverlay").hidden = true;
      $("#tokenErr").textContent = "";
      await enterPanel();
    }
  });
  $("#tokenInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("#tokenSave").click();
  });
  updateSendBtn();
  updateModeUI();
}

async function boot() {
  bindEvents();
  if (!token) { showTokenOverlay(); return; }
  await enterPanel();
}

// 面板入口：桌面端自动打开最近会话；移动端停留在会话列表
async function enterPanel() {
  try {
    await fetchChats();
    connectWS();
    const desktop = !window.matchMedia("(max-width: 720px)").matches;
    if (chats.length && desktop) openChat(chats[0].chat);
  } catch (err) {
    if (err.message === "unauthorized" || err.message === "forbidden") showTokenOverlay();
    else setStatus("加载会话列表失败：" + err.message, "err");
  }
}

boot();
