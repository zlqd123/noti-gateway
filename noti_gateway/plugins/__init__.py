"""极简插件机制：一个功能一个模块，避免网关核心随功能增长继续膨胀。

分层与借鉴（NoneBot / Koishi / AstrBot 的机制，无 AI）::

    qqws        唯一拥有 QQ WS 长连接的插件：连接/心跳/重连/Resume，
                收到事件后广播给 ctx.event_listeners，并登记 msg_id 供被动回复
    其他插件    纯消费者：chatdownload 订阅事件做归档；reminders 等用
                声明式指令（ctx.command）做交互，回复由框架统一发送

插件通过 ctx 注册::

    @ctx.command("提醒", desc=..., hint=..., usage=..., priority=50)
    async def cmd_remind(args, event) -> str | None: ...
        声明式指令：按「前缀后的第一个词」匹配，args 为剩余文本；
        args 为空或 帮助/help 时自动回 usage；返回 str 由框架自动发送
        （带被动回复参数），返回 None = 本条不回复
    ctx.event_listeners.append(fn)          async (type, event) -> None，全量网关事件
    ctx.notification_middleware.append(fn)  async (Notification) -> Notification|None，
                                            在 submit 入队前改写/丢弃通知（None=丢弃）
    ctx.schedule(name, fn, cron=..., interval=...)   框架级定时任务服务
    ctx.sessions.open(openid, handler, state)        多轮会话（got 式，见 SessionManager）
    ctx.on_startup / on_shutdown           异步启停钩子
    ctx.health_providers / stats_providers 向 /_health、/_stats 贡献自己的段

指令前缀（框架级约定）：``#`` ``/`` ``@`` ``＠``（全角）。``#帮助``（help/?）由框架
按已注册指令自动生成总览；``#<指令名> 帮助`` 由框架回该指令的 usage 详情。
会话进行中时，用户的下一条消息（含不带前缀的）优先交给会话处理器，发「取消/算了」退出。
不带前缀且无会话的普通消息只进 event_listeners（归档），永远不会触发指令。

回复链路：所有指令/会话回复经 Dispatcher.submit 进持久化队列（dedup=False）；
若事件 msg_id 在被动窗口内，自动带 msg_id/msg_seq 走 QQ 被动回复，不占主动配额。

插件元信息：模块可暴露 ``META = PluginMeta(name, desc, version)``，加载时记录，
``#插件`` 指令与 /_stats 的 plugins 段展示。
启用清单 config.yaml 的 ``plugins``（缺省 ["qqws", "chatdownload", "reminders"]，
顺序即事件分发顺序：归档先于指令）。下划线开头的模块是内部助手，不作为插件加载。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Awaitable, Callable

from ._cron import cron_next

log = logging.getLogger("noti.plugins")

# QQ 单聊指令前缀（中英文 @ 都支持；注意不是 QQ 群里的 @某人）
COMMAND_PREFIXES = ("#", "/", "＠", "@")
# 框架级帮助指令（前缀后命中即回指令总览，不进任何插件）
HELP_WORDS = ("帮助", "help", "?", "？")
# 会话退出词
SESSION_CANCEL_WORDS = ("取消", "算了", "退出", "退出会话")

# 指令处理器签名：(前缀后的第一个词之外的文本, 原始事件) -> 回复文本；None = 本条不回复
C2cHandler = Callable[[str, dict], Awaitable[str | None]]
# 事件监听者签名：(事件类型, 原始事件) -> None，收全部网关 dispatch 事件
EventListener = Callable[[str, dict], Awaitable[None]]
# 通知中间件签名：可改写通知；返回 None = 丢弃
NotifyMiddleware = Callable[[Any], Awaitable[Any | None]]


@dataclass
class PluginMeta:
    """插件自述（模块级 META = PluginMeta(...)），用于 #插件 指令与 /_stats。"""

    name: str
    desc: str = ""
    version: str = "0.1.0"


@dataclass
class CommandSpec:
    """一条声明式指令。name 为前缀后的第一个词；handler(args, event) -> 回复文本。"""

    name: str
    desc: str = ""
    hint: str = ""          # 一行用法提示（出现在 #帮助 总览里）
    usage: str = ""         # 多行详情（#<name> 帮助 或 #<name> 空参数时返回）
    priority: int = 50      # 小者优先（同名词冲突时）
    handler: C2cHandler = None


# --------------------------------------------------------------------------
# 多轮会话（got 式）：会话进行中接管用户下一条消息（无论是否带前缀）
# --------------------------------------------------------------------------
class Pending:
    """指令/会话处理器返回它 = 向用户提问并等待下一条输入（可携带新状态）。"""

    def __init__(self, prompt: str, state: dict | None = None) -> None:
        self.prompt = prompt
        self.state = state or {}


