"""极简 5 段 cron 引擎：解析 + 求下一次触发时刻。

支持语法（与 crontab 一致的常用子集）::

    分 时 日 月 周      每段: * | 数字 | a-b | a-b/步长 | */步长 | 逗号列表
    周字段 0 和 7 都表示周日；日与周同时受限时按标准 cron 取「或」语义。
    非标准扩展：日字段支持独立项 L，表示当月最后一天（如 0 9 L * *，不支持 L-3 等范围）。

有意不支持的：@reboot 等别名、秒级精度、名称月份（JAN）、环境变量行、年字段。
求下一次采用「逐日扫描 + 当日时刻集合」而非逐分钟推进，最坏 1500 天封顶，
对「永不匹配」的表达式（如 2 月 30 日）抛 ValueError 而不是死循环。
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

TZ_CN = timezone(timedelta(hours=8))  # 固定东八区，不依赖服务器系统时区

_ALL_DOM = set(range(1, 32))
_ALL_DOW = set(range(0, 8))  # 0=周日 ... 6=周六（7 已并入 0）
_ALL_HOURS = set(range(24))
_ALL_MINS = set(range(60))
_ALL_MONTHS = set(range(1, 13))
_DOM_LAST = 32  # 日字段 L 的哨兵值：当月最后一天

_WEEK_CN = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "0": "日", "7": "日"}
_DOW_CN = "日一二三四五六"  # 按下标取字：0=周日


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for term in field.split(","):
        term = term.strip()
        if not term:
            raise ValueError(f"cron 字段为空: {field!r}")
        step = 1
        if "/" in term:
            term, _, step_s = term.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"cron 步长非法: {step_s!r}") from None
            if step < 1:
                raise ValueError(f"cron 步长非法: {step}")
        if term == "*":
            lo2, hi2 = lo, hi
        elif "-" in term:
            a, _, b = term.partition("-")
            lo2, hi2 = _to_int(a), _to_int(b)
        else:
            lo2 = _to_int(term)
            # 标准 cron 语义：单个值加步长 = 从该值到上限按步长
            hi2 = hi if step > 1 else lo2
        if not (lo <= lo2 <= hi) or not (lo <= hi2 <= hi) or lo2 > hi2:
            raise ValueError(f"cron 取值越界: {term}（范围 {lo}-{hi}）")
        out.update(range(lo2, hi2 + 1, step))
    return out


def _parse_dom(field: str) -> set[int]:
    """日字段：额外支持独立项 L（当月最后一天，存为哨兵值 _DOM_LAST）。"""
    out: set[int] = set()
    rest: list[str] = []
    for term in field.split(","):
        if term.strip() == "L":
            out.add(_DOM_LAST)
        else:
            rest.append(term)
    if rest:
        out |= _parse_field(",".join(rest), 1, 31)
    return out


def _to_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"cron 取值非法: {s!r}") from None


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """解析 5 段表达式 -> (分, 时, 日, 月, 周) 的合法取值集合。非法抛 ValueError。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron 需要 5 段（分 时 日 月 周），收到 {len(parts)} 段: {expr!r}")
    mins = _parse_field(parts[0], 0, 59)
    hours = _parse_field(parts[1], 0, 23)
    doms = _parse_dom(parts[2])
    months = _parse_field(parts[3], 1, 12)
    dows = _parse_field(parts[4], 0, 7)
    if 7 in dows:
        dows.add(0)
    return mins, hours, doms, months, dows


def _dow(day) -> int:
    return day.isoweekday() % 7  # 周一=1...周六=6，周日=0


