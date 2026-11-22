"""法定节假日数据（内部助手）：工作日类提醒的「今天上不上班」判定。

数据源：holiday-cn（github.com/NateScarlet/holiday-cn，国务院放假安排的机器可读版），
文件放 noti_gateway/data/holidays-<年份>.json，格式：

    {"year": 2026, "days": [{"name": "元旦", "date": "2026-01-01", "isOffDay": true}, ...]}

isOffDay=true 放假（含调休换来的工作日假），false 调休补班（周末但要上班）。

is_workday(d) 返回三态：
    True  上班（含补班日）；False 放假（含调休换来的假）；None 该日期无数据。
    None 时调用方回退「周一至周五」的朴素判定。

启动时按需加载年份（首次查询某年时惰性加载）；数据文件缺失只记日志，不影响启动。
每年 11-12 月官方发布次年安排后，从 holiday-cn 下载对应 json 放进 data/ 即可。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("noti.holidays")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"  # noti_gateway/data/
_cache: dict[int, tuple[dict[str, str], dict[str, str]]] = {}  # 年份 -> (休: date->name, 班: date->name)
_loaded: set[int] = set()


def _load_year(year: int) -> None:
    if year in _loaded:
        return
    _loaded.add(year)
    p = DATA_DIR / f"holidays-{year}.json"
    if not p.exists():
        log.info("holidays: 无 %d 年数据（%s），工作日提醒回退按星期判定", year, p.name)
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        off: dict[str, str] = {}
        work: dict[str, str] = {}
        for d in raw.get("days", []):
            name = str(d.get("name") or "节假日")
            (off if d.get("isOffDay") else work)[d["date"]] = name
        _cache[year] = (off, work)
        log.info("holidays: %d 年数据已加载（休 %d 天，补班 %d 天）", year, len(off), len(work))
    except Exception as e:
        log.warning("holidays: %s 解析失败: %r", p.name, e)


def ensure_years(years) -> None:
    """预载指定年份（启动时调用，顺带打出数据覆盖日志）。"""
    for y in years:
        _load_year(y)


def is_workday(d: date) -> bool | None:
    """该日期是否上班。True=上班（含补班日）；False=放假；None=无数据（调用方自行回退）。"""
    _load_year(d.year)
    ent = _cache.get(d.year)
    if ent is None:
        return None
    off, work = ent
    k = d.isoformat()
    if k in off:
        return False
    if k in work:
        return True
    return None


def status(d: date) -> str:
    """给人看的一天状态：休（元旦）/ 班（调休补班·春节）/ 周末 / 工作日。"""
    _load_year(d.year)
    ent = _cache.get(d.year)
    k = d.isoformat()
    wd = "周" + "一二三四五六日"[d.weekday()]
    if ent is not None:
        off, work = ent
        if k in off:
            return f"休（{off[k]}）"
        if k in work:
            return f"班（调休补班·{work[k]}，原{wd}）"
    return wd + ("（周末）" if d.weekday() >= 5 else "")


def covered_years() -> list[int]:
    """已提供数据文件的年份列表（#节假日 展示数据覆盖范围用）。"""
    years = []
    for p in DATA_DIR.glob("holidays-*.json"):
        try:
            years.append(int(p.stem.split("-")[-1]))
        except ValueError:
            continue
    return sorted(years)


def run_days_ahead(days: int = 7) -> list[tuple[date, str]]:
    """未来 N 天的 (日期, 状态) 列表（#节假日 展示用）。"""
    out = []
    d = date.today()
    for i in range(days):
        d2 = d + timedelta(days=i)
        out.append((d2, status(d2)))
    return out