class Session:
    def __init__(self, openid: str, handler: Callable, state: dict) -> None:
        self.openid = openid
        self.handler = handler      # async (session, text, event) -> str | Pending | None
        self.state = state
        self.created = time.time()
        self.last = self.created

    @property
    def expired(self) -> bool:
        return time.time() - self.last > 300  # 5 分钟无输入自动失效


class SessionManager:
    """每用户同时最多一个会话（key = openid）。纯内存，重启即清空。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def open(self, openid: str, handler: Callable, state: dict | None = None) -> None:
        """开启（或覆盖）该用户的会话。"""
        self._sessions[openid] = Session(openid, handler, dict(state or {}))

    def close(self, openid: str) -> None:
        self._sessions.pop(openid, None)

    def has(self, openid: str) -> bool:
        s = self._sessions.get(openid)
        return s is not None and not s.expired

    def sweep(self) -> int:
        for k in [k for k, s in self._sessions.items() if s.expired]:
            self._sessions.pop(k, None)
        return len(self._sessions)

    async def feed(self, openid: str, text: str, event: dict) -> tuple[bool, str | None]:
        """把一条消息喂给该用户的会话。

        返回 (是否接管, 回复文本)。无会话返回 (False, None)；会话中的「取消」类词退出。
        """
        s = self._sessions.get(openid)
        if s is None or s.expired:
            if s is not None:
                self._sessions.pop(openid, None)  # 过期清理
            return False, None
        s.last = time.time()
        t = (text or "").strip()
        if t in SESSION_CANCEL_WORDS:
            self._sessions.pop(openid, None)
            return True, "已退出当前会话"
        try:
            r = await s.handler(s, t, event)
        except Exception:
            log.exception("会话处理器异常 openid=%s", openid)
            self._sessions.pop(openid, None)
            return True, "会话出错，已退出"
        if isinstance(r, Pending):
            s.state.update(r.state or {})
            return True, r.prompt
        self._sessions.pop(openid, None)  # 字符串/None 都视为结束
        return True, (r if isinstance(r, str) else None)


# --------------------------------------------------------------------------
# 框架级定时任务服务：cron 或固定间隔，插件 setup 时注册，startup 后运行
# --------------------------------------------------------------------------
@dataclass
class _Job:
    name: str
    fn: Callable[[], Awaitable[None]]
    cron: str | None = None
    interval: float | None = None
    next_at: float = 0.0


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[_Job] = []
        self._task: asyncio.Task | None = None

    def add(self, name: str, fn: Callable[[], Awaitable[None]],
            cron: str | None = None, interval: float | None = None) -> None:
        if (cron is None) == (interval is None):
            raise ValueError(f"定时任务 {name!r} 需要且只需 cron 或 interval 之一")
        if interval is not None and interval < 0.1:
            raise ValueError(f"定时任务 {name!r} 的 interval 最小 0.1s")
        self._jobs.append(_Job(name=name, fn=fn, cron=cron, interval=interval,
                               next_at=time.time() + (interval or 0)))
        log.info("schedule: %s (%s)", name, cron or f"every {interval}s")

    def has_jobs(self) -> bool:
        return bool(self._jobs)

    async def start(self) -> None:
        if self._task is None and self._jobs:
            for j in self._jobs:
                if j.cron:
                    j.next_at = cron_next(j.cron, time.time())
            self._task = asyncio.create_task(self._run(), name="plugin-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            now = time.time()
            for j in self._jobs:
                if now < j.next_at:
                    continue
                try:
                    await j.fn()
                except Exception:
                    log.exception("定时任务 %s 执行失败", j.name)
                try:
                    j.next_at = cron_next(j.cron, now) if j.cron else now + (j.interval or 1)
                except ValueError as e:
                    log.error("定时任务 %s 的 cron 无下一次，已移除: %s", j.name, e)
                    j.next_at = float("inf")
            # 自适应睡眠：下一 job 多久到点就睡多久（夹在 0.05~1s），不空转也不迟钝
            due = min((j.next_at for j in self._jobs), default=now + 1.0)
            await asyncio.sleep(min(1.0, max(0.05, due - time.time())))


# --------------------------------------------------------------------------
# PluginCtx：传给每个插件 setup() 的上下文
# --------------------------------------------------------------------------
@dataclass
class PluginCtx:
    app: Any                      # FastAPI 实例（插件用 add_api_route 挂端点）
    cfg: Any                      # Config（插件读自己的配置段）
    db_path: str                  # SQLite 路径（插件可建自己的表）
    hub: Any                      # Hub（向 SSE /stream 广播插件事件）
    client: Any                   # 共享 httpx.AsyncClient
    disp: Any = None              # Dispatcher（server 建好 Dispatcher 后回填）
    reg: Any = None               # Registry（token_caches 等出站资源）
    store: Any = None             # Store（队列统计等，框架指令用）

    on_startup: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    on_shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    event_listeners: list[EventListener] = field(default_factory=list)
    notification_middleware: list[NotifyMiddleware] = field(default_factory=list)
    health_providers: dict[str, Callable[[], bool]] = field(default_factory=dict)
    stats_providers: dict[str, Callable[[], dict]] = field(default_factory=dict)
    plugin_metas: list[dict] = field(default_factory=list)

    start_ts: float = field(default_factory=time.time)
    sessions: SessionManager = field(default_factory=SessionManager)
    scheduler: Scheduler = field(default_factory=Scheduler)

    # 被动回复窗口：msg_id -> (openid, 收到时刻)；窗口内回复自动带 msg_id
    _PASSIVE_WINDOW = 240.0
    _recent_msgs: "OrderedDict[str, tuple[str, float]]" = field(
        default_factory=lambda: OrderedDict(), init=False, repr=False)

    def __post_init__(self) -> None:
        self._commands: list[CommandSpec] = []

    # ---------------- 指令注册与匹配 ----------------

    def command(self, name: str, desc: str = "", hint: str = "",
                usage: str = "", priority: int = 50, handler: C2cHandler | None = None):
        """声明式指令注册：@ctx.command("提醒", hint=...) 装饰器用法，
        或 ctx.command("提醒", handler=fn) 直接注册。"""
        if handler is not None:
            self.add_command(name, handler, desc=desc, hint=hint, usage=usage,
                             priority=priority)
            return handler

        def deco(fn: C2cHandler) -> C2cHandler:
            self.add_command(name, fn, desc=desc, hint=hint, usage=usage, priority=priority)
            return fn
        return deco

    def add_command(self, name: str, handler: C2cHandler, desc: str = "",
                    hint: str = "", usage: str = "", priority: int = 50) -> None:
        for s in self._commands:
            if s.name == name:
                raise ValueError(f"指令重复注册: {name}")
        self._commands.append(CommandSpec(name=name, desc=desc, hint=hint,
                                          usage=usage, priority=priority, handler=handler))
        self._commands.sort(key=lambda s: s.priority)

    def help_text(self) -> str:
        """#帮助 的总览：按 priority 顺序列出全部已注册指令。"""
        lines = ["🤖 指令总览（前缀 # / / @ ＠ 均可）："]
        for s in self._commands:
            head = f"#{s.name}" + (f" {s.hint}" if s.hint else "")
            tail = f" —— {s.desc}" if s.desc else ""
            lines.append(head + tail)
        return "\n".join(lines)

    def remember_msg(self, msg_id: str | None, openid: str) -> None:
        """qqws 收到单聊消息时登记，供被动回复窗口判断。"""
        if not msg_id:
            return
        self._recent_msgs[msg_id] = (openid, time.time())
        while len(self._recent_msgs) > 256:
            self._recent_msgs.popitem(last=False)
        now = time.time()
        for k in [k for k, (_, ts) in self._recent_msgs.items() if now - ts > self._PASSIVE_WINDOW]:
            self._recent_msgs.pop(k, None)

    def _passive_meta(self, event: dict) -> dict | None:
        """事件 msg_id 在被动窗口内 -> {'msg_id', 'openid'}，否则 None（转主动消息）。"""
        msg_id = event.get("id")
        if not msg_id:
            return None
        rec = self._recent_msgs.get(msg_id)
        if rec is None or time.time() - rec[1] > self._PASSIVE_WINDOW:
            return None
        return {"msg_id": msg_id, "openid": rec[0], "recv_ts": rec[1]}

    async def _send_reply(self, reply: str, event: dict, title: str = "助手") -> None:
        """统一回复发送：经 Dispatcher 入队（dedup=False），可带被动回复参数。"""
        from ..dispatcher import SubmitError
        from ..models import Notification

        meta: dict[str, Any] = {"via": "cmd-reply"}
        passive = self._passive_meta(event)
        if passive:
            meta["passive"] = passive
        n = Notification(source="bot", level="info", title=title, body=reply, meta=meta)
        try:
            await self.disp.submit(n, wait=False, dedup=False)
        except SubmitError as e:
            log.warning("指令回复投递失败: %s", e)

    # ---------------- 事件分发 ----------------

    async def dispatch_event(self, type_: str, event: dict) -> None:
        """连接插件（qqws）的广播入口：监听者 -> 多轮会话 -> 指令总线。

        单个监听者/指令异常只记日志，绝不影响其余订阅者与连接本身。
        """
        for h in self.event_listeners:
            try:
                await h(type_, event)
            except Exception:
                log.exception("事件监听者 %r 异常 (type=%s)", getattr(h, "__qualname__", h), type_)

        if type_ != "C2C_MESSAGE_CREATE":
            return
        openid = str((event.get("author") or {}).get("user_openid") or "")
        text = (event.get("content") or "").strip()
        if not text:
            return

        # 0) 多轮会话优先：进行中的会话接管任何下一条消息
        handled, reply = await self.sessions.feed(openid, text, event)
        if handled:
            if reply:
                await self._send_reply(reply, event, title="会话")
            return

        # 1) 前缀门
        for prefix in COMMAND_PREFIXES:
            if not text.startswith(prefix):
                continue
            body = text[len(prefix):].strip()
            if not body:
                return  # 只发了个前缀，不当作指令
            if body.lower() in HELP_WORDS:
                await self._send_reply(self.help_text(), event, title="指令总览")
                return
            await self._dispatch_command(body, event)
            return

    async def _dispatch_command(self, body: str, event: dict) -> None:
        """按「前缀后第一个词」匹配已注册指令，回复由框架统一发送。"""
        first = body.split(maxsplit=1)[0]
        spec = next((s for s in self._commands if s.name == first), None)
        if spec is None:
            return  # 不是任何已注册指令：静默（消息仍会被归档）
        args = body[len(first):].strip()
        # #<指令> 帮助 或空参数（且注册过 usage）→ 用法详情；否则交给指令本体
        if spec.usage and (not args or args.lower() in HELP_WORDS):
            await self._send_reply(spec.usage, event, title=first)
            return
        reply = await spec.handler(args, event)  # type: ignore[misc]
        if reply:
            await self._send_reply(reply, event, title=first)

    # ---------------- 生命周期 ----------------

    async def run_startup(self) -> None:
        for fn in self.on_startup:
            await fn()
        await self.scheduler.start()

    async def run_shutdown(self) -> None:
        await self.scheduler.stop()
        for fn in self.on_shutdown:
            await fn()

    def schedule(self, name: str, fn: Callable[[], Awaitable[None]],
                 cron: str | None = None, interval: float | None = None) -> None:
        """注册框架级定时任务（插件 setup 时调用；startup 后开始运行）。"""
        self.scheduler.add(name, fn, cron=cron, interval=interval)


