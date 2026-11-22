"""服务器看门狗插件（watchdog）：内存/交换/磁盘/CPU 超阈值告警 + 恢复通知。

挂在框架调度器上周期采样 psutil 读数，状态机去抖后经 Dispatcher 投递告警：

- **状态机**：每指标独立 normal/alerting 二态——连续 confirm 次超标才告警，
  连续 recover_confirm 次回落才报恢复（防抖动导致告警风暴）；
- **告警内容**：当前读数 + 阈值 + 内存 TOP N 进程（告警瞬间的占用现场）；
- **恢复内容**：当前读数 + 告警持续时长；
- 投递走 disp.submit（dedup=False：告警不参与聚合折叠，去抖完全交给状态机），
  复用路由/限流/静默/重试；agents 的静默时段可按 level 配置放行或吞掉；
- /_stats 的 watchdog 段暴露当前读数与各指标状态。

依赖：psutil（requirements.txt）。config.yaml 的 plugins 清单加 "watchdog" 并加
watchdog 段启用（默认关闭）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..models import Notification
from . import PluginMeta

try:
    import psutil
except ImportError as e:  # 未装依赖时启动即报错，不静默降级
    raise SystemExit(f"watchdog 插件需要 psutil：pip install psutil（{e}）") from None

log = logging.getLogger("noti.watchdog")

META = PluginMeta(name="watchdog", desc="服务器资源看门狗：内存/交换/磁盘/CPU 超阈值告警 + 恢复通知",
                  version="1.0.0")

# 指标中文名（告警标题用）
_NAMES = {"mem": "内存", "swap": "交换分区", "cpu": "CPU"}

# 级别：超阈值 10 个百分点以上视为 error，其余 warn
_ERROR_GAP = 10


class WatchdogPlugin:
    def __init__(self, cfg: Any, disp: Any = None, hub: Any = None) -> None:
        self.cfg = cfg
        self.disp = disp
        self.hub = hub
        # metric -> {"alerting": bool, "over": int, "under": int, "since": ts|None}
        self.state: dict[str, dict[str, Any]] = {}
        psutil.cpu_percent(interval=None)  # 预热：首次 cpu_percent 恒为 0

    # ---------------- 采样 ----------------

    def _threshold(self, metric: str) -> int | None:
        if metric == "mem":
            return self.cfg.mem_pct
        if metric == "swap":
            return self.cfg.swap_pct
        if metric == "cpu":
            return self.cfg.cpu_pct
        if metric.startswith("disk:"):
            path = metric[5:]
            return self.cfg.disks.get(path, self.cfg.disk_pct if path == "/" else None)
        return None

    def readings(self) -> dict[str, float]:
        out: dict[str, float] = {"mem": psutil.virtual_memory().percent,
                                 "swap": psutil.swap_memory().percent}
        for path in self.cfg.disks:
            out[f"disk:{path}"] = psutil.disk_usage(path).percent
        cpu = psutil.cpu_percent(interval=None)
        if cpu is not None and cpu >= 0:
            out["cpu"] = float(cpu)
        return out

    def top_procs(self, n: int) -> list[str]:
        """按内存占用排序的 TOP N 进程行（psutil 异常时返回空列表，不拖垮告警）。"""
        if n <= 0:
            return []
        try:
            procs = []
            for p in psutil.process_iter(["pid", "name", "memory_percent"]):
                info = p.info
                procs.append((info.get("memory_percent") or 0.0, info.get("pid") or 0,
                              (info.get("name") or "?")[:24]))
            procs.sort(reverse=True)
            return [f"{pid} {name} {mp:.1f}%" for mp, pid, name in procs[:n]]
        except Exception as e:
            log.debug("top_procs 失败: %r", e)
            return []

    # ---------------- 状态机（纯函数，便于测试） ----------------

    def evaluate(self, readings: dict[str, float]) -> list[tuple[str, str, float, int]]:
        """按读数推进状态机，返回待发动作列表 [(metric, kind, value, threshold)]。

        kind: "alert"（进入告警）| "recover"（解除告警）。未达 confirm/recover_confirm
        次数的过渡只累计计数，不产生动作。"""
        actions: list[tuple[str, str, float, int]] = []
        for metric, val in readings.items():
            th = self._threshold(metric)
            if th is None or th <= 0:
                continue
            st = self.state.setdefault(metric, {"alerting": False, "over": 0,
                                                "under": 0, "since": None})
            if val >= th:
                st["over"] += 1
                st["under"] = 0
                if not st["alerting"] and st["over"] >= self.cfg.confirm:
                    st["alerting"] = True
                    st["since"] = time.time()
                    actions.append((metric, "alert", val, th))
            else:
                st["under"] += 1
                st["over"] = 0
                if st["alerting"] and st["under"] >= self.cfg.recover_confirm:
                    st["alerting"] = False
                    st["since"] = None
                    actions.append((metric, "recover", val, th))
        return actions

    # ---------------- 组装通知（纯函数，便于测试） ----------------

    def compose(self, metric: str, kind: str, val: float, th: int,
                top_lines: list[str] | None = None,
                state_since: float | None = None) -> Notification:
        name = _NAMES.get(metric, metric.replace("disk:", "磁盘 ") or metric)
        pct = f"{val:.0f}%"
        if kind == "alert":
            level = "error" if val >= th + _ERROR_GAP else "warn"
            title = f"{name} {pct} 超阈值（{th}%）"
            body_lines = list(top_lines or [])
            if body_lines:
                body_lines.insert(0, "内存占用 TOP：")
        else:
            level = "info"
            dur = ""
            if state_since:
                mins = int((time.time() - state_since) // 60)
                dur = f"，告警持续 {mins} 分钟" if mins else ""
            title = f"{name} 回落至 {pct}（低于阈值 {th}%）"
            body_lines = [f"告警解除{dur}。"] if dur else []
        n = Notification(source=self.cfg.source, level=level, title=title,
                         body="\n".join(body_lines), meta={"via": "watchdog", "metric": metric})
        return n

    # ---------------- 调度入口 ----------------

    async def sample(self) -> None:
        readings = self.readings()
        actions = self.evaluate(readings)
        for metric, kind, val, th in actions:
            top = self.top_procs(self.cfg.proc_top) if (kind == "alert" and metric == "mem") else None
            st = self.state.get(metric) or {}
            n = self.compose(metric, kind, val, th, top_lines=top, state_since=st.get("since"))
            try:
                await self.disp.submit(n, wait=False, dedup=False)
                log.info("watchdog: %s", n.title)
            except Exception as e:
                log.error("watchdog 告警提交失败: %r", e)
        if actions or self.cfg.check_interval >= 0:
            self._pub(readings)

    def _pub(self, readings: dict[str, float]) -> None:
        if self.hub is None:
            return
        try:
            self.hub.publish("watchdog", {
                "readings": readings,
                "states": {k: bool(v.get("alerting")) for k, v in self.state.items()},
            })  # /stream 订阅 &types=watchdog 可见
        except Exception:
            log.exception("hub publish failed")

    def info(self) -> dict:
        return {
            "readings": self.readings(),
            "alerting": [k for k, v in self.state.items() if v.get("alerting")],
            "thresholds": {"mem": self.cfg.mem_pct, "swap": self.cfg.swap_pct,
                           "cpu": self.cfg.cpu_pct, "disks": self.cfg.disks},
        }


def setup(ctx: Any) -> None:
    """注册 watchdog：框架调度器周期采样 + /_stats 观测段。"""
    cfg = ctx.cfg.watchdog
    if not cfg.enabled:
        log.info("watchdog 插件未启用（watchdog.enabled=false）")
        return
    plg = WatchdogPlugin(cfg, disp=ctx.disp, hub=ctx.hub)
    ctx.schedule("watchdog-sample", plg.sample, interval=max(15, cfg.check_interval))
    ctx.stats_providers["watchdog"] = plg.info
    log.info("watchdog 已启用：mem>%d%% swap>%d%% cpu>%d%% disks=%s，%ds 采样",
             cfg.mem_pct, cfg.swap_pct, cfg.cpu_pct, cfg.disks or {}, cfg.check_interval)
