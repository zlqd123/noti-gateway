"""定时提醒插件（reminders）：一次性 / 每天 / 每周 / 工作日 / 每月 / 间隔 / cron，双入口。

- HTTP   ``POST /schedule``（创建）、``GET /schedule``（列表）、``DELETE /schedule/{id}``（取消）
- QQ     手机上给机器人发指令（前缀 # / / @ ＠ 之一，无 AI，纯正则 + 确定性时间解析）::

      #提醒 30分钟后 喝水
      #提醒 18:00 下班打卡         （今天没过用今天，过了自动算明天）
      #提醒 下午3点 / 晚上8点半 / 中午12点 起来活动
      #提醒 明天 09:00 起床
      #提醒 大后天 10:00 复诊 / #提醒 下周一 09:00 周报
      #提醒 2026-11-20 09:00 交报告
      #提醒 每天 09:00 吃药
      #提醒 每周一三五 10:00 站会 / #提醒 每周一至周五 09:00 打卡
      #提醒 周末 10:00 大扫除 / #提醒 周三 09:00 交周报（下一个周三）
      #提醒 工作日 09:00 站会
      #提醒 每月1号 09:00 交房租 / #提醒 每月最后一天 对账
      #提醒 每年11月20日 09:00 续费
      #提醒 每小时 汇报 / #提醒 每30分钟 喝水 / #提醒 每隔2小时 活动 / #提醒 每3天 09:00 量体重
      #提醒 0 9 * * * 背单词       （cron：分 时 日 月 周）
      #提醒 列表 / #提醒 取消 12 / #帮助
      （前缀 / @ ＠ 等价；裸子命令也认：#列表 #取消 12 #帮助）

投递路径：调度循环到点 -> Dispatcher.submit（持久化队列，复用路由/限流/重试/回执），
提交成功即推进/收尾本次触发。崩溃语义 at-least-once：提交与落库之间崩溃最多重复
一次，不丢提醒。错过补发：停机跨过触发点，重启后 grace_seconds 内补发（正文注明
原定时间）；更久远的一次性提醒标记 missed，重复提醒跳过错过的次数直接算下一次。

时间统一东八区（_cron.TZ_CN），不依赖服务器系统时区。
提醒是用户主动创建的离散事件，不是告警风暴，submit 一律带 dedup=False。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..auth import AuthError, check_request
from ..dispatcher import Dispatcher, SubmitError
from ..models import Notification
from . import PluginMeta
from ._cron import TZ_CN, cron_human, cron_next, parse_cron

log = logging.getLogger("noti.reminders")

_CMD_RE = re.compile(r"^提醒\s*(.*)$", re.S)

MAX_WHEN_LEN = 120  # when 表达式上限，防把整段正文误当时间表达式解析


_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def fmt_ts(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, TZ_CN)
    wd = _WEEKDAY_CN[dt.weekday()]
    if dt.year != datetime.now(TZ_CN).year:
        return dt.strftime("%Y-%m-%d") + f"（{wd}）" + dt.strftime(" %H:%M")
    return dt.strftime("%m-%d") + f"（{wd}）" + dt.strftime(" %H:%M")


def fmt_day(ts: float) -> str:
    """仅日期（含星期几）：11-21（周五） / 2027-03-05（周五）。"""
    dt = datetime.fromtimestamp(ts, TZ_CN)
    if dt.year != datetime.now(TZ_CN).year:
        return dt.strftime("%Y-%m-%d") + f"（{_WEEKDAY_CN[dt.weekday()]}）"
    return dt.strftime("%m-%d") + f"（{_WEEKDAY_CN[dt.weekday()]}）"


# --------------------------------------------------------------------------
# when 表达式解析：相对时长 / 时刻(含时段限定词) / 日期 / 每天 / 每周(多天) / 工作日 /
#                  周末 / 每月 / 每年 / 每小时 / 间隔 / cron
# --------------------------------------------------------------------------
@dataclass
class ParsedWhen:
    rec: str                     # once | daily | weekly | weekdays | cron
    at_ts: float | None          # once 的触发时刻
    cron: str | None             # 重复提醒的 5 段表达式
    human: str                   # 可读规则（展示用）


_UNIT_S = {
    "s": 1, "秒": 1, "秒钟": 1,
    "m": 60, "分": 60, "分钟": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "时": 3600, "小时": 3600,
    "d": 86400, "天": 86400, "日": 86400,
}
_REL_HALF_RE = re.compile(r"^半\s*小时\s*(?:之?后)?")
_REL_RE = re.compile(
    r"^(?:in\s+)?(\d+)\s*(秒钟?|分钟?|小时|时|天|日|min(?:ute)?s?|[smhd])\s*(?:之?后)?",
    re.IGNORECASE,
)
_TOD_COLON_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
# 时刻的「分」部分：半 / 数字(分?) / 中文数字(分?)，数字与中文时刻共用
_TOD_MIN = r"(?:(半)|(\d{1,2})分?|([零〇一二两三四五六七八九十]{1,3})分?)?"
_TOD_DIAN_RE = re.compile(r"(\d{1,2})[点时]" + _TOD_MIN)
_TOD_CN_RE = re.compile(r"([零〇一二两三四五六七八九十]{1,3})[点时]" + _TOD_MIN)
_CN_DIG = {"零": 0, "〇": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 时段限定词（凌晨/早上/中午/下午/晚上…），后面跟时刻；换算见 _period_hour
_PERIOD_RE = re.compile(r"(凌晨|清晨|早上|早晨|上午|中午|午后|下午|傍晚|黄昏|晚上|夜里|夜晚|深夜|半夜|午夜)\s*")
_DAY_PRE_RE = re.compile(r"^(今天|今晚|明天|后天|大后天|下下周[一二三四五六日天]|下周[一二三四五六日天])\s*")
_NEXTW_BAD_RE = re.compile(r"^下下?周(?![一二三四五六日天])")  # 裸「下周」防被当裸时刻静默算成明天
_DAILY_RE = re.compile(r"^每[天日]\s*")
_WEEKLY_RE = re.compile(r"^每周((?:(?:星期|周)?[一二三四五六日天1-7][至到、,，和/]?)+)\s*")
_WEEKEND_RE = re.compile(r"^每?周末\s*")
_WEEKDAYS_RE = re.compile(r"^工作日\s*")
_MONTHLY_HEAD_RE = re.compile(r"^每月\s*")
_MONTHLY_DAY_RE = re.compile(r"最后一天|末|(?:[0-9]{1,2}|[零〇一二三四五六七八九十]{1,3})(?:号|日)")
_YEARLY_RE = re.compile(r"^每年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]\s*")
_HOURLY_RE = re.compile(r"^每小时\s*(?:第?\s*(\d{1,2})\s*分)?\s*")
_INTERVAL_RE = re.compile(
    r"^每(?:隔)?\s*(半)?\s*(\d{1,4}|[零〇一二两三四五六七八九十]{1,3})?\s*个?"
    r"(秒钟?|分钟?|小时|时|天|日|月|周|星期|季度|季|年|min(?:ute)?s?|[smhd])\s*",
    re.IGNORECASE,
)
_BARE_WEEK_RE = re.compile(r"^(?:星期|周)([一二三四五六日天1-7])\s*")
_DATE_LONG_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")
_DATE_SHORT_RE = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})")
_CN_DATE_RE = re.compile(r"(?:(\d{2,4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_COMPACT_DATE_RE = re.compile(r"(\d{8}|\d{6}|\d{4})(?=\s|$)")
_CRON_RE = re.compile(r"^(?:cron\s+)?((?:[0-9*,\-/L]+\s+){4}[0-9*,\-/L]+)")
_WEEK_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
            "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}


def _cn_num(s: str) -> int | None:
    """中文数字 -> 0-99（零〇一二两三四五六七八九十，支持 十X/X十/X十Y/零五）。非法返回 None。"""
    s = s.replace("〇", "零")
    if not s:
        return None
    if "十" in s:
        a, _, b = s.partition("十")
        if a:
            if a not in _CN_DIG:
                return None
            v = _CN_DIG[a] * 10
        else:
            v = 10
        if b:
            if len(b) > 1 or b not in _CN_DIG:
                return None
            v += _CN_DIG[b]
        return v
    if len(s) == 1:
        return _CN_DIG.get(s)
    if len(s) == 2 and s[0] == "零" and s[1] in _CN_DIG:
        return _CN_DIG[s[1]]  # 零五 = 05
    return None


def _tod_parts(hour_s: str, half: str | None, min_dig: str | None, min_cn: str | None) -> tuple[int, int] | None:
    """时刻四段（时/半/数字分/中文分）-> (时, 分)，越界返回 None。"""
    h = int(hour_s) if hour_s.isdigit() else _cn_num(hour_s)
    if h is None or not (0 <= h <= 23):
        return None
    if half:
        mi = 30
    elif min_dig:
        mi = int(min_dig)
    elif min_cn:
        mi = _cn_num(min_cn)
    else:
        mi = 0
    if mi is None or not (0 <= mi <= 59):
        return None
    return h, mi


def _match_bare_tod(s: str) -> tuple[int, int, int] | None:
    """前缀匹配裸时刻（09:05 / 9点15分 / 9点半 / 九点十五 / 二十一点半 / 9时），
    返回 (时, 分, 消耗字符数)。"""
    m = _TOD_COLON_RE.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return (h, mi, m.end()) if h < 24 and mi < 60 else None
    for rx in (_TOD_DIAN_RE, _TOD_CN_RE):
        m = rx.match(s)
        if m:
            parts = _tod_parts(m.group(1), m.group(2), m.group(3), m.group(4))
            if parts is not None:
                return *parts, m.end()
    return None


def _period_hour(name: str, h: int) -> int | None:
    """时段限定词 + 输入小时 -> 实际小时；组合非法（如 早上15点）返回 None。"""
    if name == "凌晨":
        return h if 0 <= h <= 5 else None
    if name in ("清晨", "早上", "早晨", "上午"):
        return h if 4 <= h <= 11 else None
    if name == "中午":
        if h in (1, 2):
            return h + 12  # 中午1点 = 13点
        return h if 10 <= h <= 13 else None
    if name in ("午后", "下午"):
        if 1 <= h <= 11:
            return h + 12
        return 12 if h == 12 else None
    if name in ("傍晚", "黄昏"):
        return h + 12 if 4 <= h <= 7 else None
    if name in ("晚上", "夜里", "夜晚"):
        if 7 <= h <= 11:
            return h + 12
        return 0 if h in (0, 12) else None  # 晚上12点 = 午夜
    if name in ("深夜", "半夜", "午夜"):
        if h <= 3:
            return h
        return {11: 23, 12: 0}.get(h)
    return None


def _match_tod(s: str) -> tuple[int, int, int] | None:
    """前缀匹配时刻，可带时段限定词（下午3点 / 晚上8点半 / 凌晨2点 / 上午9:00），
    返回 (时, 分, 消耗字符数)。"""
    m = _PERIOD_RE.match(s)
    if m:
        base = _match_bare_tod(s[m.end():])
        if base is None:
            return None
        h = _period_hour(m.group(1), base[0])
        if h is None:
            return None
        return h, base[1], m.end() + base[2]
    return _match_bare_tod(s)


def _local_dt(day, h: int, mi: int, sec: int = 0) -> float:
    return datetime(day.year, day.month, day.day, h, mi, sec, tzinfo=TZ_CN).timestamp()


def _p_relative(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    m = _REL_HALF_RE.match(text)
    if m:
        return ParsedWhen("once", now + 1800, None, "30分钟后"), m.end()
    m = _REL_RE.match(text)
    if not m:
        return None
    n = int(m.group(1))
    sec = _UNIT_S.get(m.group(2).lower())
    if sec is None:
        return None
    return ParsedWhen("once", now + n * sec, None, f"{n}{m.group(2)}后"), m.end()


def _p_cron(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    m = _CRON_RE.match(text)
    if not m:
        return None
    try:
        parse_cron(m.group(1))
    except ValueError:
        return None  # cron 形状但非法：让别的模式试，最后统一给「看不懂」
    return ParsedWhen("cron", None, m.group(1), cron_human(m.group(1))), m.end()


def _p_daily(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    m = _DAILY_RE.match(text)
    if not m:
        return None
    tod = _match_tod(text[m.end():])
    if not tod:
        return None
    h, mi, w = tod
    return ParsedWhen("daily", None, f"{mi} {h} * * *", f"每天 {h:02d}:{mi:02d}"), m.end() + w


def _parse_weekdays(s: str) -> list[int] | None:
    """每周后的星期列表 -> 升序去重 1-7；支持 一三五 / 一、周三 / 周一至五 / 星期六日。"""
    toks = re.findall(r"(?:星期|周)?[一二三四五六日天1-7]|[至到、,，和/]+", s)
    days: list[int] = []
    pending = ""
    for t in toks:
        if re.fullmatch(r"[至到、,，和/]+", t):
            pending = t
            continue
        d = _WEEK_CN[t[-1]]
        if pending in ("至", "到") and days:
            if days[-1] >= d:
                return None
            days.extend(range(days[-1] + 1, d + 1))
        else:
            days.append(d)
        pending = ""
    if not days or pending:
        return None
    return sorted(set(days))


def _weekday_human(days: list[int]) -> str:
    """[1,3,5] -> 一三五；[1..5] -> 一至五；[7] -> 日。"""
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{'一二三四五六日'[days[0] - 1]}至{'一二三四五六日'[days[-1] - 1]}"
    return "".join("一二三四五六日"[d - 1] for d in days)


def _p_weekly(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    m = _WEEKLY_RE.match(text)
    if not m:
        return None
    days = _parse_weekdays(m.group(1))
    if not days:
        return None
    tod = _match_tod(text[m.end():])
    if not tod:
        raise ValueError(f"缺时刻：每周{m.group(1)}，请补上时刻（如 每周一 9点）")
    h, mi, w = tod
    dow = ",".join(str(d) for d in days)
    return ParsedWhen("weekly", None, f"{mi} {h} * * {dow}",
                      f"每周{_weekday_human(days)} {h:02d}:{mi:02d}"), m.end() + w


def _p_weekend(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    m = _WEEKEND_RE.match(text)
    if not m:
        return None
    tod = _match_tod(text[m.end():])
    if not tod:
        raise ValueError("缺时刻：周末，请补上时刻（如 周末 9点）")
    h, mi, w = tod
    return ParsedWhen("weekly", None, f"{mi} {h} * * 0,6", f"周末 {h:02d}:{mi:02d}"), m.end() + w


def _p_weekdays(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    m = _WEEKDAYS_RE.match(text)
    if not m:
        return None
    tod = _match_tod(text[m.end():])
    if not tod:
        return None
    h, mi, w = tod
    return ParsedWhen("weekdays", None, f"{mi} {h} * * 1-5", f"工作日 {h:02d}:{mi:02d}"), m.end() + w


def _parse_month_days(run: str) -> list[int] | None:
    """「1号、15号」这类日期串 -> 升序去重 1-31；解析不了返回 None。"""
    days: list[int] = []
    for tok in re.split(r"[、,，和/]+", run):
        tok = tok.strip().rstrip("号日")
        if not tok:
            return None
        v = int(tok) if tok.isdigit() else _cn_num(tok)
        if v is None or not (1 <= v <= 31):
            return None
        days.append(v)
    return sorted(set(days)) if days else None


def _p_monthly(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """每月1号 09:00 / 每月1、15号 9点 / 每月最后一天 09:00 / 每月末 9点。"""
    head = _MONTHLY_HEAD_RE.match(text)
    if not head:
        return None
    rest = text[head.end():]
    cands = []  # (结束位置, 日期串, (时刻, 日期与时刻间的空白宽度))
    for m in _MONTHLY_DAY_RE.finditer(rest):
        gap = rest[m.end():]
        stripped = gap.lstrip()
        tod = _match_tod(stripped)
        cands.append((m.end(), m.group(0),
                      None if tod is None else (tod, len(gap) - len(stripped))))
    if not cands:
        return None
    ok = [c for c in cands if c[2]]
    if not ok:  # 日期合法但缺时刻：给出比「看不懂」更具体的提示
        raise ValueError(f"缺时刻：每月{cands[-1][1]}，请补上时刻（如 每月{cands[-1][1]} 9点）")
    end, run, (tod, pad) = ok[-1]  # 取与时刻匹配的最长日期串（兼容「1号、15号」逐段命中）
    if run in ("最后一天", "末"):
        dom, human_day = "L", "最后一天"
    else:
        days = _parse_month_days(rest[:end])
        if not days:
            raise ValueError(f"每月{run} 日期非法（1-31 或 最后一天）")
        dom = ",".join(str(d) for d in days)
        human_day = "、".join(str(d) for d in days) + "号"
    h, mi, w = tod
    return ParsedWhen("cron", None, f"{mi} {h} {dom} * *",
                      f"每月{human_day} {h:02d}:{mi:02d}"), head.end() + end + pad + w


def _p_yearly(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """每年11月20日 09:00 / 每年2月29日 9点（仅在闰年触发）。"""
    m = _YEARLY_RE.match(text)
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        datetime(2024, mo, d)  # 2024 是闰年：2月29日放行，2月30日拦截
    except ValueError:
        raise ValueError(f"每年{mo}月{d}日不存在") from None
    tod = _match_tod(text[m.end():])
    if not tod:
        raise ValueError(f"缺时刻：每年{mo}月{d}日，请补上时刻（如 每年{mo}月{d}日 9点）")
    h, mi, w = tod
    return ParsedWhen("cron", None, f"{mi} {h} {d} {mo} *",
                      f"每年{mo}月{d}日 {h:02d}:{mi:02d}"), m.end() + w


def _p_hourly(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """每小时 / 每小时30分 / 每小时第15分。"""
    m = _HOURLY_RE.match(text)
    if not m:
        return None
    mi = int(m.group(1)) if m.group(1) else 0
    if mi > 59:
        raise ValueError(f"每小时的分钟数需在 0-59：每小时{mi}分")
    human = "每小时" if mi == 0 else f"每小时{mi}分"
    return ParsedWhen("cron", None, f"{mi} * * * *", human), m.end()


def _p_interval(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """间隔重复：每N分钟 / 每半小时 / 每N小时 / 每N天 09:00 / 每N个月 9点 / 每季度 / 每半年。

    全部归一化为 5 段 cron，因此分钟间隔需整除 60，秒级/周间隔/年间隔明确报错。
    """
    m = _INTERVAL_RE.match(text)
    if not m:
        return None
    half, n_s, unit = m.group(1), m.group(2), m.group(3)
    u = unit.lower()
    if half:  # 每半小时 / 每半年 / 每半个月
        if u in ("h", "小时", "时"):
            return ParsedWhen("cron", None, "*/30 * * * *", "每半小时"), m.end()
        if u in ("y", "年"):
            tod = _match_tod(text[m.end():])
            if not tod:
                raise ValueError("每半年请指定时刻，如：每半年 9点")
            h, mi, w = tod
            return ParsedWhen("cron", None, f"{mi} {h} 1 */6 *",
                              f"每半年（1号起） {h:02d}:{mi:02d}"), m.end() + w
        if u == "月":
            tod = _match_tod(text[m.end():])
            if not tod:
                raise ValueError("每半个月请指定时刻，如：每半个月 9点")
            h, mi, w = tod
            return ParsedWhen("cron", None, f"{mi} {h} 1,15 * *",
                              f"每月1、15号（每半月） {h:02d}:{mi:02d}"), m.end() + w
        raise ValueError("「每半…」只支持 半小时 / 半年 / 半个月")
    n = 1
    if n_s:
        n = int(n_s) if n_s.isdigit() else (_cn_num(n_s) or 0)
        if n < 1:
            raise ValueError(f"间隔数字非法：每{n_s}{unit}")
    if u in ("s", "秒", "秒钟"):
        raise ValueError("间隔最小单位是分钟（cron 不支持秒级），如：每30分钟")
    if u in ("w", "周", "星期"):
        if n == 1:
            return None  # 每周X 归 _p_weekly；裸「每周」交给通用提示
        raise ValueError("周间隔无法用 cron 精确表达，请改用：每周一 … / 每3天 09:00")
    if u in ("y", "年"):
        if n == 1:
            raise ValueError("每年请指定日期，如：每年11月20日 9点")
        raise ValueError("年间隔无法用 cron 表达，请用：每年M月D日 9点")
    if u in ("m", "min", "mins", "minute", "minutes", "分", "分钟"):
        if n == 1:
            return ParsedWhen("cron", None, "* * * * *", "每分钟"), m.end()
        if 60 % n:
            raise ValueError(f"每{n}分钟无法用 cron 精确表达（需整除60），可用：每15/20/30分钟")
        return ParsedWhen("cron", None, f"*/{n} * * * *", f"每{n}分钟"), m.end()
    if u in ("h", "小时", "时"):
        if n == 1:
            return ParsedWhen("cron", None, "0 * * * *", "每小时"), m.end()
        if n > 23:
            raise ValueError("小时数需在 1-23")
        return ParsedWhen("cron", None, f"0 */{n} * * *", f"每{n}小时"), m.end()
    if u in ("d", "天", "日"):
        tod = _match_tod(text[m.end():])
        if not tod:
            if n == 1:
                return None  # 裸「每天」由 _p_daily/通用提示兜底
            raise ValueError(f"每{n}天请指定时刻，如：每{n}天 09:00")
        if n > 31:
            raise ValueError("天数需在 1-31")
        h, mi, w = tod
        expr = f"{mi} {h} * * *" if n == 1 else f"{mi} {h} */{n} * *"
        human = ("每天" if n == 1 else f"每{n}天") + f" {h:02d}:{mi:02d}"
        return ParsedWhen("cron", None, expr, human), m.end() + w
    if u == "月":
        tod = _match_tod(text[m.end():])
        if not tod:
            if n == 1:
                return None  # 裸「每月」交给通用提示（带日期请用 每月X号）
            raise ValueError(f"每{n}个月请指定时刻，如：每{n}个月 9点")
        if n > 12:
            raise ValueError("月数需在 1-12")
        h, mi, w = tod
        expr = f"{mi} {h} 1 * *" if n == 1 else f"{mi} {h} 1 */{n} *"
        human = (f"每月1号 {h:02d}:{mi:02d}" if n == 1
                 else f"每{n}个月（1号起） {h:02d}:{mi:02d}")
        return ParsedWhen("cron", None, expr, human), m.end() + w
    if u in ("q", "季度", "季"):
        tod = _match_tod(text[m.end():])
        if not tod:
            raise ValueError("每季度请指定时刻，如：每季度 9点")
        h, mi, w = tod
        return ParsedWhen("cron", None, f"{mi} {h} 1,4,7,10 *",
                          f"每季度 {h:02d}:{mi:02d}"), m.end() + w
    return None


def _p_date(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    for rx, has_year in ((_DATE_LONG_RE, True), (_DATE_SHORT_RE, False)):
        m = rx.match(text)
        if not m:
            continue
        g = m.groups()
        try:
            if has_year:
                y, mo, d, h, mi = int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4])
                sec = int(g[5] or 0)
            else:
                y = datetime.now(TZ_CN).year
                mo, d, h, mi = int(g[0]), int(g[1]), int(g[2]), int(g[3])
                sec = 0
            ts = _local_dt(datetime(y, mo, d).date(), h, mi, sec)
        except ValueError:
            return None
        return ParsedWhen("once", ts, None, fmt_ts(ts)), m.end()
    return None


def _p_cn_date(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """中文日期（[yy]yy年M月D日/号），日期后必须紧跟时刻。"""
    m = _CN_DATE_RE.match(text)
    if not m:
        return None
    ys = m.group(1)
    today = datetime.fromtimestamp(now, TZ_CN)
    y = today.year if ys is None else (2000 + int(ys) if len(ys) == 2 else int(ys))
    mo, d = int(m.group(2)), int(m.group(3))
    try:
        day = datetime(y, mo, d).date()
    except ValueError:
        return None
    gap = text[m.end():]
    tod = _match_tod(gap.lstrip())
    if tod is None:
        raise ValueError(
            f"日期缺时刻：{m.group(0).strip()} 被理解为 {y}-{mo:02d}-{d:02d}，"
            f"请补上时刻（如 {m.group(0).strip()} 9点）"
        )
    h, mi = tod[0], tod[1]
    w = tod[2] + len(gap) - len(gap.lstrip())
    ts = _local_dt(day, h, mi)
    return ParsedWhen("once", ts, None, fmt_ts(ts)), m.end() + w


def _p_compact_date(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """紧凑数字日期（YYYYMMDD / YYMMDD / MMDD，须是独立 token），后必须紧跟时刻。"""
    m = _COMPACT_DATE_RE.match(text)
    if not m:
        return None
    s = m.group(1)
    today = datetime.fromtimestamp(now, TZ_CN)
    try:
        if len(s) == 8:
            y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
        elif len(s) == 6:
            y, mo, d = 2000 + int(s[:2]), int(s[2:4]), int(s[4:])
        else:
            y, mo, d = today.year, int(s[:2]), int(s[2:])
        day = datetime(y, mo, d).date()
    except ValueError:
        return None
    gap = text[m.end():]
    tod = _match_tod(gap.lstrip())
    if tod is None:
        hint = ""
        if len(s) == 4 and int(s[:2]) <= 23 and int(s[2:]) <= 59:
            hint = f"；若想表达时刻请写 {s[:2]}:{s[2:]}"
        raise ValueError(
            f"日期缺时刻：{s} 被理解为 {mo}月{d}日，请补上时刻（如 {s} 9点）{hint}"
        )
    h, mi = tod[0], tod[1]
    w = tod[2] + len(gap) - len(gap.lstrip())
    ts = _local_dt(day, h, mi)
    return ParsedWhen("once", ts, None, fmt_ts(ts)), m.end() + w


def _p_day_prefix(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    if _NEXTW_BAD_RE.match(text):
        raise ValueError("下周请指定星期几，如：下周一 09:00")
    m = _DAY_PRE_RE.match(text)
    if not m:
        return None
    tod = _match_tod(text[m.end():])
    if not tod:
        return None
    word = m.group(1)
    today = datetime.fromtimestamp(now, TZ_CN).date()
    fixed = {"今天": 0, "今晚": 0, "明天": 1, "后天": 2, "大后天": 3}
    if word in fixed:
        offset = fixed[word]
    else:  # 下周X / 下下周X：下周的星期X 距今 = 7 - 今天周几 + X
        offset = (7 if word.startswith("下下") else 0) + 7 - today.isoweekday() + _WEEK_CN[word[-1]]
    h, mi, w = tod
    day = today + timedelta(days=offset)
    ts = _local_dt(day, h, mi)
    if offset == 0 and ts <= now:
        raise ValueError(f"今天的 {h:02d}:{mi:02d} 已经过去了，用「明天」或具体日期")
    return ParsedWhen("once", ts, None, fmt_ts(ts)), m.end() + w


def _p_bare_weekday(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    """裸星期（周一/星期五 09:00）：下一个对应的星期几，今天未过也算今天。"""
    m = _BARE_WEEK_RE.match(text)
    if not m:
        return None
    tod = _match_tod(text[m.end():])
    if not tod:
        return None
    h, mi, w = tod
    today = datetime.fromtimestamp(now, TZ_CN).date()
    offset = (_WEEK_CN[m.group(1)] - today.isoweekday()) % 7
    ts = _local_dt(today + timedelta(days=offset), h, mi)
    if offset == 0 and ts <= now:
        offset = 7
        ts = _local_dt(today + timedelta(days=offset), h, mi)
    return ParsedWhen("once", ts, None, fmt_ts(ts)), m.end() + w


def _p_bare_tod(text: str, now: float) -> tuple[ParsedWhen, int] | None:
    tod = _match_tod(text)
    if not tod:
        return None
    h, mi, w = tod
    day = datetime.fromtimestamp(now, TZ_CN).date()
    ts = _local_dt(day, h, mi)
    if ts <= now:  # 没过用今天，过了自动算明天
        ts = _local_dt(day + timedelta(days=1), h, mi)
    return ParsedWhen("once", ts, None, fmt_ts(ts)), w


# 顺序即优先级：cron/固定前缀在前，裸时刻兜底；_split_when 取「匹配最长」者。
# 同长度时靠前的赢：每天/每小时 与间隔模式重叠处，交由语义更明确的模式。
_PATTERNS = (_p_cron, _p_daily, _p_weekly, _p_weekend, _p_weekdays, _p_monthly,
             _p_yearly, _p_hourly, _p_interval, _p_date, _p_cn_date,
             _p_compact_date, _p_relative, _p_day_prefix, _p_bare_weekday, _p_bare_tod)

_WHEN_USAGE = (
    "支持：30分钟后 / 18:00 / 九点十五 / 下午3点 / 晚上8点半 / 明天 09:00 / 下周一 09:00 / "
    "2026-11-20 09:00 / 11月15日 9点 / 1115 9点 / 每天 09:00 / 每周一三五 10:00 / 周末 09:00 / "
    "工作日 09:00 / 每月1号 09:00 / 每月最后一天 09:00 / 每年11月20日 09:00 / "
    "每小时 / 每30分钟 / 每隔2小时 / 每3天 09:00 / cron(分 时 日 月 周)"
)


def _split_when(text: str, now: float | None = None) -> tuple[ParsedWhen, str]:
    """命令用：从「时间表达式+内容」里拆出规则与内容。取匹配最长的模式。"""
    now = time.time() if now is None else now
    text = text.strip()
    best: tuple[ParsedWhen, int] | None = None
    err: ValueError | None = None
    for fn in _PATTERNS:
        try:
            r = fn(text, now)
        except ValueError as e:  # 模式命中但语义非法（如今天的时间已过）：保留报错
            err = err or e
            continue
        if r and (best is None or r[1] > best[1]):
            best = r
    if best is None:
        raise err or ValueError(f"时间看不懂: {text[:60]!r}（{_WHEN_USAGE}）")
    return best[0], text[best[1]:].strip()


def parse_when(expr: str, now: float | None = None) -> ParsedWhen:
    """HTTP 用：整个字符串就是一个时间表达式（不允许尾随内容，内容放 title/body）。"""
    now = time.time() if now is None else now
    expr = (expr or "").strip()
    if not expr:
        raise ValueError(f"时间表达式为空（{_WHEN_USAGE}）")
    if len(expr) > MAX_WHEN_LEN:
        raise ValueError(f"时间表达式过长（>{MAX_WHEN_LEN} 字符），内容请放 title/body")
    pw, content = _split_when(expr, now)
    if content:
        raise ValueError(f"时间表达式带上了多余内容 {content!r}（内容请放 title/body）")
    return pw


def _parse_at(v: Any) -> ParsedWhen:
    """HTTP 的 at 字段：epoch 秒或 ISO 8601（无时区按东八区）。"""
    if isinstance(v, bool):
        raise ValueError("at 不能是布尔值")
    if isinstance(v, (int, float)):
        return ParsedWhen("once", float(v), None, fmt_ts(float(v)))
    s = str(v).strip()
    try:
        ts = float(s)
        return ParsedWhen("once", ts, None, fmt_ts(ts))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"at 不是合法时间: {s!r}（ISO 8601 或 epoch 秒）") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    ts = dt.timestamp()
    return ParsedWhen("once", ts, None, fmt_ts(ts))


def _parse_cron_only(expr: str) -> ParsedWhen:
    expr = expr.strip()
    parse_cron(expr)  # 非法即抛 ValueError
    return ParsedWhen("cron", None, expr, cron_human(expr))


# --------------------------------------------------------------------------
# 存储：同一 SQLite 文件里独立的 reminders 表
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    body         TEXT    DEFAULT '',
    rec          TEXT    NOT NULL DEFAULT 'once',   -- once|daily|weekly|weekdays|cron
    at_ts        REAL,                               -- once 的目标时刻
    cron         TEXT,                               -- 重复提醒的 5 段表达式
    human        TEXT    DEFAULT '',                 -- 创建时可读规则（展示用）
    state        TEXT    NOT NULL DEFAULT 'active',  -- active|done|cancelled|missed
    next_at      REAL,                               -- 下次触发（调度字段）
    err_count    INTEGER NOT NULL DEFAULT 0,
    last_err     TEXT,
    fired_count  INTEGER NOT NULL DEFAULT 0,
    last_fired   REAL,
    created_at   REAL    NOT NULL,
    created_by   TEXT    DEFAULT ''                  -- 'qq:<openid前8位>' / 'http:<source>'
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(state, next_at);
"""


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


