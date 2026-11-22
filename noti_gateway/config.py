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
    # 私聊消息归档（chatdownload）：qqws 插件收 C2C 消息后广播，本插件只负责落盘
    # 布局固定：chat.txt（文本）+ media/（图片与文件）+ events.jsonl（防丢流水）
    enabled: bool = False
    data_dir: str = "quest"  # 归档目录（相对运行目录）
    target_users: list[str] = field(default_factory=list)  # user_openid 白名单；空 = 全部
    download_concurrency: int = 2
    # 兼容保留：连接归属优先看 qqws.bot；两者都留空时才用它（历史配置的 bot 字段）
    bot: str = ""


@dataclass
class QqwsCfg:
    # QQ 官方网关长连接插件（qqws）：全进程唯一的收消息入口，
    # 收到 dispatch 事件后广播给其他插件（chatdownload 归档、reminders 指令等）
    enabled: bool = False
    # 连接用哪台机器人的凭据（bots[].id）；留空 = 依次回落 chatdownload.bot >
    # 第一个带凭据的 bot > 旧式全局 qq 段
    bot: str = ""


@dataclass
class AgentsCfg:
    # Agent 通知处理插件（agents）：ZCode/OpenCode/DSH/WorkBuddy 等来源的通知中间件
    enabled: bool = False
    # 适用来源（X-Source / HMAC 密钥归属）；不在列表内的通知原样通过
    sources: list[str] = field(default_factory=lambda: ["zcode", "opencode", "dsh", "workbuddy"])
    # 静默时段（东八区，HH:MM-HH:MM，支持跨午夜如 "23:00-07:30"）；空 = 不静默
    quiet_hours: str = ""
    # 静默时段内被丢弃的通知级别
    quiet_levels: list[str] = field(default_factory=lambda: ["info"])
    # per-source 呈现模板（{{ title }} / {{ body }} 等）；未配置的 source 原样通过
    templates: dict[str, str] = field(default_factory=dict)


@dataclass
class UploadsCfg:
    # 分片上传收件（/u/*）：大文件先分片推给网关，通知里用 ref 引用
    dir: str = ""                       # 存储目录；留空 = NOTI_DB 同级 uploads/
    ttl: int = 86400                    # 上传件保留秒数，过期自动清理
    chunk_size: int = 4 * 1024 * 1024   # 建议分片大小（字节）
    max_size: int = 190 * 1024 * 1024   # 单件上限（QQ 分片通道实测 200MB，留余量）


@dataclass
class RemindersCfg:
    # 定时提醒插件：一次性/每天/每周/工作日/cron，HTTP /schedule + QQ 固定语法命令
    enabled: bool = True
    source: str = "remind"        # 提醒投递/回复用的 source（走 routes 路由扇出）
    grace_seconds: int = 900      # 错过补发窗口：停机跨过触发点，重启后窗口内补发
    tick_seconds: float = 1.0     # 调度扫描间隔秒
    max_list: int = 20            # 「提醒 列表」最多展示条数
    # 工作日提醒的节假日感知（noti_gateway/data/holidays-<年份>.json）
    holidays_enabled: bool = True


@dataclass
class WatchdogCfg:
    # 服务器看门狗插件：内存/交换/磁盘/CPU 超阈值告警 + 恢复通知（状态机去抖）
    enabled: bool = False
    # 告警通知的 source（走 routes 路由扇出；默认路由时跟随 default_route）
    source: str = "watchdog"
    # 各指标告警线（百分比）
    mem_pct: int = 85
    swap_pct: int = 70
    cpu_pct: int = 90
    disk_pct: int = 90              # 根分区 /
    disks: dict[str, int] = field(default_factory=dict)  # 额外挂载点: {"/data": 85}
    check_interval: int = 60        # 采样周期秒（最低 15）
    confirm: int = 3                # 连续 N 次超标才告警（去抖）
    recover_confirm: int = 5        # 连续 N 次回落才报恢复
    proc_top: int = 5               # 内存告警时附 TOP N 进程