def setup_plugins(ctx: PluginCtx) -> None:
    """按 cfg.plugins 加载内置插件并调用 setup(ctx)。未知模块直接启动失败（显式配置错误）。"""
    ctx.plugin_metas.append({"name": "框架", "desc": "noti-gateway 核心", "version": "0.1.0"})
    ctx.stats_providers["plugins"] = lambda: {"loaded": list(ctx.plugin_metas)}

    # 框架自持指令（与插件指令同一套注册/匹配/帮助机制）
    async def cmd_status(args: str, event: dict) -> str:
        up = time.time() - ctx.start_ts
        c = ctx.disp.stats() if ctx.disp else {}
        q = await ctx.store.stats() if ctx.store else {}
        pending = sum((q.get("tasks") or {}).get(k, 0) for k in ("pending", "inflight"))
        return (f"运行 {int(up // 3600)}h{int(up % 3600 // 60)}m ｜ "
                f"收 {c.get('received', 0)} 发 {c.get('sent', 0)} 死 {c.get('dead', 0)} ｜ "
                f"队列积压 {pending} ｜ 会话 {ctx.sessions.sweep()} ｜ 插件 {len(ctx.plugin_metas)}")

    async def cmd_plugins(args: str, event: dict) -> str:
        lines = [f"{m.get('name')} v{m.get('version')} — {m.get('desc')}".rstrip(" —")
                 for m in ctx.plugin_metas]
        return "已加载插件：\n" + "\n".join(lines)

    ctx.add_command("状态", cmd_status, desc="网关运行状态")
    ctx.add_command("插件", cmd_plugins, desc="已加载插件列表")

    for name in ctx.cfg.plugins:
        try:
            mod = import_module(f".{name}", __package__)
        except ImportError as e:
            raise SystemExit(f"plugins: 未知插件 {name!r}: {e}") from None
        setup = getattr(mod, "setup", None)
        if not callable(setup):
            raise SystemExit(f"plugins: {name} 缺少 setup(ctx)")
        setup(ctx)
        meta = getattr(mod, "META", None)
        if isinstance(meta, PluginMeta):
            ctx.plugin_metas.append({"name": meta.name, "desc": meta.desc,
                                     "version": meta.version})
        log.info("plugin loaded: %s", name)