class ReminderStore:
    """提醒的增删查与调度状态推进。同步方法，调用方用 asyncio.to_thread 包裹。"""

    def __init__(self, db_path: str) -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._db.close()

    # ---------------- 写 ----------------

    def create(self, pw: ParsedWhen, title: str, body: str, created_by: str) -> dict:
        now = time.time()
        if pw.rec == "once":
            if pw.at_ts is None:
                raise ValueError("once 提醒缺少触发时刻")
            next_at: float | None = pw.at_ts
        else:
            if not pw.cron:
                raise ValueError("重复提醒缺少 cron 表达式")
            next_at = cron_next(pw.cron, now)  # 永不匹配的表达式在这里报错，不会入库
        cur = self._db.execute(
            """INSERT INTO reminders
               (title, body, rec, at_ts, cron, human, state, next_at, created_at, created_by)
               VALUES (?,?,?,?,?,?,'active',?,?,?)""",
            (title, body, pw.rec, pw.at_ts, pw.cron, pw.human, next_at, now, created_by),
        )
        self._db.commit()
        rid = cur.lastrowid
        return _row(self._db.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone())  # type: ignore[arg-type]

    def cancel(self, rid: int) -> dict | None:
        cur = self._db.execute(
            "UPDATE reminders SET state = 'cancelled' WHERE id = ? AND state = 'active'",
            (rid,),
        )
        self._db.commit()
        return self.get(rid) if cur.rowcount == 1 else None

    def mark_fired(self, rid: int, next_at: float | None, state: str) -> None:
        self._db.execute(
            """UPDATE reminders SET state = ?, next_at = ?, fired_count = fired_count + 1,
                   last_fired = ?, err_count = 0, last_err = NULL WHERE id = ?""",
            (state, next_at, time.time(), rid),
        )
        self._db.commit()

    def mark_error(self, rid: int, next_at: float, err_count: int, err: str) -> None:
        self._db.execute(
            "UPDATE reminders SET next_at = ?, err_count = ?, last_err = ? WHERE id = ?",
            (next_at, err_count, err[:300], rid),
        )
        self._db.commit()

    def mark_missed(self, rid: int, err: str) -> None:
        self._db.execute(
            "UPDATE reminders SET state = 'missed', next_at = NULL, last_err = ? WHERE id = ?",
            (err[:300], rid),
        )
        self._db.commit()

    def skip_to_next(self, rid: int, next_at: float, err: str) -> None:
        """重复提醒跳过错过的触发：保持 active，仅推进 next_at。"""
        self._db.execute(
            "UPDATE reminders SET next_at = ?, last_err = ? WHERE id = ?",
            (next_at, err[:300], rid),
        )
        self._db.commit()

    # ---------------- 读 ----------------

    def get(self, rid: int) -> dict | None:
        return _row(self._db.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone())

    def list_items(self, state: str | None = "active", limit: int = 50) -> list[dict]:
        if state:
            cur = self._db.execute(
                "SELECT * FROM reminders WHERE state = ? ORDER BY next_at ASC LIMIT ?",
                (state, limit),
            )
        else:
            cur = self._db.execute(
                "SELECT * FROM reminders ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in cur.fetchall()]

    def due(self, now: float) -> list[dict]:
        return [
            dict(r)
            for r in self._db.execute(
                "SELECT * FROM reminders WHERE state = 'active' AND next_at IS NOT NULL AND next_at <= ? "
                "ORDER BY next_at ASC",
                (now,),
            ).fetchall()
        ]


# --------------------------------------------------------------------------
# 插件：调度循环 + HTTP API + QQ 命令
# --------------------------------------------------------------------------
_HELP = (
    "⏰ 定时提醒（#提醒 帮助 看这里）：\n"
    "#提醒 30分钟后 内容 / #提醒 半小时后 内容\n"
    "#提醒 18:00 内容（过了就算明天）\n"
    "#提醒 九点十五 / 9点15 / 下午3点 / 晚上8点半 内容\n"
    "#提醒 明天 09:00 内容 / #提醒 大后天 10:00 内容\n"
    "#提醒 下周一 09:00 内容 / #提醒 周三 09:00 内容（下一个周三）\n"
    "#提醒 2026-11-20 09:00 内容\n"
    "（日期也认：11-20 09:00 / 1120 09:00 / 11月20日 09:00 / 26年11月20日 09:00）\n"
    "#提醒 每天 09:00 内容\n"
    "#提醒 每周一三五 10:00 内容 / #提醒 每周一至五 09:00 内容\n"
    "#提醒 周末 10:00 内容 / #提醒 工作日 09:00 内容\n"
    "#提醒 每月1号 09:00 内容 / #提醒 每月最后一天 内容\n"
    "#提醒 每年11月20日 09:00 内容\n"
    "#提醒 每小时 内容 / #提醒 每30分钟 内容 / #提醒 每隔2小时 内容\n"
    "#提醒 每3天 09:00 内容 / #提醒 每季度 09:00 内容\n"
    "#提醒 0 9 * * * 内容（cron）\n"
    "#提醒 列表 ｜ #提醒 取消 12"
)


class RemindersPlugin:
    def __init__(self, ctx: Any) -> None:
        self.cfg = ctx.cfg.reminders
        self.auth = ctx.cfg.auth
        self.disp: Dispatcher = ctx.disp
        self.hub = ctx.hub
        self.store = ReminderStore(ctx.db_path)
        self._task: asyncio.Task | None = None

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="reminders-scheduler")
        active = await asyncio.to_thread(self.store.list_items, "active", 1000)
        log.info("reminders 调度器启动 active=%d", len(active))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=5)
            self._task = None

    def _pub(self, data: dict) -> None:
        if self.hub is None:
            return
        try:
            self.hub.publish("reminder", data)  # /stream 订阅 &types=reminder 可见
        except Exception:
            log.exception("hub publish failed")

    # ---------------- 调度 ----------------

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reminders tick error")
            await asyncio.sleep(max(0.2, self.cfg.tick_seconds))

    async def _tick(self) -> None:
        now = time.time()
        for r in await asyncio.to_thread(self.store.due, now):
            late = now - float(r["next_at"])
            if late > self.cfg.grace_seconds:
                await self._on_missed(r, late)
            else:
                await self._fire(r, late)

    async def _fire(self, r: dict, late: float) -> None:
        rid = r["id"]
        body = r["body"] or ""
        if late > 60:
            note = f"(错过补发，原定 {fmt_ts(r['next_at'])})"
            body = f"{body}\n{note}" if body else note
        # 脚注：编号 + 规则 + 设置日，方便对着消息直接「#取消 N」
        footer = f"⏰ 提醒#{rid} · {r['human']} · 设于{fmt_day(r['created_at'])}"
        body = f"{body}\n{footer}" if body else footer
        n = Notification(
            source=self.cfg.source, level="info", title=r["title"], body=body,
            meta={"via": "reminder", "reminder_id": rid, "rule": r["human"]},
        )
        try:
            res = await self.disp.submit(n, wait=False, dedup=False)
        except SubmitError as e:
            await self._on_submit_error(r, e)
            return
        targets = res.get("targets") or list((res.get("results") or {}).keys())
        self._pub({"action": "fired", "id": rid, "title": r["title"],
                   "targets": targets, "late_s": int(late)})
        if r["rec"] == "once":
            await asyncio.to_thread(self.store.mark_fired, rid, None, "done")
        else:
            try:
                nxt = cron_next(r["cron"], float(r["next_at"]))
            except ValueError as e:  # 理论上建库时已校验，防御历史脏数据
                await asyncio.to_thread(self.store.mark_missed, rid, str(e))
                return
            await asyncio.to_thread(self.store.mark_fired, rid, nxt, "active")
        log.info("reminder #%d fired (%s) targets=%s", rid, r["human"], ",".join(targets))

    async def _on_missed(self, r: dict, late: float) -> None:
        rid = r["id"]
        reason = f"错过 {int(late)}s，超过补发窗口 {self.cfg.grace_seconds}s"
        if r["rec"] == "once":
            await asyncio.to_thread(self.store.mark_missed, rid, reason)
            self._pub({"action": "missed", "id": rid, "title": r["title"], "late_s": int(late)})
            log.warning("reminder #%d missed: %s", rid, reason)
        else:
            nxt = cron_next(r["cron"], time.time())  # 跳过错过的触发，直接算下一次
            await asyncio.to_thread(self.store.skip_to_next, rid, nxt, f"跳过错过的触发（迟了 {int(late)}s）")
            self._pub({"action": "missed_skip", "id": rid, "title": r["title"], "late_s": int(late)})
            log.warning("reminder #%d 跳过错过的触发: %s", rid, reason)

    async def _on_submit_error(self, r: dict, e: SubmitError) -> None:
        rid = r["id"]
        cnt = int(r["err_count"]) + 1
        if cnt >= 5:
            await asyncio.to_thread(self.store.mark_missed, rid, f"连续 {cnt} 次提交失败: {e}")
            log.error("reminder #%d 放弃: %s", rid, e)
        else:
            backoff = min(60.0, 5.0 * (2 ** (cnt - 1)))
            await asyncio.to_thread(self.store.mark_error, rid, time.time() + backoff, cnt, str(e))
            log.warning("reminder #%d 提交失败(第 %d 次): %s", rid, cnt, e)
        self._pub({"action": "error", "id": rid, "title": r["title"], "error": str(e)})

    # ---------------- QQ 指令（声明式注册，回复由框架统一发送）----------------

    async def cmd_remind(self, args: str, event: dict) -> str | None:
        """#提醒 <时间> <内容>；兼容裸子写法 #提醒 列表 / #提醒 取消 N。"""
        s = (args or "").strip()
        if s in ("列表", "list"):
            return await self._fmt_list()
        m = re.fullmatch(r"(?:取消|删除|cancel|del)\s*(\d+)", s)
        if m:
            return await self._cancel(int(m.group(1)))
        return await self._create(s, event)

    async def cmd_list(self, args: str, event: dict) -> str | None:
        return await self._fmt_list()

    async def cmd_cancel(self, args: str, event: dict) -> str | None:
        s = (args or "").strip()
        if not s.isdigit():
            return "用法：#取消 <id>（#提醒 列表 可查看 id）"
        return await self._cancel(int(s))

    async def _create(self, rest: str, event: dict) -> str:
        try:
            pw, content = _split_when(rest)
        except ValueError as e:
            return f"❌ {e}\n（发送 #提醒 帮助 查看用法）"
        lines = content.splitlines()
        title = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        if not title:
            return "❌ 提醒内容不能为空\n（发送 #提醒 帮助 查看用法）"
        openid = str((event.get("author") or {}).get("user_openid") or "")
        created_by = f"qq:{openid[:8]}" if openid else "qq"
        try:
            row = await asyncio.to_thread(self.store.create, pw, title, body, created_by)
        except ValueError as e:
            return f"❌ {e}"
        log.info("reminder #%d created via qq rule=%s", row["id"], row["human"])
        return (
            f"✅ 已添加 #{row['id']}\n"
            f"内容：{title}\n"
            f"规则：{row['human']}\n"
            f"设于：{fmt_day(row['created_at'])}\n"
            f"下次：{fmt_ts(row['next_at'])}"
        )

    async def _cancel(self, rid: int) -> str:
        row = await asyncio.to_thread(self.store.cancel, rid)
        if row is None:
            return f"没找到进行中的提醒 #{rid}（发送 #提醒 列表 查看）"
        log.info("reminder #%d cancelled via qq", rid)
        return f"🗑 已取消 #{rid}（{row['title']}）"

    async def _fmt_list(self) -> str:
        rows = await asyncio.to_thread(self.store.list_items, "active", self.cfg.max_list)
        if not rows:
            return "📋 没有进行中的提醒"
        lines = [f"📋 进行中的提醒 {len(rows)} 条："]
        for r in rows:
            lines.append(f"#{r['id']} {r['human']} {r['title']} → {fmt_ts(r['next_at'])}"
                         f"（设于{fmt_day(r['created_at'])}）")
        return "\n".join(lines)

    # ---------------- HTTP API ----------------

    async def api_create(self, req: Request) -> JSONResponse:
        raw = await req.body()
        if len(raw) > 64 * 1024:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            source = check_request(req, self.auth, raw)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            return JSONResponse({"error": f"bad json: {e}"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body 必须是 JSON 对象"}, status_code=400)
        title = str(payload.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title 不能为空"}, status_code=400)
        body = str(payload.get("body") or "")
        try:
            if "when" in payload:
                pw = parse_when(str(payload["when"]))
            elif "at" in payload:
                pw = _parse_at(payload["at"])
            elif "cron" in payload:
                pw = _parse_cron_only(str(payload["cron"]))
            else:
                return JSONResponse({"error": "需要 when / at / cron 之一"}, status_code=400)
            row = await asyncio.to_thread(self.store.create, pw, title, body, f"http:{source}")
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        log.info("reminder #%d created via http rule=%s", row["id"], row["human"])
        return JSONResponse({
            "ok": True, "id": row["id"], "rec": row["rec"], "human": row["human"],
            "next_at": row["next_at"], "next_human": fmt_ts(row["next_at"]),
        })

    async def api_list(self, req: Request) -> JSONResponse:
        try:
            check_request(req, self.auth)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        state = (req.query_params.get("state") or "active").strip().lower()
        if state == "all":
            state = None
        elif state not in ("active", "done", "cancelled", "missed"):
            return JSONResponse(
                {"error": "state 取值 active|done|cancelled|missed|all"}, status_code=400
            )
        try:
            limit = min(max(1, int(req.query_params.get("limit") or 50)), 500)
        except ValueError:
            return JSONResponse({"error": "limit 必须是整数"}, status_code=400)
        items = await asyncio.to_thread(self.store.list_items, state, limit)
        return JSONResponse({"count": len(items), "items": [
            {**r, "next_human": fmt_ts(r["next_at"]) if r["next_at"] else None}
            for r in items
        ]})

    async def api_cancel(self, rid: int, req: Request) -> JSONResponse:
        try:
            check_request(req, self.auth)
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        row = await asyncio.to_thread(self.store.cancel, rid)
        if row is None:
            return JSONResponse({"error": f"没有进行中的提醒 #{rid}"}, status_code=404)
        return JSONResponse({"ok": True, "id": row["id"], "title": row["title"]})


META = PluginMeta(name="定时提醒",
                  desc="一次性/每天/每周/工作日/每月/间隔/cron 提醒，QQ 指令 + HTTP /schedule",
                  version="1.2.0")


def setup(ctx: Any) -> None:
    """注册 reminders 插件：HTTP 端点 + 启停钩子 + 声明式指令。"""
    plg = RemindersPlugin(ctx)
    if not plg.cfg.enabled:
        log.info("reminders 插件未启用（reminders.enabled=false）")
        return
    ctx.app.add_api_route("/schedule", plg.api_create, methods=["POST"])
    ctx.app.add_api_route("/schedule", plg.api_list, methods=["GET"])
    ctx.app.add_api_route("/schedule/{rid}", plg.api_cancel, methods=["DELETE"])
    ctx.on_startup.append(plg.start)
    ctx.on_shutdown.append(plg.stop)
    ctx.command("提醒", handler=plg.cmd_remind,
                desc="创建定时提醒（一次性/每天/每周/工作日/每月/间隔/cron）",
                hint="<时间> <内容>", usage=_HELP)
    ctx.command("列表", handler=plg.cmd_list, desc="查看进行中的提醒")
    ctx.command("取消", handler=plg.cmd_cancel, desc="取消提醒", hint="<id>")