@dataclass
class WebchatCfg:
    # WebChat 网页消息面板插件：网页实时查看/发送 QQ 单聊消息（迁移自 bison2）
    enabled: bool = False
    totp_secret: str = ""         # TOTP 密钥（Base32，验证器 App 绑定用）；留空自动生成并持久到 data_dir
    session_days: int = 7         # 登录会话令牌有效期（天）
    max_per_chat: int = 300       # 每会话内存保留条数
    persist: bool = True          # jsonl 落盘（重启恢复最近历史）
    data_dir: str = "webchat"     # 落盘目录（相对运行目录）


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
    qqws: QqwsCfg = field(default_factory=QqwsCfg)
    agents: AgentsCfg = field(default_factory=AgentsCfg)
    uploads: UploadsCfg = field(default_factory=UploadsCfg)
    reminders: RemindersCfg = field(default_factory=RemindersCfg)
    webchat: WebchatCfg = field(default_factory=WebchatCfg)
    watchdog: WatchdogCfg = field(default_factory=WatchdogCfg)
    default_route: str = "default"
    # 插件清单：noti_gateway/plugins/ 下不以 _ 开头的模块名。
    # 顺序即事件分发顺序：qqws 持有连接，chatdownload 归档在前，命令类插件在后
    plugins: list[str] = field(default_factory=lambda: ["qqws", "chatdownload", "reminders", "agents"])
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

    def qqws_bot(self) -> tuple[str, QQCfg]:
        """网关长连接绑定的机器人：qqws.bot > chatdownload.bot > 第一个带凭据的 bot > 旧式全局段。"""
        if self.qqws.bot:
            b = self.bot(self.qqws.bot)
            if b is None:
                raise SystemExit(f"qqws.bot 引用了不存在的 bot: {self.qqws.bot}")
            return b.id, b.qq()
        return self.chatdownload_bot()


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
        qqws=_dd(raw.get("qqws"), QqwsCfg),
        agents=_dd(raw.get("agents"), AgentsCfg),
        uploads=_dd(raw.get("uploads"), UploadsCfg),
        reminders=_dd(raw.get("reminders"), RemindersCfg),
        webchat=_dd(raw.get("webchat"), WebchatCfg),
        watchdog=_dd(raw.get("watchdog"), WatchdogCfg),
        default_route=raw.get("default_route", "default"),
        plugins=[str(p) for p in (raw.get("plugins") or ["qqws", "chatdownload", "reminders", "agents"])],
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
    if cfg.qqws.bot and cfg.qqws.bot not in bot_ids:
        raise SystemExit(f"qqws.bot 引用了不存在的 bot: {cfg.qqws.bot}")
    # 一个 appid 只有一条事件流：连接插件与归档器必须指向同一台机器人
    if (cfg.chatdownload.enabled and cfg.qqws.bot and cfg.chatdownload.bot
            and cfg.qqws.bot != cfg.chatdownload.bot):
        raise SystemExit(
            f"qqws.bot({cfg.qqws.bot}) 与 chatdownload.bot({cfg.chatdownload.bot}) 不一致："
            "一个 appid 只有一条事件流，两者必须指向同一台机器人"
        )
    # 归档器依赖事件流：qqws 必须启用且在插件清单里
    if cfg.chatdownload.enabled:
        if not cfg.qqws.enabled:
            raise SystemExit("chatdownload.enabled=true 需要 qqws 提供事件流（请设置 qqws.enabled=true）")
        if "qqws" not in cfg.plugins:
            raise SystemExit("chatdownload.enabled=true 但 plugins 清单里没有 qqws，归档器将收不到任何消息")
    for r in routes:
        for tid in r.targets:
            if tid not in ids:
                raise SystemExit(f"route 引用了不存在的 target: {tid}")

    return cfg
