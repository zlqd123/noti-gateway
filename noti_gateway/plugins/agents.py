"""Agent 通知处理插件（agents）：ZCode/OpenCode/DSH/WorkBuddy 等来源的通知中间件。

挂在 Dispatcher.submit 入队前（框架级通知中间件链），按 source 做确定性处理：

- **静默时段**：quiet_hours（如 "23:00-07:30"，支持跨午夜）内的 quiet_levels 通知
  直接丢弃（账本仍记 SSE dropped 事件），防半夜被 agent 刷屏；
- **per-source 模板**：templates.<source> 用网关的 {{ var }} 模板改写标题/正文，
  不同 agent 有不同呈现风格；未配置的 source 原样通过；
- **MD 卡片**：md_card 开启后，为 md_card_sources（缺省同 sources）的通知自动把
  title/body/fields 组装成 QQ markdown 卡片（msg_type=2）；通知自带 markdown/ark/
  raw_qq 或富媒体时不套卡片；发送侧无 markdown 权限时由 targets 自动降级纯文本；
- **来源统计**：/_stats 的 agents 段暴露每来源 seen/sent/quiet_dropped 计数。

丢弃语义：middleware 返回 None -> Dispatcher 计入 dropped 计数并广播 SSE
``dropped`` 事件（/stream 订阅 &types=dropped 可见），HTTP 提交方收到
{"queued": false, "dropped": true}。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from ..models import Notification
from . import PluginMeta
from ._cron import TZ_CN
from ..targets import render

log = logging.getLogger("noti.agents")

META = PluginMeta(name="agents", desc="Agent 通知静默时段 / per-source 模板 / MD 卡片 / 来源统计",
                  version="1.1.0")

# QQ markdown 码点上限（契约同 dsh qq-gw 的 QQ_MARKDOWN_MAX_CODEPOINTS）
MD_CARD_MAX_CODEPOINTS = 3000

_LEVEL_ICON = {"debug": "🔍", "info": "✅", "warn": "⚠️", "error": "❌"}


def _md_card(n: Notification) -> str:
    """title/body/fields -> QQ markdown 卡片文本（页面顶部加粗标题，底部来源时间页脚）。"""
    icon = _LEVEL_ICON.get(n.level, "🔔")
    lines = [f"**{icon} {n.title}**"]
    if n.body:
        lines.append("")
        lines.append(n.body)
    if n.fields:
        lines.append("")
        for k, v in n.fields.items():
            v = str(v)
            lines.append(f"> {k}：{v[:120]}{'…' if len(v) > 120 else ''}")
    lines.append("")
    lines.append(f"*{n.source} · {time.strftime('%m-%d %H:%M', time.localtime(n.ts))}*")
    text = "\n".join(lines)
    if len(text) > MD_CARD_MAX_CODEPOINTS:  # len(str) 即码点数
        text = text[: MD_CARD_MAX_CODEPOINTS - 1] + "…"
    return text


def _parse_hm(s: str) -> int | None:
    parts = s.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None


class AgentProcessor:
    def __init__(self, cfg: Any, hub: Any = None) -> None:
        self.cfg = cfg
        self.hub = hub
        self.stats: dict[str, dict[str, int]] = {}
        q = self._parse_range(cfg.quiet_hours)
        if cfg.quiet_hours and q is None:
            raise SystemExit(f"agents.quiet_hours 格式非法: {cfg.quiet_hours!r}（应为 HH:MM-HH:MM）")
        self._quiet = q
        # MD 卡片：来源清单缺省同 sources
        self._card_sources = set(cfg.md_card_sources or cfg.sources)

    @staticmethod
    def _parse_range(s: str) -> tuple[int, int] | None:
        if not s or "-" not in s:
            return None
        a, b = s.split("-", 1)
        start, end = _parse_hm(a), _parse_hm(b)
        return (start, end) if start is not None and end is not None else None

    def _in_quiet(self) -> bool:
        if self._quiet is None:
            return False
        start, end = self._quiet
        m = datetime.now(TZ_CN).hour * 60 + datetime.now(TZ_CN).minute
        return (start <= m < end) if start <= end else (m >= start or m < end)

    def _stat(self, source: str, key: str) -> None:
        row = self.stats.setdefault(source, {"seen": 0, "sent": 0, "quiet_dropped": 0})
        row[key] += 1

    async def process(self, n: Notification) -> Notification | None:
        if n.source not in self.cfg.sources:
            return n
        self._stat(n.source, "seen")

        if n.level in self.cfg.quiet_levels and self._in_quiet():
            self._stat(n.source, "quiet_dropped")
            log.info("agents: %s 的 %s 通知在静默时段被过滤（%s）", n.source, n.level, n.title)
            if self.hub is not None:
                try:
                    self.hub.publish("dropped", {"source": n.source, "level": n.level,
                                                 "title": n.title, "reason": "quiet_hours"})
                except Exception:
                    pass
            return None

        tpl = (self.cfg.templates or {}).get(n.source)
        if tpl:
            text = render(tpl, n, default="")
            lines = text.splitlines()
            if lines and lines[0].strip():
                n.title = lines[0].strip()
                n.body = "\n".join(lines[1:]).strip()

        # MD 卡片：仅纯文本型通知套卡片；自带 markdown/ark/raw_qq 或富媒体的不动
        if (self.cfg.md_card and n.source in self._card_sources
                and not any((n.markdown, n.ark, n.raw_qq, n.image, n.file, n.video, n.audio))):
            n.markdown = {"content": _md_card(n)}

        self._stat(n.source, "sent")
        return n

    def info(self) -> dict:
        return {
            "sources": list(self.cfg.sources),
            "quiet_hours": self.cfg.quiet_hours or "无",
            "stats": self.stats,
        }


def setup(ctx: Any) -> None:
    """注册 agents：通知中间件 + /_stats 观测段。"""
    cfg = ctx.cfg.agents
    if not cfg.enabled:
        log.info("agents 插件未启用（agents.enabled=false）")
        return
    proc = AgentProcessor(cfg, hub=ctx.hub)
    ctx.notification_middleware.append(proc.process)
    ctx.stats_providers["agents"] = proc.info
