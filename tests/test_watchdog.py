"""看门狗插件测试：状态机去抖 + 告警/恢复通知组装（纯逻辑，不依赖真实 psutil 读数）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noti_gateway.config import WatchdogCfg  # noqa: E402
from noti_gateway.plugins.watchdog import WatchdogPlugin  # noqa: E402


def _plugin(confirm: int = 2, recover: int = 2, mem_pct: int = 85) -> WatchdogPlugin:
    cfg = WatchdogCfg(enabled=True, mem_pct=mem_pct, swap_pct=70, cpu_pct=90,
                      disk_pct=90, disks={"/": 90}, confirm=confirm,
                      recover_confirm=recover, proc_top=0)
    return WatchdogPlugin(cfg)


def test_confirm_debounce() -> None:
    plg = _plugin(confirm=2)
    # 第 1 次超标：只计数，不告警
    assert plg.evaluate({"mem": 90.0}) == []
    # 第 2 次超标：进入告警
    acts = plg.evaluate({"mem": 90.0})
    assert [(m, k) for m, k, _, _ in acts] == [("mem", "alert")]


def test_recover_debounce() -> None:
    plg = _plugin(confirm=1, recover=2)
    assert plg.evaluate({"mem": 90.0})  # 立即告警
    assert plg.evaluate({"mem": 50.0}) == []  # 第 1 次回落：只计数
    acts = plg.evaluate({"mem": 50.0})
    assert [(m, k) for m, k, _, _ in acts] == [("mem", "recover")]


def test_flap_resets_counters() -> None:
    plg = _plugin(confirm=2)
    plg.evaluate({"mem": 90.0})   # over=1
    plg.evaluate({"mem": 50.0})   # under=1, over 清零
    plg.evaluate({"mem": 90.0})   # over=1（重新计）
    assert plg.evaluate({"mem": 90.0})  # over=2 -> 告警


def test_multi_metric_independent() -> None:
    plg = _plugin(confirm=1, recover=1)
    acts = plg.evaluate({"mem": 90.0, "swap": 30.0})
    assert [(m, k) for m, k, _, _ in acts] == [("mem", "alert")]
    acts = plg.evaluate({"mem": 50.0, "swap": 80.0})
    assert [(m, k) for m, k, _, _ in acts] == [("mem", "recover"), ("swap", "alert")]


def test_disk_metric_threshold() -> None:
    plg = _plugin(confirm=1)
    plg.cfg.disks = {"/": 90, "/data": 50}
    acts = plg.evaluate({"disk:/": 91.0, "disk:/data": 55.0})
    assert sorted((m, k) for m, k, _, _ in acts) == [("disk:/", "alert"), ("disk:/data", "alert")]


def test_compose_alert_error_level() -> None:
    plg = _plugin()
    n = plg.compose("mem", "alert", 96.0, 85, top_lines=["123 proc 45.6%"])
    assert n.level == "error"  # 超阈值 10 个点以上 -> error
    assert "96%" in n.title and "85%" in n.title
    assert "123 proc" in n.body


def test_compose_recover_info_level() -> None:
    plg = _plugin()
    import time
    since = time.time() - 600
    n = plg.compose("mem", "recover", 50.0, 85, state_since=since)
    assert n.level == "info"
    assert "回落" in n.title and "告警持续 10 分钟" in n.body
