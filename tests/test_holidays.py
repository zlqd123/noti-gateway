"""节假日感知测试：_holidays 数据判定 + 工作日提醒的推进逻辑。

数据用 2026 年官方安排（随仓库入库的 holidays-2026.json）：
- 2026-01-01~01-03 元旦休（周四~周六），01-04 周日调休补班；
- 2026-02-14 周六调休补班（春节前）；
- 未列出的周六/周日按周末休。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noti_gateway.plugins import _holidays, reminders  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def _ts(y: int, m: int, d: int, hh: int = 0, mi: int = 0) -> float:
    return datetime(y, m, d, hh, mi, tzinfo=TZ).timestamp()


def test_is_workday_three_state() -> None:
    # 法定假日的周四 -> False；调休补班的周日 -> True；未列出的周六 -> None（无数据回退）
    assert _holidays.is_workday(date(2026, 1, 1)) is False
    assert _holidays.is_workday(date(2026, 1, 4)) is True
    assert _holidays.is_workday(date(2026, 2, 14)) is True
    assert _holidays.is_workday(date(2026, 3, 14)) is None  # 周六且无数据条目
    assert _holidays.is_workday(date(2026, 3, 16)) is None  # 周一无条目 -> 回退按星期
    # status() 人话输出
    assert "元旦" in _holidays.status(date(2026, 1, 1))
    assert "补班" in _holidays.status(date(2026, 1, 4))


def test_next_weekday_fire_skips_holidays() -> None:
    # 元旦假期（1/1~1/3 休）里触发推进 -> 跳到 1/4 周日补班日
    nxt = reminders._next_weekday_fire("0 9 * * 1-5", _ts(2026, 1, 1, 10))
    assert datetime.fromtimestamp(nxt, TZ) == datetime(2026, 1, 4, 9, tzinfo=TZ)


def test_next_weekday_fire_hits_makeup_saturday() -> None:
    # 周五（2/13）09:00 已过 -> 下一次是 2/14 周六调休补班日
    nxt = reminders._next_weekday_fire("0 9 * * 1-5", _ts(2026, 2, 13, 10))
    assert datetime.fromtimestamp(nxt, TZ) == datetime(2026, 2, 14, 9, tzinfo=TZ)


def test_next_weekday_fire_same_day() -> None:
    # 周一 09:00 前推进 -> 当天 09:00
    nxt = reminders._next_weekday_fire("30 8 * * 1-5", _ts(2026, 3, 2, 8, 0))
    assert datetime.fromtimestamp(nxt, TZ) == datetime(2026, 3, 2, 8, 30, tzinfo=TZ)


def test_run_day_fallback_without_data() -> None:
    # 数据未覆盖的年份回退按星期：2024-11-16 是周六 -> 非工作日；11-18 周一 -> 工作日
    assert reminders._run_day(date(2024, 11, 16)) is False
    assert reminders._run_day(date(2024, 11, 18)) is True