def _month_days(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _day_ok(day, doms: set[int], dows: set[int], months: set[int]) -> bool:
    if day.month not in months:
        return False
    dom_r, dow_r = doms != _ALL_DOM, dows != _ALL_DOW
    dom_hit = day.day in doms or (
        _DOM_LAST in doms and day.day == _month_days(day.year, day.month)
    )
    if dom_r and dow_r:  # 标准 cron：日与周同时受限时取「或」
        return dom_hit or _dow(day) in dows
    if dom_r:
        return dom_hit
    if dow_r:
        return _dow(day) in dows
    return True


def cron_next(expr: str, after_ts: float) -> float:
    """expr 在 after_ts 之后的下一次触发时刻（epoch 秒，东八区）。永不匹配抛 ValueError。"""
    mins, hours, doms, months, dows = parse_cron(expr)
    cur = datetime.fromtimestamp(after_ts, TZ_CN).replace(second=0, microsecond=0)
    cur += timedelta(minutes=1)
    floor_hm = cur.hour * 60 + cur.minute  # 第一天只看此刻之后的时刻
    day = cur.date()
    first = True
    for _ in range(1500):  # 约 4 年，防死循环
        if _day_ok(day, doms, dows, months):
            floor = floor_hm if first else 0
            for h in sorted(hours):
                for m in sorted(mins):
                    if h * 60 + m >= floor:
                        cand = datetime(day.year, day.month, day.day, h, m, tzinfo=TZ_CN)
                        return cand.timestamp()
        first = False
        day += timedelta(days=1)
    raise ValueError(f"cron 永无匹配时刻: {expr!r}（检查日/月/周组合）")


def _step_of(vals: set[int], lo: int, hi: int) -> int | None:
    """vals 恰好是从 lo 起步长 n（n≥2）的等差数列时返回 n，否则 None。"""
    if len(vals) < 2 or min(vals) != lo:
        return None
    ordered = sorted(vals)
    n = ordered[1] - ordered[0]
    if n < 2 or ordered != list(range(lo, hi + 1, n)):
        return None
    return n


def _human_time(mins: set[int], hours: set[int]) -> str | None:
    """时刻部分：单一时刻 HH:MM，或 每小时/每N分钟/每N小时 形态；认不出返回 None。"""
    if len(mins) == 1 and len(hours) == 1:
        return f"{next(iter(hours)):02d}:{next(iter(mins)):02d}"
    if hours == _ALL_HOURS:
        if mins == {0}:
            return "每小时"
        if mins == _ALL_MINS:
            return "每分钟"
        if len(mins) == 1:
            return f"每小时{next(iter(mins))}分"
        n = _step_of(mins, 0, 59)
        if n and 60 % n == 0:
            return f"每{n}分钟"
    if mins == {0}:
        n = _step_of(hours, 0, 23)
        if n and 24 % n == 0:
            return f"每{n}小时"
    return None


def cron_human(expr: str) -> str:
    """常见形态转中文可读描述，认不出就原样返回。仅展示用。"""
    try:
        mins, hours, doms, months, dows = parse_cron(expr)
    except ValueError:
        return f"cron {expr}"
    t = _human_time(mins, hours)
    if t is None:
        return f"cron {expr}"
    if doms == _ALL_DOM and months == _ALL_MONTHS and dows == _ALL_DOW:
        return t if ":" not in t else f"每天 {t}"
    if ":" not in t:  # 间隔型（每小时/每30分钟/每2小时）不再叠加日期限定渲染
        return f"cron {expr}"
    if doms == _ALL_DOM and months == _ALL_MONTHS:
        if dows == _ALL_DOW:
            return f"每天 {t}"
        if dows == set(range(1, 6)):
            return f"工作日 {t}"
        if dows == {0, 6}:
            return f"周末 {t}"
        ordered = sorted(dows, key=lambda x: (x == 0, x))  # 周日排最后
        if len(ordered) == 1:
            return f"每周{_DOW_CN[ordered[0]]} {t}"
        return f"每周{''.join(_DOW_CN[d] for d in ordered)} {t}"
    if months == _ALL_MONTHS and dows == _ALL_DOW:
        if doms == {_DOM_LAST}:
            return f"每月最后一天 {t}"
        n = _step_of(doms, 1, 31)
        if n:
            return f"每{n}天 {t}"
        return f"每月{'、'.join(str(d) for d in sorted(doms))}号 {t}"
    if len(months) == 1 and len(doms) == 1 and dows == _ALL_DOW:
        return f"每年{next(iter(months))}月{next(iter(doms))}日 {t}"
    n = _step_of(months, 1, 12)
    if n and dows == _ALL_DOW and doms == {1}:
        return f"每{n}个月1号 {t}"
    return f"cron {expr}"
