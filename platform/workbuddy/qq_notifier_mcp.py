#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 官方机器人通知 MCP 连接器 (stdio)。

发送方式对齐 ZCode 现行方案 (~/.zcode/qq-notify.sh，已实测可用)：
  - 获取 token:  https://bots.qq.com/app/getAppAccessToken
  - 发送消息:    https://api.sgroup.qq.com/v2/users/{openid}/messages
  - 每条消息携带唯一 msg_seq，避免同内容被服务端去重后静默丢弃
  - 「卡片」= Markdown(msg_type 2) + keyboard 回调按钮；按钮点击/文本回复由
    WebSocket 监听服务 (~/.zcode/qq-bot-listener.py，常驻) 推回本机，
    wait_reply=True 可阻塞等待用户在 QQ 里的回答并返回给代理
  - 旧 Ark 模板 23 (msg_type 3) 已弃用但保留兼容：服务端会接收成功，
    但 C2C 客户端经常不渲染 —— 这就是此前「卡片发不出来」的原因

环境变量 (由 mcp.json 注入):
  QQ_BOT_APPID        机器人 appid (必填)
  QQ_BOT_SECRET       机器人 clientSecret (必填)
  QQ_TARGET_OPENID    目标用户的 openid —— 单聊接收方 (必填)
  QQ_NOTIFY_DRY_RUN   设为 1 时只打印、不真正发送 (本地测试用)
  QQ_ANSWER_PING_URL  本地监听服务健康检查 (默认 http://127.0.0.1:39527)
  QQ_LISTENER_DIR     监听服务写应答文件的目录 (默认 ~/.zcode)

暴露的工具:
  notify_me(content)                      -> 纯文本单聊 (msg_type 0)
  notify_me_card(question, options, ...)  -> Markdown 卡片 + 回复按钮；可等待你的回答
  notify_me_markdown(content)             -> 纯 Markdown 消息 (msg_type 2)
  send_qq_message(openid, content)        -> 指定 openid 纯文本
  send_qq_channel_message(g, c, text)     -> 频道子频道文本

通知版式（所有 notify_* 工具与 Hook 共用，见 decorate()）:
  第 1 行  🗂 <会话标题>     <- 取自 workbuddy.db 当前会话，取不到则省略
  第 2 行  <正文>            <- 折叠成一行，超过 100 字符自动截断为 …
"""

import json
import os
import random
import time
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

APPID = os.environ.get("QQ_BOT_APPID", "")
SECRET = os.environ.get("QQ_BOT_SECRET", "")
TARGET_OPENID = os.environ.get("QQ_TARGET_OPENID", "")
TOKEN_URL = os.environ.get("QQ_BOT_TOKEN_URL", "https://bots.qq.com/app/getAppAccessToken")
API_BASE = os.environ.get("QQ_BOT_API_BASE", "https://api.sgroup.qq.com")
DRY_RUN = os.environ.get("QQ_NOTIFY_DRY_RUN", "0") == "1"
PING_URL = os.environ.get("QQ_ANSWER_PING_URL", "http://127.0.0.1:39527")
LISTENER_DIR = os.environ.get(
    "QQ_LISTENER_DIR", os.path.join(os.path.expanduser("~"), ".zcode"))
# 统一通知网关（noti-gateway）：纯文本/Markdown 推送优先走网关（统一配额/持久化/重试），
# 失败自动回退直连 QQ。带按钮的交互卡片不走网关（网关不支持键盘按钮），始终直连。
GATEWAY_URL = os.environ.get("QQ_GATEWAY_URL", "").rstrip("/")
GATEWAY_TOKEN = os.environ.get("QQ_GATEWAY_TOKEN", "")
GATEWAY_SOURCE = os.environ.get("QQ_GATEWAY_SOURCE", "workbuddy")

_token = None
_token_exp = 0

# ---------- 会话上下文（标题 + 正文长度） ----------
# 通知版式约定：首行是「会话标题」，正文压到 100 字符以内，避免 QQ 里刷屏。
SESSION_DB = os.path.expanduser(os.environ.get(
    "QQ_SESSION_DB", "~/.workbuddy/workbuddy.db"))
MAX_BODY_CHARS = 100
MAX_TITLE_CHARS = 30


def clip_text(text, limit=MAX_BODY_CHARS):
    """折叠换行/多余空白后按字符数截断（超长以 … 结尾，总长仍 <= limit）。"""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)].rstrip() + "…"


def session_title(session_id=None):
    """解析当前会话标题。

    优先级：QQ_SESSION_TITLE 环境变量 -> 按 session_id 直查 workbuddy.db
    -> 同项目(cwd)最近活跃的会话。取不到就返回空串（调用方退化为不带标题）。
    """
    override = (os.environ.get("QQ_SESSION_TITLE") or "").strip()
    if override:
        return clip_text(override, MAX_TITLE_CHARS)
    sid = (session_id
           or os.environ.get("CODEBUDDY_SESSION_ID")
           or os.environ.get("CLAUDE_SESSION_ID") or "").strip()
    cwd = (os.environ.get("CODEBUDDY_PROJECT_DIR")
           or os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{SESSION_DB}?mode=ro", uri=True, timeout=2)
        try:
            if sid:
                row = con.execute(
                    "select custom_title, title from sessions "
                    "where id = ? and deleted_at is null", (sid,)).fetchone()
                if row and (row[0] or row[1]):
                    return clip_text(row[0] or row[1], MAX_TITLE_CHARS)
            sql = ("select custom_title, title from sessions "
                   "where deleted_at is null and title is not null and title <> ''")
            args = ()
            if cwd:
                sql += " and cwd = ?"
                args = (cwd,)
            sql += " order by coalesce(last_activity_at, updated_at) desc limit 1"
            row = con.execute(sql, args).fetchone()
            if row and (row[0] or row[1]):
                return clip_text(row[0] or row[1], MAX_TITLE_CHARS)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def decorate(content, session_id=None):
    """统一通知版式：`🗂 会话标题` + 一行 <=100 字符的正文。"""
    body = clip_text(content) or "WorkBuddy 通知"
    title = session_title(session_id)
    return f"🗂 {title}\n{body}" if title else body


def card_header(base="❓ **WorkBuddy 提问**", session_id=None):
    """卡片标题行补上会话标题（取不到标题时原样返回）。"""
    title = session_title(session_id)
    if not title:
        return base
    return f"{base} · 🗂 {title}" if base else f"🗂 {title}"


def get_token():
    """获取并缓存 access_token。"""
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    if not APPID or not SECRET:
        raise RuntimeError("缺少 QQ_BOT_APPID / QQ_BOT_SECRET 环境变量")
    body = json.dumps({"appId": APPID, "clientSecret": SECRET}).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read().decode("utf-8"))
    _token = j["access_token"]
    _token_exp = time.time() + int(j.get("expires_in", 7200))
    return _token


def _msg_seq():
    """每条消息唯一的 msg_seq (int32 安全)。不携带时服务端可能按默认值去重，
    造成「接口返回成功但客户端收不到」。ZCode 现行脚本同样每次生成新 seq。"""
    return (random.randint(0, 32767) << 15) + random.randint(0, 32767) \
        + int(time.time()) % 100000 + 1


def _post(path, payload):
    token = get_token()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + path, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "QQBot " + token,
            "X-Union-Appid": APPID,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"QQ API 错误 HTTP {e.code}: {detail}")


# ---------- 纯文本 (msg_type 0) ----------
def _via_gateway(content):
    """把纯文本通知投递到统一网关。成功返回网关响应文本；未配置或失败返回 None，
    调用方回退直连 QQ。仅适用于发往网关既定目标（TARGET_OPENID）的消息。"""
    if not GATEWAY_URL or not GATEWAY_TOKEN:
        return None
    lines = content.splitlines() or [""]
    title = lines[0].strip()[:100] or "(无标题)"
    body = "\n".join(lines[1:]).strip()
    payload = json.dumps({"title": title, "body": body},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{GATEWAY_URL}/n/{GATEWAY_SOURCE}", data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + GATEWAY_TOKEN,
            "X-Source": GATEWAY_SOURCE,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if 200 <= r.status < 300:
                return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    return None


def send_c2c(openid, content):
    if DRY_RUN:
        return f"[DRY_RUN] C2C -> {openid}: {content}"
    if openid == TARGET_OPENID:
        resp = _via_gateway(content)
        if resp is not None:
            return resp + "  [via gateway]"
        # 网关未配置/失败：回退直连
    payload = {"content": content, "msg_type": 0, "msg_seq": _msg_seq()}
    return _post(f"/v2/users/{openid}/messages", payload)


# ---------- 纯 Markdown (msg_type 2) ----------
def send_markdown(openid, content):
    if DRY_RUN:
        return f"[DRY_RUN] MD -> {openid}: {content}"
    if openid == TARGET_OPENID:
        # 经网关时以纯文本投递（网关 QQ 通道为 msg_type 0），** 等标记原样可见
        resp = _via_gateway(content)
        if resp is not None:
            return resp + "  [via gateway]"
    payload = {"msg_type": 2, "markdown": {"content": content},
               "msg_seq": _msg_seq()}
    return _post(f"/v2/users/{openid}/messages", payload)


# ---------- Markdown 卡片 + 键盘按钮 (ZCode 现行做法) ----------
def build_keyboard(buttons, input_button=None):
    """构造 keyboard 载荷。

    buttons: [(label, visited_label, data)] 回调按钮 (action.type 1)；
             点击不打开链接，事件由本机 WebSocket 监听服务接收。
    input_button: (label, prefix) 可选的自定义输入按钮 (action.type 2)，
             点击后 QQ 弹输入框并以 prefix 预填。
    每行最多 3 个按钮（与实测可用的 ZCode 脚本一致）。
    """
    def cb(idx, label, visited, data, style=1, action_type=1):
        return {
            "id": str(idx),
            "render_data": {"label": label, "visited_label": visited,
                            "style": style},
            "action": {"type": action_type, "data": data,
                       "permission": {"type": 2}},
        }

    rows, row, idx = [], [], 1
    for label, visited, data in buttons:
        row.append(cb(idx, label, visited, data))
        idx += 1
        if len(row) >= 3:
            rows.append({"buttons": row})
            row = []
    if row:
        rows.append({"buttons": row})
    if input_button:
        label, prefix = input_button
        rows.append({"buttons": [cb(idx, label, "请输入", prefix,
                                    style=0, action_type=2)]})
    return {"content": {"rows": rows}}


def send_card(openid, markdown_text, buttons, input_button=None,
              plain_text=None):
    """发送 Markdown 卡片 + 按钮，三级降级：md+按钮 -> 文本+按钮 -> 纯文本。"""
    plain = plain_text or markdown_text
    seq = _msg_seq()
    if DRY_RUN:
        p = {"msg_type": 2, "markdown": {"content": markdown_text},
             "keyboard": build_keyboard(buttons, input_button),
             "msg_seq": seq}
        return f"[DRY_RUN] CARD -> {openid}: {json.dumps(p, ensure_ascii=False)}"
    kb = build_keyboard(buttons, input_button)
    err1 = err2 = ""
    try:
        return _post(f"/v2/users/{openid}/messages",
                     {"msg_type": 2, "markdown": {"content": markdown_text},
                      "keyboard": kb, "msg_seq": seq})
    except RuntimeError as e:
        err1 = str(e)
    try:
        return _post(f"/v2/users/{openid}/messages",
                     {"msg_type": 0, "content": plain, "keyboard": kb,
                      "msg_seq": seq + 1})
    except RuntimeError as e:
        err2 = str(e)
    try:
        return _post(f"/v2/users/{openid}/messages",
                     {"msg_type": 0, "content": plain, "msg_seq": seq + 2})
    except RuntimeError as e:
        raise RuntimeError(f"卡片三级降级均失败 | md+按钮: {err1} | "
                           f"文本+按钮: {err2} | 纯文本: {e}")


# ---------- 等待用户在 QQ 里的回答 ----------
def listener_alive():
    try:
        with urllib.request.urlopen(PING_URL + "/ping", timeout=2) as r:
            return r.read().decode("utf-8").strip() == "pong"
    except Exception:  # noqa: BLE001
        return False


def wait_qq_answer(timeout_s=90, t0_ms=None):
    """轮询监听服务写的最新点击/回复文件，返回 ('按钮'|'文本', 内容) 或 None。

    仅当 t0_ms 之后产生的新事件才算数；依赖 ~/.zcode/qq-bot-listener.py
    在运行（它把 INTERACTION_CREATE / C2C_MESSAGE_CREATE 写到 ~/.zcode/.qq-*-latest）。
    """
    t0 = t0_ms if t0_ms is not None else int(time.time() * 1000)
    if not listener_alive():
        return None
    paths = [(os.path.join(LISTENER_DIR, ".qq-click-latest"), "按钮"),
             (os.path.join(LISTENER_DIR, ".qq-msg-latest"), "文本")]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for path, kind in paths:
            try:
                with open(path, encoding="utf-8") as f:
                    ts, _, val = f.read().partition("\t")
            except Exception:  # noqa: BLE001
                continue
            if not (ts.isdigit() and int(ts) > t0 and val.strip()):
                continue
            val = val.strip()
            if kind == "按钮":
                if val.startswith("answer:"):
                    return ("按钮", val[len("answer:"):])
                return ("按钮", val)
            if val.lower().startswith("reply:"):
                val = val[len("reply:"):].strip()
            if val:
                return ("文本", val)
        time.sleep(2)
    return None


def build_question_card(question, options, header="❓ **WorkBuddy 提问**"):
    """由 question + 选项构造 (markdown 正文, 纯文本正文, 按钮, 输入按钮)。

    header 传 None 时不加默认的「提问」标题（预设通知用它自定义标题）。
    """
    items = [o.strip() for o in options.split(";") if o.strip()] \
        if isinstance(options, str) else [str(o) for o in options]
    now = time.strftime("%H:%M:%S")
    parts = [p for p in (header, f"🕐 {now}") if p]
    parts += ["", question, "", "点按钮选择，或点「✍️ 自定义回答」/ 直接回复消息"]
    md = "\n".join(parts)
    plain = "\n".join(parts[:-1]).replace("**", "")
    buttons = [(it, f"已选：{it}", f"answer:{it}") for it in items]
    return md, plain, buttons, ("✍️ 自定义回答", "reply:")


# ---------- 旧 Ark 模板 23 (已弃用，保留兼容) ----------
def build_ark_question(prompt, title, desc, items, jump_url=""):
    kv = [
        {"key": "#DESC#", "value": desc},
        {"key": "#PROMPT#", "value": prompt},
        {"key": "#TITLE#", "value": title},
        {"key": "#META_URL#", "value": jump_url or "https://qq.com"},
    ]
    if items:
        obj = []
        for i, it in enumerate(items, 1):
            obj.append({"obj_kv": [
                {"key": "name", "value": it},
                {"key": "age", "value": str(i)},
            ]})
        kv.append({"key": "#META_LIST#", "obj": obj})
    return {"msg_type": 3, "ark": {"template_id": 23, "kv": kv},
            "msg_seq": _msg_seq()}


def send_ark(openid, payload):
    """已弃用：Ark 模板 23 服务端会接收成功，但 C2C 客户端经常不渲染。"""
    if DRY_RUN:
        return f"[DRY_RUN] ARK -> {openid}: {json.dumps(payload, ensure_ascii=False)}"
    return _post(f"/v2/users/{openid}/messages", payload)


# ---------- 频道消息 ----------
def send_channel(guild_id, channel_id, content):
    payload = {"content": content, "msg_type": 0}
    if DRY_RUN:
        return f"[DRY_RUN] channel {guild_id}/{channel_id}: {content}"
    return _post(
        f"/v2/guilds/{guild_id}/channels/{channel_id}/messages",
        payload,
    )


mcp = FastMCP("gate-notify")


@mcp.tool()
def notify_me(content: str) -> str:
    """向「目标用户」(QQ_TARGET_OPENID) 发送纯文本单聊通知。

    版式固定为：首行「🗂 会话标题」，正文折叠为一行且不超过 100 字符。
    """
    if not TARGET_OPENID:
        raise RuntimeError("未配置 QQ_TARGET_OPENID，请在 mcp.json 中补充")
    return send_c2c(TARGET_OPENID, decorate(content))


@mcp.tool()
def notify_me_markdown(content: str) -> str:
    """向「目标用户」发送纯 Markdown 消息(msg_type 2)，同样带会话标题、正文 <=100 字符。"""
    if not TARGET_OPENID:
        raise RuntimeError("未配置 QQ_TARGET_OPENID，请在 mcp.json 中补充")
    return send_markdown(TARGET_OPENID, decorate(content))


@mcp.tool()
def notify_me_card(question: str,
                   options: str = "✅ 收到;❌ 没收到",
                   wait_reply: bool = False,
                   wait_seconds: int = 90) -> str:
    """向「目标用户」发送 Markdown 卡片 + 回复按钮（ZCode 现行做法，客户端可渲染）。

    question 为问题正文（自动带会话标题、正文 <=100 字符）；options 用分号分隔按钮选项
    （如 '方案A;方案B;方案C'），另自动附「✍️ 自定义回答」输入按钮，用户也可直接回复消息。
    wait_reply=True 时阻塞等待用户点按钮或回复文本（需本机 qq-bot-listener 在运行），
    返回值中包含用户的回答，供代理直接使用；超时则如实说明。
    """
    if not TARGET_OPENID:
        raise RuntimeError("未配置 QQ_TARGET_OPENID，请在 mcp.json 中补充")
    md, plain, buttons, input_button = build_question_card(
        clip_text(question) or "你收到了吗？", options, card_header())
    t0 = int(time.time() * 1000)
    resp = send_card(TARGET_OPENID, md, buttons, input_button, plain_text=plain)
    if not wait_reply:
        return resp
    if DRY_RUN:
        return resp + "\n[DRY_RUN] 跳过等待回答"
    if not listener_alive():
        return resp + "\n⚠️ 本机监听服务未运行，按钮点击无法回传，请让用户在对话里继续输入"
    ans = wait_qq_answer(wait_seconds, t0_ms=t0)
    if ans:
        return resp + f"\n👉 用户回答（{ans[0]}）: {ans[1]}"
    return resp + f"\n⏳ 等待 {wait_seconds}s 未见回答（用户可能不在）"


@mcp.tool()
def send_qq_message(recipient_openid: str, content: str) -> str:
    """向指定 QQ 用户发送纯文本单聊(C2C)消息。"""
    return send_c2c(recipient_openid, content)


@mcp.tool()
def send_qq_channel_message(guild_id: str, channel_id: str, content: str) -> str:
    """向 QQ 频道(群组)的某个子频道发送纯文本消息。"""
    return send_channel(guild_id, channel_id, content)


if __name__ == "__main__":
    mcp.run()
