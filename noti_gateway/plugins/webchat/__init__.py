"""WebChat —— 网页版 QQ 风格消息面板（noti-gateway 插件版，迁移自 bison2）。

捕获网关收发的全部 QQ 单聊消息，存内存环形缓冲（jsonl 落盘），挂 Web 面板：

    http://<host>:<port>/webchat/    （TOTP 动态码登录）

- 进站：消费 qqws 广播的 C2C_MESSAGE_CREATE（挂 ctx.event_listeners）
- 出站：注册 targets 出站观察者，记录 dispatcher/提醒/面板发出的全部 QQ 消息
- 发送：动态构造 QQBotTarget，复用 targets 的文本发送/分片上传/被动回复实现
  （msg_id+msg_seq，窗口外自动降级主动消息）；网页编辑文本/图片/文件发出
- 图片：消息内 QQ CDN 图经面板代理展示，不受 rkey 时效与 Referer 防盗链影响

与 bison2 版（NoneBot monkey-patch 捕获）的差异：
- 仅单聊（qqws 当前只订阅 C2C）；qqws 扩展群聊事件后在此追加渲染即可
- 视频/音频走 targets 分片通道按文件发送（file_type=4）
- 图片+说明文字拆成「图 + 文」两条消息（targets 的 send 组合行为）

安全：所有 API/WS 需 TOTP 登录签发的会话令牌；带 Origin 的请求校验同源，
挡 CSRF/DNS rebinding；登录限速防爆破；图片代理限 QQ 域名白名单防 SSRF。
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .. import PluginMeta
from ...config import WebchatCfg
from ...models import Notification
from ...targets import QQBotTarget, TargetCfg, register_send_observer
from . import totp as totp_mod
from . import web as webchat_web
from .store import ChatStore

log = logging.getLogger("noti.webchat")

META = PluginMeta(name="WebChat",
                  desc="网页版消息面板：实时查看/发送 QQ 单聊消息（TOTP 登录）",
                  version="1.0.0")

# QQ 图片直链 host 白名单（防止面板代理被当作任意 SSRF 跳板）
MEDIA_HOST_SUFFIXES = (".qq.com", ".qq.com.cn", ".qpic.cn", ".gtimg.cn")
MEDIA_DOWNLOAD_TIMEOUT = 20.0
MEDIA_MAX_BYTES = 30 * 1024 * 1024

_FRIEND_RE = re.compile(r"^friend_(.+)$")

_media_client: httpx.AsyncClient | None = None


def _url_allowed(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("https", "http"):  # 官方附件 url 存在 http 形态
        return False
    host = (parsed.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in MEDIA_HOST_SUFFIXES)


def _media_client_get() -> httpx.AsyncClient:
    global _media_client
    if _media_client is None or _media_client.is_closed:
        _media_client = httpx.AsyncClient(
            timeout=httpx.Timeout(MEDIA_DOWNLOAD_TIMEOUT),
            follow_redirects=True,
            # 不带 Referer：QQ CDN 拒绝非 QQ 站点来源
            headers={"User-Agent": "Mozilla/5.0 (WebChat panel)"},
        )
    return _media_client


async def _fetch_media(url: str) -> tuple[bytes, str] | None:
    """服务端抓取图片（无 Referer），内存中转不落盘；失败返回 None。"""
    if not _url_allowed(url):
        return None
    try:
        resp = await _media_client_get().get(url)
        if resp.status_code != 200 or not resp.content:
            return None
        if len(resp.content) > MEDIA_MAX_BYTES:
            return None
        ct = resp.headers.get("content-type", "")
        if not ct.startswith("image/"):
            ct = "application/octet-stream"
        return resp.content, ct
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 进站渲染

def _event_ts(d: dict) -> int:
    raw = d.get("timestamp")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _render_c2c(d: dict) -> tuple[str, list[str]]:
    """官方 C2C 原始 payload -> 展示文本 + 图片 URL 列表。"""
    parts: list[str] = []
    if d.get("content"):
        parts.append(str(d["content"]))
    urls: list[str] = []
    for att in d.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        if str(att.get("content_type") or "").startswith("image"):
            parts.append("[图片]")
            u = str(att.get("url") or "")
            if u:
                urls.append(u)
    return "".join(parts).strip(), urls


class WebchatPlugin:
    """webchat 插件：进站监听 + 出站观察 + 面板路由挂载，无后台任务。"""

    def __init__(self, ctx: Any, cfg: WebchatCfg) -> None:
        self.ctx = ctx
        self.cfg = cfg
        self.data_dir = Path(cfg.data_dir)
        self.secret = self._resolve_totp_secret()
        self.store = ChatStore(
            max_per_chat=cfg.max_per_chat,
            persist_dir=str(self.data_dir) if cfg.persist else None,
        )
        # 发送凭据：与 qqws 相同的 bot 归属（首次发送时惰性解析）
        self._qq: Any = None
        self._tokens: Any = None

    # ---------------- 凭据 ----------------

    def _credentials(self) -> tuple[Any, Any] | None:
        """与 qqws 相同的 bot 归属解析；无凭据时发送不可用（收消息不受影响）。"""
        if self._tokens is None:
            bot_id, qq = self.ctx.cfg.qqws_bot()
            reg = self.ctx.reg
            cache = reg.token_caches.get(bot_id) if reg is not None else None
            if cache is None and reg is not None:
                cache = reg.token_caches.get("")
            if cache is None or not qq.app_id or not qq.client_secret:
                return None
            self._qq, self._tokens = qq, cache
        return self._qq, self._tokens

    def _make_target(self, openid: str) -> QQBotTarget:
        qq, tokens = self._credentials()  # type: ignore[misc]
        tcfg = TargetCfg(id=f"webchat-{openid[:12]}", type="qq", user_openid=openid,
                         template="{{ title }}")  # 模板取原文，面板发的就是发的
        return QQBotTarget(tcfg, qq, tokens, self.ctx.client)

    # ---------------- 进站 ----------------

    async def on_event(self, type_: str, d: dict) -> None:
        """挂 ctx.event_listeners；qqws 广播的每个 dispatch 事件都到这里。"""
        if type_ != "C2C_MESSAGE_CREATE" or not isinstance(d, dict):
            return
        author = d.get("author") or {}
        openid = str(author.get("user_openid") or "")
        if not openid:
            return
        chat = f"friend_{openid}"
        text, urls = _render_c2c(d)
        nickname = str(author.get("username") or "")
        self.store.add({
            "dir": "in",
            "chat": chat,
            "type": "friend",
            "user": openid,
            "nickname": nickname,
            "msg_id": str(d.get("id") or ""),
            "text": text,
            "media_urls": urls,
            "ts": _event_ts(d),
        })
        if nickname:
            self.store.set_chat_name(chat, nickname)

    # ---------------- 出站观察 ----------------

    def _on_send_observed(self, rec: dict) -> None:
        """targets 出站观察者：QQBotTarget 的每次文本/媒体投递都记录。"""
        scene = rec.get("scene")
        if scene == "c2c":
            chat, ctype = f"friend_{rec.get('scene_id')}", "friend"
        elif scene == "group":
            chat, ctype = f"group_{rec.get('scene_id')}", "group"
        else:
            return
        self.store.add({
            "dir": "out",
            "chat": chat,
            "type": ctype,
            "user": "bot",
            "nickname": "bot",
            "msg_id": str(rec.get("msg_id") or ""),
            "text": str(rec.get("text") or ""),
            "ts": int(time.time()),
        })

    # ---------------- 面板发送 ----------------

    async def _send_message(self, chat: str, text: str, reply_msg_id: str | None) -> dict:
        m = _FRIEND_RE.match(chat)
        if not m:
            return {"ok": False, "error": "当前仅支持好友单聊会话（friend_*）"}
        try:
            target = self._make_target(m.group(1))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"发送凭据不可用: {e}"}
        n = Notification(source="webchat", title=text)
        if reply_msg_id:
            msg = self.store.latest_incoming(chat)
            recv_ts = msg["ts"] if msg and msg.get("msg_id") == reply_msg_id else time.time()
            n.meta["passive"] = {"msg_id": reply_msg_id, "recv_ts": recv_ts}
        res = await target.send(n)
        msg_id = res.detail.removeprefix("msg_id=") if res.ok and res.detail.startswith("msg_id=") else ""
        return {"ok": res.ok, "message_id": msg_id} if res.ok else {"ok": False, "error": res.detail}

    async def _send_media_message(
        self, chat: str, data: bytes, file_name: str, mime: str,
        caption: str, reply_msg_id: str | None,
    ) -> dict:
        m = _FRIEND_RE.match(chat)
        if not m:
            return {"ok": False, "error": "当前仅支持好友单聊会话（friend_*）"}
        try:
            target = self._make_target(m.group(1))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"发送凭据不可用: {e}"}
        media = {"b64": base64.b64encode(data).decode(), "mime": mime, "name": file_name}
        is_image = mime.startswith("image/")
        n = Notification(
            source="webchat",
            title=caption,
            image=media if is_image else None,
            file=None if is_image else media,
        )
        if reply_msg_id:
            msg = self.store.latest_incoming(chat)
            recv_ts = msg["ts"] if msg and msg.get("msg_id") == reply_msg_id else time.time()
            n.meta["passive"] = {"msg_id": reply_msg_id, "recv_ts": recv_ts}
        res = await target.send(n)
        msg_id = res.detail.removeprefix("msg_id=") if res.ok and res.detail.startswith("msg_id=") else ""
        return {"ok": res.ok, "message_id": msg_id} if res.ok else {"ok": False, "error": res.detail}

    # ---------------- 状态与挂载 ----------------

    def _bot_status(self) -> dict[str, bool]:
        info = self.ctx.stats_providers.get("qqws")
        online = bool(info and info().get("state") == "open")
        return {"qq-bot": online}

    def _resolve_totp_secret(self) -> str:
        """配置了用配置的；否则自动生成一份持久到 data_dir，保证重启不变。"""
        if self.cfg.totp_secret:
            return self.cfg.totp_secret
        secret_file = self.data_dir / "webchat_totp_secret.txt"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if secret_file.exists():
                saved = secret_file.read_text(encoding="utf-8").strip()
                if saved:
                    return saved
            generated = totp_mod.generate_secret()
            secret_file.write_text(generated, encoding="utf-8")
            log.warning("[WebChat] 未配置 webchat.totp_secret，已自动生成并保存到 %s，"
                        "首次启用请用它绑定验证器 App：%s", secret_file, generated)
            return generated
        except OSError:
            log.exception("[WebChat] 无法读取或生成 TOTP 密钥，登录功能不可用")
            return ""

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 启动时从持久化历史回填单聊会话的对方昵称
        for chat_info in self.store.summary():
            if chat_info["type"] in ("friend", "dms") and not chat_info["name"]:
                msg = self.store.latest_incoming(chat_info["chat"])
                if msg and msg.get("nickname"):
                    self.store.set_chat_name(chat_info["chat"], msg["nickname"])
        self.ctx.event_listeners.append(self.on_event)
        register_send_observer(self._on_send_observed)
        webchat_web.setup(
            self.ctx.app,
            self.store,
            self.secret,
            self.cfg.session_days,
            self._send_message,
            self._send_media_message,
            self._bot_status,
            _fetch_media,
            _url_allowed,
        )
        self.ctx.stats_providers["webchat"] = lambda: {
            "chats": len(self.store.summary()),
            "totp_secret_file": str(self.data_dir / "webchat_totp_secret.txt")
            if not self.cfg.totp_secret else "(config)",
        }

        async def _close_media_client() -> None:
            global _media_client
            if _media_client is not None and not _media_client.is_closed:
                await _media_client.aclose()
            _media_client = None

        self.ctx.on_shutdown.append(_close_media_client)
        log.info("webchat 面板已挂载 /webchat/（TOTP 登录）")


def setup(ctx: Any) -> None:
    """注册 webchat 插件：面板路由 + 进站监听 + 出站观察。"""
    cfg: WebchatCfg = ctx.cfg.webchat
    if not cfg.enabled:
        log.info("webchat 插件未启用（webchat.enabled=false）")
        return
    WebchatPlugin(ctx, cfg).start()
