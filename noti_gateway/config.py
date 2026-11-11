"""配置加载：YAML -> 内存配置对象。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = (
    Path("config.yaml"),
    Path("config.example.yaml"),
)


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    # 注意：端口范围 0-65535，100086 是非法端口值
    port: int = 10086


@dataclass
class AuthCfg:
    ip_allow: list[str] = field(default_factory=list)
    # 密钥文件：实时读取，坏行静默忽略，随时增删改即生效（见 keyring.py 格式）
    keys_file: str = ""
    # HMAC 签名时间窗（秒），超出即拒绝，配合 nonce 防重放
    hmac_window_sec: int = 300
    # 命中密钥后是否强制 X-Source 与密钥 source 相符
    strict_source: bool = False


@dataclass
class LimitsCfg:
    per_source_per_minute: int = 60
    per_target_per_minute: int = 30
    # QQ 主动消息每日上限/群，未认证 1000
    per_target_per_day: int = 1000


@dataclass
class DedupCfg:
    enabled: bool = True
    window_seconds: int = 60


@dataclass
class DispatchCfg:
    # wait=1 时的同步等待上限
    sync_timeout: float = 5.0
    # 后台 worker 轮询间隔
    poll_interval: float = 0.5
    # 并发投递数
    concurrency: int = 8
    # 指数退避参数
    retry_base: float = 2.0
    retry_max: float = 300.0
    max_attempts: int = 5


@dataclass
class IngestCfg:
    # WebSocket 上行 / SSE 订阅的接入控制
    max_ws_clients: int = 64        # 全局并发 WS 上行连接上限（0 = 不限）
    max_ws_per_source: int = 8      # 同一 source 的 WS 连接上限（0 = 不限）
    ws_keepalive: float = 20.0      # 服务端应用层心跳间隔秒；空闲超此时长发 {"type":"ping"}（0 = 关闭）
    ws_send_timeout: float = 10.0   # 给单个 WS 客户端回包的发送超时秒，超时断开该客户端
    sse_keepalive: float = 15.0     # SSE 注释心跳间隔秒
    sse_buffer: int = 256           # 每个订阅者的队列长度，满则丢消息


@dataclass
class QQCfg:
    app_id: str = ""
    client_secret: str = ""
    api_base: str = "https://api.bot.qq.com"


@dataclass
class TargetCfg:
    id: str
    type: str
    # 中文备注（仅展示用，出现在 /_stats 等处，不参与路由）
    remark: str = ""
    # 所属机器人 id（bots[].targets 展开时自动填；顶层 target 可写 bot: xxx 复用某机器人凭据）
    bot: str = ""
    # qq
    group_openid: str = ""
    user_openid: str = ""
    channel_id: str = ""
    msg_type: int = 0
    template: str | None = None
    # webhook
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    # ws
    pool: int = 1
    ack_timeout: float = 5.0
    ping_interval: float = 30.0
    reconnect_base: float = 1.0
    reconnect_max: float = 60.0
    max_size: int = 8 * 1024 * 1024
    timeout: float = 10.0
    enabled: bool = True
    # 每分钟覆盖限流
    rate_per_minute: int | None = None

    def limit(self, global_limit: int) -> int:
        return self.rate_per_minute if self.rate_per_minute is not None else global_limit


@dataclass
class BotCfg:
    """一个发送机器人：一套 QQ 凭据 + 名下多个目标。

    bots[].targets 里的目标会平铺进 Config.targets（bot 字段回填本机器人 id），
    路由/分发逻辑不感知 bot 结构；每台机器人、每个目标都可配 remark 中文备注。
    """

    id: str
    # 中文备注（仅展示用，不参与路由）
    remark: str = ""
    app_id: str = ""
    client_secret: str = ""
    api_base: str = "https://api.bot.qq.com"
    targets: list[TargetCfg] = field(default_factory=list)

    def qq(self) -> QQCfg:
        return QQCfg(app_id=self.app_id, client_secret=self.client_secret, api_base=self.api_base)


@dataclass
class RouteCfg:
    # match 支持：source(支持 * 通配/列表) / level(列表) / title_prefix / title_contains
    source: list[str] | str = "*"
    level: list[str] | None = None
    title_prefix: str | None = None
    title_contains: str | None = None
    targets: list[str] = field(default_factory=list)

    def match(self, n) -> bool:
        src = self.source
        srcs = [src] if isinstance(src, str) else src
        if "*" not in srcs and n.source not in srcs:
            return False
        if self.level and n.level not in self.level:
            return False
        if self.title_prefix and not n.title.startswith(self.title_prefix):
            return False
        if self.title_contains and self.title_contains not in n.title:
            return False
        return True


@dataclass
class ChatdownloadCfg:
    # 私聊消息归档（chatdownload）：复用 qq 段同一个机器人，WS 收 C2C 消息落盘
    # 布局固定：chat.txt（文本）+ media/（图片与文件）+ events.jsonl（防丢流水）
    enabled: bool = False
    data_dir: str = "quest"  # 归档目录（相对运行目录）
    target_users: list[str] = field(default_factory=list)  # user_openid 白名单；空 = 全部
    download_concurrency: int = 2
    # 绑定哪个机器人收消息（bots[].id）；留空 = 第一个带凭据的 bot，再无则回落旧式全局 qq 段
    bot: str = ""


@dataclass
class Config:
    server: ServerCfg
    auth: AuthCfg
    limits: LimitsCfg
    dedup: DedupCfg
    dispatch: DispatchCfg
    qq: QQCfg
    targets: list[TargetCfg]
    routes: list[RouteCfg]
    ingest: IngestCfg = field(default_factory=IngestCfg)
    chatdownload: ChatdownloadCfg = field(default_factory=ChatdownloadCfg)
    default_route: str = "default"
    # 多机器人：bots[].targets 已平铺进 targets，此处保留结构用于凭据解析/展示
    bots: list[BotCfg] = field(default_factory=list)

    def target(self, tid: str) -> TargetCfg | None:
        for t in self.targets:
            if t.id == tid:
                return t
        return None

    def bot(self, bid: str) -> BotCfg | None:
        for b in self.bots:
            if b.id == bid:
                return b
        return None

    def qq_for(self, tc: TargetCfg) -> QQCfg:
        """目标绑定的机器人凭据：bot 指定的机器人优先，否则回落旧式全局 qq 段。"""
        if tc.bot:
            b = self.bot(tc.bot)
            if b is not None:
                return b.qq()
        return self.qq

    def chatdownload_bot(self) -> tuple[str, QQCfg]:
        """chatdownload 绑定的机器人：bot 字段指定 > 第一个带凭据的 bot > 旧式全局 qq 段。

        返回 (bot_id, QQCfg)；bot_id 为空表示旧式全局 qq 段。
        """
        if self.chatdownload.bot:
            b = self.bot(self.chatdownload.bot)
            if b is None:
                raise SystemExit(f"chatdownload.bot 引用了不存在的 bot: {self.chatdownload.bot}")
            return b.id, b.qq()
        for b in self.bots:
            if b.app_id and b.client_secret:
                return b.id, b.qq()
        return "", self.qq


def _dd(d: dict[str, Any], cls):
    """把 dict 转成 dataclass，忽略未知字段。"""
    names = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in (d or {}).items() if k in names})


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        env = os.environ.get("NOTI_CONFIG")
        if env:
            path = env
        else:
            for p in DEFAULT_CONFIG_FILES:
                if p.exists():
                    path = p
                    break
    if path is None:
        raise SystemExit(
            "找不到配置文件：请在项目根目录放 config.yaml，"
            "或设置环境变量 NOTI_CONFIG=/path/to/config.yaml"
        )

    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    targets = []
    bots: list[BotCfg] = []
    for b in raw.get("bots", []) or []:
        if "id" not in b:
            raise SystemExit(f"bot 缺少 id: {b}")
        bot = BotCfg(
            id=b["id"],
            remark=b.get("remark", ""),
            app_id=b.get("app_id", ""),
            client_secret=b.get("client_secret", ""),
            api_base=b.get("api_base") or "https://api.bot.qq.com",
        )
        for t in b.get("targets", []) or []:
            if "id" not in t:
                raise SystemExit(f"bot {bot.id} 的 target 缺少 id: {t}")
            if "type" not in t:
                t = {**t, "type": "qq"}  # bot 名下的目标默认 QQ 发送
            tc = _dd(t, TargetCfg)
            tc.bot = bot.id
            bot.targets.append(tc)
            targets.append(tc)
        bots.append(bot)

    for t in raw.get("targets", []) or []:
        if "id" not in t or "type" not in t:
            raise SystemExit(f"target 缺少 id 或 type: {t}")
        targets.append(_dd(t, TargetCfg))

    seen: set[str] = set()
    for tc in targets:
        if tc.id in seen:
            raise SystemExit(f"target id 重复: {tc.id}")
        seen.add(tc.id)

    routes = [ _dd(r, RouteCfg) for r in (raw.get("routes") or []) ]
    if not routes:
        raise SystemExit("至少要配置一条 route")

    cfg = Config(
        server=_dd(raw.get("server"), ServerCfg),
        auth=_dd(raw.get("auth"), AuthCfg),
        limits=_dd(raw.get("limits"), LimitsCfg),
        dedup=_dd(raw.get("dedup"), DedupCfg),
        dispatch=_dd(raw.get("dispatch"), DispatchCfg),
        qq=_dd(raw.get("qq"), QQCfg),
        targets=targets,
        routes=routes,
        ingest=_dd(raw.get("ingest"), IngestCfg),
        chatdownload=_dd(raw.get("chatdownload"), ChatdownloadCfg),
        default_route=raw.get("default_route", "default"),
        bots=bots,
    )

    # keys_file 相对路径按配置文件所在目录解析（部署时不依赖 cwd）
    if cfg.auth.keys_file:
        kp = Path(cfg.auth.keys_file)
        if not kp.is_absolute():
            kp = path.parent / kp
        cfg.auth.keys_file = str(kp)

    if not cfg.auth.keys_file:
        raise SystemExit("auth.keys_file 未配置：出于安全考虑不提供无鉴权模式")

    ids = {t.id for t in targets}
    bot_ids = {b.id for b in bots}
    for t in targets:
        if t.bot and t.bot not in bot_ids:
            raise SystemExit(f"target {t.id} 引用了不存在的 bot: {t.bot}")
    if cfg.chatdownload.bot and cfg.chatdownload.bot not in bot_ids:
        raise SystemExit(f"chatdownload.bot 引用了不存在的 bot: {cfg.chatdownload.bot}")
    for r in routes:
        for tid in r.targets:
            if tid not in ids:
                raise SystemExit(f"route 引用了不存在的 target: {tid}")

    return cfg
