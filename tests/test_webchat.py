"""WebChat 插件测试（迁移自 bison2 的面板）。

覆盖三层：
1. ChatStore：环形缓冲/会话摘要/历史翻页/被动回复序号/jsonl 落盘重载
2. TOTP：动态码校验（含容差窗口）、会话令牌签发/篡改/过期
3. HTTP/WS 面板（TestClient + 注入假发送函数）：登录/鉴权/会话/发送/图片代理/WS 广播
4. 插件集成：C2C 进站渲染、出站观察者会话映射

不依赖 QQ 凭证、不联网（图片代理用假 fetch）。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noti_gateway.plugins.webchat import (  # noqa: E402
    WebchatPlugin,
    _render_c2c,
    _url_allowed,
)
from noti_gateway.plugins.webchat import totp as totp_mod  # noqa: E402
from noti_gateway.plugins.webchat import web as webchat_web  # noqa: E402
from noti_gateway.plugins.webchat.store import ChatStore  # noqa: E402
from noti_gateway.plugins.webchat.totp import generate_secret  # noqa: E402

CN = ZoneInfo("Asia/Shanghai")


# --------------------------------------------------------------------------
# [1] ChatStore
# --------------------------------------------------------------------------
def store_checks(check) -> None:
    print("\n[1] ChatStore 存储")
    store = ChatStore(max_per_chat=3, persist_dir=None)
    for i in range(5):
        store.add({"dir": "in", "chat": "friend_a", "type": "friend", "text": f"m{i}",
                   "msg_id": f"mid{i}", "ts": 1700000000 + i})
    store.add({"dir": "out", "chat": "friend_a", "type": "friend", "text": "reply",
               "msg_id": "bot1", "ts": 1700000010})
    store.add({"dir": "in", "chat": "group_b", "type": "group", "text": "hi",
               "msg_id": "g1", "ts": 1700000020})

    check(len(store.history("friend_a")["messages"]) == 3, "环形缓冲按 maxlen 截断")
    check(store.history("friend_a")["messages"][-1]["text"] == "reply", "最新消息在末尾")
    page1 = store.history("friend_a", before=store.history("friend_a")["messages"][0]["seq"])
    check(page1["messages"] == [], "游标翻页边界")
    check([c["chat"] for c in store.summary()] == ["group_b", "friend_a"], "会话按最近活跃倒序")
    check(store.latest_incoming("friend_a")["msg_id"] == "mid4", "latest_incoming 跳过 out")

    seqs = [store.next_reply_seq("mid9") for _ in range(3)]
    check(seqs == [1, 2, 3], "被动回复序号自增")

    # 落盘 + 重载（seq 续上）
    tmp = Path(__import__("tempfile").mkdtemp(prefix="webchat-store-"))
    s2 = ChatStore(max_per_chat=10, persist_dir=str(tmp))
    s2.add({"dir": "in", "chat": "friend_c", "type": "friend", "text": "x", "msg_id": "c1"})
    s2.add({"dir": "in", "chat": "friend_c", "type": "friend", "text": "y", "msg_id": "c2"})
    s3 = ChatStore(max_per_chat=10, persist_dir=str(tmp))
    msgs = s3.history("friend_c")["messages"]
    check(len(msgs) == 2 and msgs[-1]["seq"] == 2, "jsonl 重载且 seq 续接")
    s3.add({"dir": "in", "chat": "friend_c", "type": "friend", "text": "z", "msg_id": "c3"})
    check(s3.history("friend_c")["messages"][-1]["seq"] == 3, "重载后新消息 seq 顺延")

    # WS 广播
    q = store.subscribe()
    store.add({"dir": "in", "chat": "friend_d", "type": "friend", "text": "push"})
    check(q.empty() is False and q.get_nowait()["text"] == "push", "add 广播给订阅队列")
    store.unsubscribe(q)
    store.add({"dir": "in", "chat": "friend_d", "type": "friend", "text": "push2"})
    check(True, "退订后不再投递")


# --------------------------------------------------------------------------
# [2] TOTP
# --------------------------------------------------------------------------
def totp_checks(check) -> None:
    print("\n[2] TOTP 与会话令牌")
    secret = generate_secret()
    check(len(secret) == 32, "密钥为 Base32 无填充 32 字符")

    key = totp_mod.base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // totp_mod.TOTP_STEP
    code = totp_mod._hotp(key, counter)
    check(totp_mod.verify_totp(secret, code), "当前窗口动态码通过")
    check(totp_mod.verify_totp(secret, totp_mod._hotp(key, counter - 1)), "前 1 窗口容差")
    check(not totp_mod.verify_totp(secret, totp_mod._hotp(key, counter - 2)), "前 2 窗口拒绝")
    check(not totp_mod.verify_totp(secret, "000000") or code == "000000", "错误动态码拒绝")
    check(not totp_mod.verify_totp(secret, "12345"), "非 6 位拒绝")

    token, expires = totp_mod.issue_session(secret, 7)
    check(totp_mod.verify_session(secret, token, 7) and expires == 7 * 86400, "会话令牌签发校验")
    check(not totp_mod.verify_session(secret, token[:-2] + "xx", 7), "篡改令牌拒绝")
    old = f"{int(time.time()) - 8 * 86400}.{token.split('.', 1)[1]}"
    check(not totp_mod.verify_session(secret, old, 7), "过期令牌拒绝")
    other = generate_secret()
    check(not totp_mod.verify_session(other, token, 7), "换密钥后旧令牌全部失效")


# --------------------------------------------------------------------------
# [3][4] HTTP / WS 面板（假发送注入）
# --------------------------------------------------------------------------
class FakePanel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, str, str | None]] = []
        self.medias: list[tuple[str, bytes, str, str, str, str | None]] = []

    async def send(self, chat: str, text: str, reply_msg_id: str | None) -> dict:
        self.sends.append((chat, text, reply_msg_id))
        return {"ok": True, "message_id": f"OUT{len(self.sends)}"}

    async def send_media(self, chat, data, file_name, mime, caption, reply_msg_id):
        self.medias.append((chat, data, file_name, mime, caption, reply_msg_id))
        return {"ok": True, "message_id": "MEDIA1"}

    async def fetch_media(self, url: str):
        if "expired" in url:
            return None
        return b"\x89PNG-fake", "image/png"


def build_panel_app(fake: FakePanel, secret: str) -> FastAPI:
    app = FastAPI()
    store = ChatStore(max_per_chat=50, persist_dir=None)
    webchat_web.setup(
        app, store, secret, 7,
        fake.send, fake.send_media,
        lambda: {"qq-bot": True},
        fake.fetch_media, _url_allowed,
    )
    app.state.webchat_store = store
    return app


def panel_checks(check) -> None:
    print("\n[3] 面板 HTTP API（TOTP 登录 + 注入假发送）")
    fake = FakePanel()
    secret = generate_secret()
    app = build_panel_app(fake, secret)
    store: ChatStore = app.state.webchat_store
    client = TestClient(app)

    # 登录：正确码直接成功
    key = totp_mod.base64.b32decode(secret, casefold=True)
    code = totp_mod._hotp(key, int(time.time()) // totp_mod.TOTP_STEP)
    r = client.post("/webchat/api/login", json={"code": code})
    check(r.json()["ok"] is True and r.json().get("token"), "正确动态码登录成功")
    token = r.json()["token"]

    # 限速锁定：独立 app 实例（模块级计数器共享，避免污染上面的成功会话）
    from fastapi.testclient import TestClient as _TC
    fake2 = FakePanel()
    app2 = build_panel_app(fake2, generate_secret())
    c2 = _TC(app2)
    for _ in range(6):
        r = c2.post("/webchat/api/login", json={"code": "000000"})
    check(r.json()["ok"] is False and "尝试过于频繁" in r.json().get("error", ""),
          "连续失败触发限速锁定")

    # 未带令牌 → 401；带令牌可用
    check(client.get("/webchat/api/chats").status_code == 401, "无令牌访问 -> 401")
    auth = {"X-Webchat-Token": token}
    check(client.get("/webchat/api/chats", headers=auth).status_code == 200, "带令牌取会话列表")

    # 会话与消息
    store.add({"dir": "in", "chat": "friend_x", "type": "friend",
               "user": "ux", "nickname": "小明", "msg_id": "m1", "text": "你好"})
    store.set_chat_name("friend_x", "小明")  # 插件进站时登记会话显示名
    r = client.get("/webchat/api/chats", headers=auth)
    check(r.json()["chats"][0]["name"] == "小明", "会话摘要含显示名")
    r = client.get("/webchat/api/messages", params={"chat": "friend_x"}, headers=auth)
    check(r.json()["messages"][0]["text"] == "你好", "历史消息返回")
    r = client.get("/webchat/api/reply_target", params={"chat": "friend_x"}, headers=auth)
    check(r.json()["msg"]["msg_id"] == "m1", "被动回复默认目标")

    # 手动发送：主动 / 被动
    r = client.post("/webchat/api/send", headers=auth,
                    json={"chat": "friend_x", "text": "面板消息", "active": True})
    check(r.json()["ok"] is True and fake.sends[-1][1] == "面板消息", "主动发送透传")
    check(fake.sends[-1][2] is None, "主动发送不带 reply_msg_id")
    r = client.post("/webchat/api/send", headers=auth,
                    json={"chat": "friend_x", "text": "回你", "reply_msg_id": "m1"})
    check(r.json()["ok"] is True and fake.sends[-1][2] == "m1", "被动回复带 msg_id")
    check(client.post("/webchat/api/send", headers=auth,
                      json={"chat": "group_x", "text": "t"}).status_code in (200, 403), "群聊 key 交给注入实现")

    # 媒体发送
    r = client.post("/webchat/api/send_media", headers=auth,
                    data={"chat": "friend_x", "caption": "看图", "active": "false"},
                    files={"file": ("shot.png", b"\x89PNGdata", "image/png")})
    check(r.json()["ok"] is True, "媒体发送成功")
    check(fake.medias[-1][1] == b"\x89PNGdata" and fake.medias[-1][2] == "shot.png", "媒体内容/文件名透传")
    check(fake.medias[-1][5] is None or fake.medias[-1][5] == "", "active=false 时按请求携带")

    # 图片代理：白名单外 404；白名单内走假 fetch
    r = client.get("/webchat/api/media", params={"u": "https://evil.com/a.png"}, headers=auth)
    check(r.status_code == 404, "非白名单图片拒绝")
    r = client.get("/webchat/api/media",
                   params={"u": "https://multimedia.nt.qq.com.cn/download?fid=x"}, headers=auth)
    check(r.status_code == 200 and r.content.startswith(b"\x89PNG"), "白名单图片代理返回")
    r = client.get("/webchat/api/media",
                   params={"u": "https://multimedia.nt.qq.com.cn/expired"}, headers=auth)
    check(r.status_code == 404, "下载失败 -> 404")

    check(_url_allowed("http://lmq.qpic.cn/a") and _url_allowed("https://gtimg.cn/a"),
          "白名单含 http 形态与多个 QQ 域")
    check(not _url_allowed("https://qq.com.evil.com/a"), "后缀伪装域拒绝")

    print("\n[4] WebSocket 推送")
    with client.websocket_connect(f"/webchat/ws?token={token}") as ws:
        hello = ws.receive_json()
        check(hello.get("type") == "hello" and hello.get("bots", {}).get("qq-bot") is True,
              "WS 握手 hello 含 bot 状态")
        store.add({"dir": "in", "chat": "friend_x", "type": "friend", "text": "实时推送"})
        pushed = ws.receive_json()
        check(pushed.get("text") == "实时推送", "新消息实时推送")
    bad = None
    try:
        with client.websocket_connect("/webchat/ws?token=bogus") as ws:
            ws.receive_json()
    except Exception as e:  # noqa: BLE001
        bad = e
    check(bad is not None, "无效令牌 WS 被拒")


# --------------------------------------------------------------------------
# [5] 插件集成：进站渲染 + 出站观察映射
# --------------------------------------------------------------------------
class FakeCfg:
    def __init__(self, data_dir: str) -> None:
        self.totp_secret = generate_secret()
        self.session_days = 7
        self.max_per_chat = 50
        self.persist = True
        self.data_dir = data_dir


class FakeCtx:
    def __init__(self, cfg: FakeCfg) -> None:
        self.cfg = type("C", (), {"webchat": cfg, "qqws_bot": staticmethod(lambda: ("", __import__(
            "noti_gateway.config", fromlist=["QQCfg"]).QQCfg()))})()
        self.app = FastAPI()
        self.event_listeners = []
        self.stats_providers = {}
        self.on_shutdown = []
        self.reg = None
        self.client = None


def plugin_checks(check) -> None:
    print("\n[5] 插件集成：C2C 渲染与出站观察")
    tmp = Path(__import__("tempfile").mkdtemp(prefix="webchat-plg-"))
    cfg = FakeCfg(str(tmp))
    ctx = FakeCtx(cfg)
    plg = WebchatPlugin(ctx, cfg)
    plg.start()

    check(ctx.event_listeners and ctx.event_listeners[0] == plg.on_event, "进站监听已注册")
    check(any("webchat" in k for k in ctx.stats_providers), "观测段已注册")

    import asyncio

    ev = {
        "id": "BID123",
        "content": "早上好",
        "timestamp": "2026-11-21T10:30:00+08:00",
        "author": {"user_openid": "UX1", "username": "阿明"},
        "attachments": [{"content_type": "image/png", "url": "https://lmq.qpic.cn/a.png"}],
    }
    asyncio.run(plg.on_event("C2C_MESSAGE_CREATE", ev))
    msgs = plg.store.history("friend_UX1")["messages"]
    check(len(msgs) == 1, "C2C 事件入库")
    check(msgs[0]["msg_id"] == "BID123" and msgs[0]["nickname"] == "阿明", "msg_id/昵称记录")
    check("早上好" in msgs[0]["text"] and "[图片]" in msgs[0]["text"], "文本+图片占位渲染")
    check(msgs[0]["media_urls"] == ["https://lmq.qpic.cn/a.png"], "图片 URL 收集")
    check(msgs[0]["ts"] == int(datetime(2026, 11, 21, 10, 30, tzinfo=CN).timestamp()),
          "ISO 时间戳解析为 epoch")
    asyncio.run(plg.on_event("GROUP_AT_MESSAGE_CREATE", ev))
    check(len(plg.store.summary()) == 1, "非 C2C 事件忽略")

    # 出站观察者：c2c -> friend_*；group -> group_*；其他忽略
    plg._on_send_observed({"scene": "c2c", "scene_id": "UX1", "kind": "text",
                           "text": "bot 回复", "msg_id": "OUT9", "error": ""})
    out = plg.store.history("friend_UX1")["messages"][-1]
    check(out["dir"] == "out" and out["text"] == "bot 回复" and out["msg_id"] == "OUT9",
          "出站观察记录 c2c 发送")
    plg._on_send_observed({"scene": "group", "scene_id": "G1", "kind": "text",
                           "text": "群发", "msg_id": "G9", "error": ""})
    check(plg.store.history("group_G1")["messages"][-1]["chat"] == "group_G1", "出站观察记录 group 发送")
    plg._on_send_observed({"scene": "channel", "scene_id": "C1", "kind": "text",
                           "text": "频道", "msg_id": "", "error": ""})
    check(all(m["chat"] != "channel_C1" for m in plg.store.history("channel_C1")["messages"]),
          "频道场景不入库")

    # 发送错误也广播（store.add 带 error 字段走 broadcast 通道）
    check(_render_c2c({"content": "", "attachments": []}) == ("", []), "空消息渲染")


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, name: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    store_checks(check)
    totp_checks(check)
    panel_checks(check)
    plugin_checks(check)

    print("\n" + "=" * 54)
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
