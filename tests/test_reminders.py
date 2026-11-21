"""定时提醒插件测试。

覆盖四层：
1. when 表达式解析（相对/时刻/日期/每天/每周/工作日/cron/非法输入）
2. cron 引擎（next 计算、日与周「或」语义、周日 0/7、永不匹配报错）
3. HTTP API 全链路（HMAC 鉴权下创建/列表/取消/触发/错过补发，带 mock QQ）
4. QQ 命令层（提醒/列表/取消/帮助/非命令忽略，FakeDisp 收回复）

自带 mock 服务，不需要真实 QQ 凭证、不需要联网。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _hmac import sign_headers, signed_json_headers  # noqa: E402

from noti_gateway.config import AuthCfg, RemindersCfg  # noqa: E402
from noti_gateway.plugins._cron import TZ_CN, cron_human, cron_next  # noqa: E402
from noti_gateway.plugins.reminders import (  # noqa: E402
    ParsedWhen,
    ReminderStore,
    RemindersPlugin,
    _split_when,
    fmt_ts,
    parse_when,
)

PORT = 10093
QQ_PORT = 10094
SECRET = "test-secret"

mock_state = {"sends": 0, "last_body": {}}


def build_mock() -> FastAPI:
    app = FastAPI()

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.post("/v2/users/{oid}/messages")
    async def send(oid: str, request: Request):
        mock_state["sends"] += 1
        mock_state["last_body"] = await request.json()
        return {"id": f"M{mock_state['sends']}"}

    return app


CONFIG_TEMPLATE = """
server: {{ host: 127.0.0.1, port: {port} }}
auth: {{ keys_file: "{keys_file}" }}
limits: {{ per_source_per_minute: 600, per_target_per_minute: 600, per_target_per_day: 100000 }}
dedup: {{ enabled: true, window_seconds: 60 }}
dispatch: {{ poll_interval: 0.05, concurrency: 4, max_attempts: 4, retry_base: 0.2, retry_max: 1.0 }}
qq: {{ app_id: "T", client_secret: "S", api_base: "http://127.0.0.1:{qq_port}" }}

targets:
  - id: qq:only
    type: qq
    user_openid: "u1"
    rate_per_minute: 600

routes:
  - source: ["*"]
    targets: [qq:only]

reminders:
  enabled: true
  source: remind
  grace_seconds: 30
  tick_seconds: 0.5
"""


async def wait_up(url: str, tries: int = 80) -> bool:
    async with httpx.AsyncClient(timeout=1) as c:
        for _ in range(tries):
            try:
                await c.get(url)
                return True
            except Exception:
                await asyncio.sleep(0.25)
    return False


async def wait_for(fn, timeout: float, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        await asyncio.sleep(interval)
    return False


# --------------------------------------------------------------------------
# [1][2] 纯单元：when 解析 + cron 引擎
# --------------------------------------------------------------------------
def unit_checks(check) -> None:
    print("\n[1] when 表达式解析")
    now = datetime(2026, 11, 15, 10, 0, tzinfo=TZ_CN).timestamp()  # 周日 10:00

    check(fmt_ts(now) == "11-15（周日） 10:00", "fmt_ts 带星期几")
    check(fmt_ts(datetime(2027, 3, 5, 9, 0, tzinfo=TZ_CN).timestamp()) == "2027-03-05（周五） 09:00",
          "fmt_ts 跨年带年份与星期几")

    pw = parse_when("30分钟后", now=now)
    check(pw.rec == "once" and abs(pw.at_ts - (now + 1800)) < 1, "30分钟后 -> +1800s")

    pw = parse_when("in 5min", now=now)
    check(pw.rec == "once" and abs(pw.at_ts - (now + 300)) < 1, "in 5min -> +300s")

    ts = datetime(2026, 11, 20, 9, 0, tzinfo=TZ_CN).timestamp()
    pw = parse_when("2026-11-20 09:00", now=now)
    check(pw.rec == "once" and pw.at_ts == ts, "完整日期时刻")

    pw = parse_when("11-20 09:30", now=now)
    check(pw.at_ts == datetime(2026, 11, 20, 9, 30, tzinfo=TZ_CN).timestamp(), "年内短日期")

    pw = parse_when("每天 09:30", now=now)
    check(pw.rec == "daily" and pw.cron == "30 9 * * *", "每天 -> cron")

    pw = parse_when("每周一 09:00", now=now)
    check(pw.rec == "weekly" and pw.cron == "0 9 * * 1", "每周一 -> cron")

    pw = parse_when("工作日 9点", now=now)
    check(pw.rec == "weekdays" and pw.cron == "0 9 * * 1-5", "工作日 -> cron")

    pw = parse_when("0 9 * * *", now=now)
    check(pw.rec == "cron" and pw.cron == "0 9 * * *", "裸 cron")

    pw = parse_when("09:00", now=now)
    tomorrow9 = datetime(2026, 11, 16, 9, 0, tzinfo=TZ_CN).timestamp()
    check(pw.rec == "once" and pw.at_ts == tomorrow9, "裸时刻已过自动算明天")

    pw = parse_when("明天 9点半", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 30, tzinfo=TZ_CN).timestamp(), "明天 9点半")

    print("\n[1c] 扩展日期/时刻格式")
    ts9 = datetime(2026, 11, 15, 9, 0, tzinfo=TZ_CN).timestamp()
    pw = parse_when("26年11月15日 9点", now=now)
    check(pw.at_ts == ts9, "26年11月15日 9点")
    pw = parse_when("2026年11月15日 09:30", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 9, 30, tzinfo=TZ_CN).timestamp(), "2026年…09:30")
    pw = parse_when("11月15号 9点半", now=now)
    check(pw.at_ts == ts9 + 1800, "11月15号 9点半（无年份=当年）")
    pw = parse_when("261115 9点", now=now)
    check(pw.at_ts == ts9, "261115 9点（YYMMDD）")
    pw = parse_when("20261115 9点", now=now)
    check(pw.at_ts == ts9, "20261115 9点（YYYYMMDD）")
    pw = parse_when("1115 9点", now=now)
    check(pw.at_ts == ts9, "1115 9点（MMDD）")
    pw = parse_when("九点十五", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 15, tzinfo=TZ_CN).timestamp(),
          "九点十五（今天已过→明天）")
    pw = parse_when("二十一点半", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 21, 30, tzinfo=TZ_CN).timestamp(), "二十一点半")
    pw = parse_when("九点零五", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 5, tzinfo=TZ_CN).timestamp(), "九点零五")
    pw = parse_when("9点15", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 15, tzinfo=TZ_CN).timestamp(), "9点15（回归）")
    pw = parse_when("9点15分", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 15, tzinfo=TZ_CN).timestamp(), "9点15分（回归）")
    pw = parse_when("9点十五", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 15, tzinfo=TZ_CN).timestamp(), "9点十五（混写）")
    try:
        parse_when("1115", now=now)
        check(False, "1115 缺时刻应报错")
    except ValueError as e:
        check("日期缺时刻" in str(e) and "11:15" in str(e), "1115 报错含歧义提示")
    pw = parse_when("2026-11-20 09:00", now=now)
    check(pw.rec == "once", "破折号完整日期不受影响（回归）")

    pw, content = _split_when("261115 9点 喝水", now=now)
    check(content == "喝水" and pw.at_ts == ts9, "紧凑日期+内容切分")
    pw, content = _split_when("九点十五 喝水", now=now)
    check(content == "喝水" and pw.at_ts == datetime(2026, 11, 16, 9, 15, tzinfo=TZ_CN).timestamp(),
          "中文时刻+内容切分")

    for bad in ("今天 08:00", "18:00下班", "随便什么", "0 9 * * abc"):
        try:
            parse_when(bad, now=now)
            check(False, f"非法 when 应报错: {bad}")
        except ValueError:
            check(True, f"非法 when 报错: {bad}")

    print("\n[1b] 命令切分（时间+内容）")
    pw, content = _split_when("30分钟后喝水", now=now)
    check(content == "喝水" and pw.at_ts == now + 1800, "无空格切分")
    pw, content = _split_when("每天09:00吃药", now=now)
    check(content == "吃药" and pw.cron == "0 9 * * *", "每天+内容无空格")
    pw, content = _split_when("每周一 09:00 周会", now=now)
    check(content == "周会" and pw.cron == "0 9 * * 1", "每周一+内容")
    pw, content = _split_when("0 9 * * * 背单词", now=now)
    check(content == "背单词" and pw.rec == "cron", "cron+内容")

    print("\n[1d] 时段限定词（凌晨/早上/中午/下午/晚上）")
    pw = parse_when("下午3点", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 15, 0, tzinfo=TZ_CN).timestamp(), "下午3点 -> 15:00")
    pw = parse_when("晚上8点半", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 20, 30, tzinfo=TZ_CN).timestamp(), "晚上8点半 -> 20:30")
    pw = parse_when("早上7点", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 7, 0, tzinfo=TZ_CN).timestamp(), "早上7点（已过->明天）")
    pw = parse_when("中午12点", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 12, 0, tzinfo=TZ_CN).timestamp(), "中午12点 -> 12:00")
    pw = parse_when("中午1点", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 13, 0, tzinfo=TZ_CN).timestamp(), "中午1点 -> 13:00")
    pw = parse_when("凌晨2点", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 2, 0, tzinfo=TZ_CN).timestamp(), "凌晨2点（已过->明天）")
    pw = parse_when("下午三点半", now=now)
    check(pw.at_ts == datetime(2026, 11, 15, 15, 30, tzinfo=TZ_CN).timestamp(), "下午三点半")
    pw = parse_when("晚上12点", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 0, 0, tzinfo=TZ_CN).timestamp(), "晚上12点 = 午夜")
    pw = parse_when("上午9:00", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 9, 0, tzinfo=TZ_CN).timestamp(), "上午9:00（冒号）")
    pw = parse_when("每天 下午3点", now=now)
    check(pw.rec == "daily" and pw.cron == "0 15 * * *", "每天 下午3点")
    pw = parse_when("明天 晚上11点", now=now)
    check(pw.at_ts == datetime(2026, 11, 16, 23, 0, tzinfo=TZ_CN).timestamp(), "明天 晚上11点")
    pw, content = _split_when("下午3点 喝水", now=now)
    check(content == "喝水" and pw.at_ts == datetime(2026, 11, 15, 15, 0, tzinfo=TZ_CN).timestamp(),
          "下午3点+内容切分")
    for bad in ("下午14点", "早上15点", "晚上6点"):
        try:
            parse_when(bad, now=now)
            check(False, f"时段+越界小时应报错: {bad}")
        except ValueError:
            check(True, f"时段+越界小时报错: {bad}")

    print("\n[1e] 每月 / 每年")
    pw = parse_when("每月1号 09:00", now=now)
    check(pw.rec == "cron" and pw.cron == "0 9 1 * *", "每月1号 -> cron")
    pw = parse_when("每月15日 9点", now=now)
    check(pw.cron == "0 9 15 * *", "每月15日 9点")
    pw = parse_when("每月1、15号 09:00", now=now)
    check(pw.cron == "0 9 1,15 * *", "每月1、15号")
    pw = parse_when("每月1号9点", now=now)
    check(pw.cron == "0 9 1 * *", "每月1号9点（无空格）")
    pw = parse_when("每月最后一天 09:00", now=now)
    check(pw.cron == "0 9 L * *", "每月最后一天 -> L")
    pw = parse_when("每月末 09:00", now=now)
    check(pw.cron == "0 9 L * *", "每月末 -> L")
    pw = parse_when("每年11月20日 09:00", now=now)
    check(pw.cron == "0 9 20 11 *", "每年11月20日")
    pw = parse_when("每月 09:00", now=now)
    check(pw.cron == "0 9 1 * *", "裸每月 09:00 -> 每月1号")
    pw, content = _split_when("每月1号 09:00 交房租", now=now)
    check(content == "交房租" and pw.cron == "0 9 1 * *", "每月1号+内容切分")
    try:
        parse_when("每月1号 交房租", now=now)
        check(False, "每月1号缺时刻应报错")
    except ValueError as e:
        check("缺时刻" in str(e), "每月缺时刻报错含提示")
    for bad in ("每年 09:00", "每年2月30日 09:00", "每月32号 09:00"):
        try:
            parse_when(bad, now=now)
            check(False, f"非法应报错: {bad}")
        except ValueError:
            check(True, f"报错: {bad}")

    print("\n[1f] 间隔重复")
    pw = parse_when("每小时", now=now)
    check(pw.rec == "cron" and pw.cron == "0 * * * *", "每小时")
    pw, content = _split_when("每小时15分 报数", now=now)
    check(content == "报数" and pw.cron == "15 * * * *", "每小时15分+内容")
    pw = parse_when("每30分钟", now=now)
    check(pw.cron == "*/30 * * * *", "每30分钟")
    pw = parse_when("每半小时", now=now)
    check(pw.cron == "*/30 * * * *", "每半小时")
    pw = parse_when("每隔2小时", now=now)
    check(pw.cron == "0 */2 * * *", "每隔2小时")
    pw = parse_when("每两小时", now=now)
    check(pw.cron == "0 */2 * * *", "每两小时（中文数字）")
    pw = parse_when("每15分钟", now=now)
    check(pw.cron == "*/15 * * * *", "每15分钟")
    pw = parse_when("每分钟", now=now)
    check(pw.cron == "* * * * *", "每分钟")
    pw, content = _split_when("每3天 09:00 量体重", now=now)
    check(content == "量体重" and pw.cron == "0 9 */3 * *", "每3天 09:00+内容")
    pw = parse_when("每半年 09:00", now=now)
    check(pw.cron == "0 9 1 */6 *", "每半年")
    pw = parse_when("每季度 09:00", now=now)
    check(pw.cron == "0 9 1,4,7,10 *", "每季度")
    pw = parse_when("每2个月 9点", now=now)
    check(pw.cron == "0 9 1 */2 *", "每2个月")
    pw = parse_when("每半个月 9点", now=now)
    check(pw.cron == "0 9 1,15 * *", "每半个月 -> 每月1、15号")
    for bad in ("每45分钟", "每10秒", "每3天", "每2周", "每2年"):
        try:
            parse_when(bad, now=now)
            check(False, f"非法应报错: {bad}")
        except ValueError:
            check(True, f"报错: {bad}")

    print("\n[1g] 每周多天 / 周末 / 裸星期 / 下周")
    pw = parse_when("每周一三五 09:00", now=now)
    check(pw.rec == "weekly" and pw.cron == "0 9 * * 1,3,5", "每周一三五")
    pw = parse_when("每周一至周五 09:00", now=now)
    check(pw.cron == "0 9 * * 1,2,3,4,5", "每周一至周五")
    pw = parse_when("每周一、三 10:00", now=now)
    check(pw.cron == "0 10 * * 1,3", "每周一、三")
    pw = parse_when("每周六日 10:00", now=now)
    check(pw.cron == "0 10 * * 6,7", "每周六日")
    pw = parse_when("周末 09:00", now=now)
    check(pw.cron == "0 9 * * 0,6", "周末")
    pw = parse_when("每周末 10:00", now=now)
    check(pw.cron == "0 10 * * 0,6", "每周末")
    pw = parse_when("周三 09:00", now=now)  # 今天周日 -> 11-18
    check(pw.at_ts == datetime(2026, 11, 18, 9, 0, tzinfo=TZ_CN).timestamp(), "裸周三 -> 下个周三")
    pw = parse_when("周日 11:00", now=now)  # 今天周日10:00，未过 -> 今天
    check(pw.at_ts == datetime(2026, 11, 15, 11, 0, tzinfo=TZ_CN).timestamp(), "裸周日（今天未过）")
    pw = parse_when("星期五 18:00", now=now)
    check(pw.at_ts == datetime(2026, 11, 20, 18, 0, tzinfo=TZ_CN).timestamp(), "星期五 18:00")
    pw = parse_when("下周一 09:00", now=now)  # 今天周日 -> 11-16
    check(pw.at_ts == datetime(2026, 11, 16, 9, 0, tzinfo=TZ_CN).timestamp(), "下周一")
    pw = parse_when("下周五 9点", now=now)
    check(pw.at_ts == datetime(2026, 11, 20, 9, 0, tzinfo=TZ_CN).timestamp(), "下周五")
    pw = parse_when("下周日 09:00", now=now)
    check(pw.at_ts == datetime(2026, 11, 22, 9, 0, tzinfo=TZ_CN).timestamp(), "下周日")
    pw = parse_when("大后天 10:00", now=now)
    check(pw.at_ts == datetime(2026, 11, 18, 10, 0, tzinfo=TZ_CN).timestamp(), "大后天")
    pw = parse_when("下下周三 10:00", now=now)
    check(pw.at_ts == datetime(2026, 11, 25, 10, 0, tzinfo=TZ_CN).timestamp(), "下下周三")
    pw, content = _split_when("每周一三五 10:00 站会", now=now)
    check(content == "站会" and pw.cron == "0 10 * * 1,3,5", "每周一三五+内容切分")
    for bad in ("每周 09:00", "下周 09:00", "下周末 09:00"):
        try:
            parse_when(bad, now=now)
            check(False, f"非法应报错: {bad}")
        except ValueError:
            check(True, f"报错: {bad}")

    print("\n[2] cron 引擎")
    nxt = cron_next("0 9 * * *", now)
    check(nxt == datetime(2026, 11, 16, 9, 0, tzinfo=TZ_CN).timestamp(), "每天：跳过今天已过时刻")

    nxt = cron_next("*/15 * * * *", datetime(2026, 11, 15, 9, 7, tzinfo=TZ_CN).timestamp())
    check(nxt == datetime(2026, 11, 15, 9, 15, tzinfo=TZ_CN).timestamp(), "*/15 步长")

    nxt = cron_next("0 9 13 * 1", datetime(2026, 11, 17, 10, 0, tzinfo=TZ_CN).timestamp())
    # 日(13)与周(周一)同时受限 -> 「或」语义：13 日已过，取下一个周一
    check(datetime(2026, 11, 23).weekday() == 0, "前提：2026-11-23 是周一")
    check(nxt == datetime(2026, 11, 23, 9, 0, tzinfo=TZ_CN).timestamp(), "日/周或语义")

    nxt = cron_next("0 9 * * 7", datetime(2026, 11, 17, 10, 0, tzinfo=TZ_CN).timestamp())
    check(nxt == datetime(2026, 11, 22, 9, 0, tzinfo=TZ_CN).timestamp(), "周日 7 == 0")

    try:
        cron_next("0 2 30 2 *", now)
        check(False, "2月30日应报永无匹配")
    except ValueError:
        check(True, "2月30日报永无匹配")

    print("\n[2b] cron 引擎：L 与 cron_human")
    nxt = cron_next("0 9 L * *", datetime(2026, 11, 15, 10, 0, tzinfo=TZ_CN).timestamp())
    check(nxt == datetime(2026, 11, 30, 9, 0, tzinfo=TZ_CN).timestamp(), "L = 11月最后一天（30日）")
    nxt = cron_next("0 9 L 2 *", datetime(2026, 11, 15, 10, 0, tzinfo=TZ_CN).timestamp())
    check(nxt == datetime(2027, 2, 28, 9, 0, tzinfo=TZ_CN).timestamp(), "L 2月 = 28日（2027 平年）")
    nxt = cron_next("0 9 1,L * *", datetime(2026, 11, 15, 10, 0, tzinfo=TZ_CN).timestamp())
    check(nxt == datetime(2026, 11, 30, 9, 0, tzinfo=TZ_CN).timestamp(), "1,L 组合 -> 30日（月末）")

    check(cron_human("0 9 * * *") == "每天 09:00", "human 每天（回归）")
    check(cron_human("0 9 * * 1-5") == "工作日 09:00", "human 工作日（回归）")
    check(cron_human("0 9 * * 3") == "每周三 09:00", "human 每周三（回归）")
    check(cron_human("0 9 * * 1,3,5") == "每周一三五 09:00", "human 每周一三五")
    check(cron_human("0 9 * * 0,6") == "周末 09:00", "human 周末")
    check(cron_human("30 * * * *") == "每小时30分", "human 每小时30分")
    check(cron_human("0 */2 * * *") == "每2小时", "human 每2小时")
    check(cron_human("*/30 * * * *") == "每30分钟", "human 每30分钟")
    check(cron_human("* * * * *") == "每分钟", "human 每分钟")
    check(cron_human("0 9 1 * *") == "每月1号 09:00", "human 每月1号")
    check(cron_human("0 9 1,15 * *") == "每月1、15号 09:00", "human 每月1、15号")
    check(cron_human("0 9 L * *") == "每月最后一天 09:00", "human 每月最后一天")
    check(cron_human("0 9 20 11 *") == "每年11月20日 09:00", "human 每年")
    check(cron_human("0 9 */3 * *") == "每3天 09:00", "human 每3天")


# --------------------------------------------------------------------------
# [7] QQ 命令层（FakeDisp，不起服务器）
# --------------------------------------------------------------------------
class FakeDisp:
    def __init__(self) -> None:
        self.sent: list = []

    async def submit(self, n, wait=False, dedup=True):
        self.sent.append(n)
        return {"queued": True, "event_id": n.event_id, "targets": ["qq:only"]}


async def command_checks(check, tmp: Path) -> None:
    from types import SimpleNamespace

    from noti_gateway.plugins import PluginCtx
    from noti_gateway.plugins.reminders import _HELP, setup as _reminders_setup

    print("\n[7] 声明式指令总线：前缀门 / 自动帮助 / 被动回复")
    store = ReminderStore(str(tmp / "cmd.db"))
    disp = FakeDisp()
    plg = RemindersPlugin(SimpleNamespace(
        cfg=SimpleNamespace(reminders=RemindersCfg(source="remind"), auth=AuthCfg()),
        disp=disp, hub=None, db_path=str(tmp / "cmd.db"),
    ))
    ev = {"author": {"user_openid": "OPENID12345678"}, "id": "MSG-A1"}

    ctx = PluginCtx(app=None, cfg=None, db_path=str(tmp / "cmd.db"),
                    disp=disp, hub=None, client=None)
    seen_events: list[tuple] = []

    async def _listener(t: str, e: dict) -> None:
        seen_events.append((t, e))

    ctx.event_listeners.append(_listener)
    # 等价于 setup()：声明式注册三个指令
    ctx.command("提醒", handler=plg.cmd_remind, desc="创建定时提醒",
                hint="<时间> <内容>", usage=_HELP)
    ctx.command("列表", handler=plg.cmd_list, desc="查看进行中的提醒")
    ctx.command("取消", handler=plg.cmd_cancel, desc="取消提醒", hint="<id>")

    async def say(content: str, etype: str = "C2C_MESSAGE_CREATE",
                  msg_id: str | None = None) -> None:
        e = {"content": content, "author": ev["author"]}
        if msg_id:
            e["id"] = msg_id
        ctx.remember_msg(msg_id, ev["author"]["user_openid"])
        await ctx.dispatch_event(etype, e)

    await say("今天天气不错，出去走走")
    check(disp.sent == [] and store.list_items("active") == [], "无前缀消息不触发指令")
    check(len(seen_events) == 1, "无前缀消息仍广播给监听者（归档可见）")

    await say("#提醒 1分钟后 x", etype="GROUP_AT_MESSAGE_CREATE")
    check(store.list_items("active") == [], "非 C2C 事件类型不进指令总线")

    await say("#提醒 30分钟后 喝水", msg_id="MSG-A1")
    items = store.list_items("active")
    check(len(items) == 1 and items[0]["title"] == "喝水", "#前缀创建成功")
    check(items[0]["created_by"] == "qq:OPENID12", "created_by 记录 openid 前缀")
    check(len(disp.sent) == 1 and disp.sent[0].body and "已添加" in disp.sent[0].body,
          "回复由框架统一发送")
    check("设于" in disp.sent[0].body and "（周" in disp.sent[0].body
          and "下次" in disp.sent[0].body, "创建回复含编号/设于/下次（带星期几）")
    check(disp.sent[0].meta.get("passive", {}).get("msg_id") == "MSG-A1",
          "窗口内的回复自动携带被动 msg_id")
    check(disp.sent[0].title == "提醒", "回复 title 取指令名")

    await say("/提醒 列表", msg_id="MSG-A2")
    check(len(disp.sent) == 2 and disp.sent[1].body and "#1" in disp.sent[1].body,
          "/前缀 列表含条目")

    await say("＠提醒 取消 1", msg_id="MSG-A3")
    check(len(disp.sent) == 3 and disp.sent[2].body and "已取消" in disp.sent[2].body
          and store.list_items("active") == [], "全角＠前缀取消")

    await say("@列表")
    check(len(disp.sent) == 4 and "没有进行中的提醒" in disp.sent[3].body, "裸子命令 #列表")

    await say("@帮助")
    check(len(disp.sent) == 5 and disp.sent[4].body and "指令总览" in disp.sent[4].body,
          "#帮助 -> 框架自动生成的总览")
    check("#提醒" in disp.sent[4].body and "#状态" not in disp.sent[4].body,
          "总览按已注册指令生成")

    await say("#提醒 帮助", msg_id="MSG-A4")
    check(len(disp.sent) == 6 and "定时提醒" in disp.sent[5].body,
          "#提醒 帮助 -> 框架回 usage 详情")

    await say("#提醒", msg_id="MSG-A5")
    check(len(disp.sent) == 7 and "定时提醒" in disp.sent[6].body, "空参数 -> usage 详情")

    await say("#取消 99")
    check(len(disp.sent) == 8 and "没找到" in disp.sent[7].body, "取消不存在的 id")

    await say("#随便聊聊")
    check(len(disp.sent) == 8, "不认识的指令静默不误伤")

    await say("#提醒 18:00")
    check(len(disp.sent) == 9 and "内容不能为空" in disp.sent[8].body, "缺内容报错")

    r = await plg.cmd_remind("每周一 09:00 周会", ev)
    check(r and "#2" in r, "第二条创建 id 自增")

    # 未登记 msg_id 的回复不带被动参数
    await say("#列表", msg_id=None)
    last = disp.sent[-1]
    check(not last.meta.get("passive"), "未登记 msg_id -> 主动消息（不带 passive）")
    store.close()


async def session_and_scheduler_checks(check, tmp: Path) -> None:
    from types import SimpleNamespace

    from noti_gateway.plugins import Pending, PluginCtx

    print("\n[8] 多轮会话（got 式）")
    disp = FakeDisp()
    ctx = PluginCtx(app=None, cfg=None, db_path=str(tmp / "sess.db"),
                    disp=disp, hub=None, client=None)
    ev = {"author": {"user_openid": "U-SESS"}, "id": "MSG-S1"}
    openid = ev["author"]["user_openid"]
    seen: list = []

    async def note_handler(session, text: str, event: dict):
        seen.append(text)
        return f"已记下：{text}"

    # 用指令开启会话：#记 -> 提问；下一条消息（不带前缀）被会话接管
    async def cmd_note(args: str, event: dict) -> str:
        ctx.sessions.open(openid, note_handler, state={})
        return "进入记事模式，" + "要记什么内容？直接回复文本即可"

    ctx.add_command("记", cmd_note, desc="记一条笔记（多轮）")

    async def say(content: str) -> None:
        ctx.remember_msg(ev.get("id"), openid)
        await ctx.dispatch_event("C2C_MESSAGE_CREATE", {"content": content,
                                                        "author": ev["author"], "id": ev["id"]})

    await say("#记")
    check(ctx.sessions.has(openid), "指令开启会话")
    check(len(disp.sent) == 1 and "记事模式" in disp.sent[0].body, "会话开启回复")

    await say("今天买了杯咖啡")
    check(len(disp.sent) == 2 and "已记下：今天买了杯咖啡" in disp.sent[1].body,
          "会话接管无前缀消息")
    check(not ctx.sessions.has(openid), "会话完成自动关闭")

    await say("随便一条消息")
    check(len(disp.sent) == 2, "会话关闭后恢复正常前缀门")

    # 会话中「取消」退出
    async def endless(session, text, event):
        return Pending("继续？")

    ctx.sessions.open(openid, endless, state={})
    await say("算了")
    check(len(disp.sent) == 3 and "已退出" in disp.sent[2].body, "取消词退出会话")
    check(not ctx.sessions.has(openid), "退出后会话关闭")

    # 带前缀的消息也会话优先（NoneBot got 语义）
    ctx.sessions.open(openid, endless, state={})
    await say("#列表")
    check(len(disp.sent) == 4 and "继续？" in disp.sent[3].body, "会话优先于前缀指令")
    ctx.sessions.close(openid)

    print("\n[9] scheduler 服务")
    fired: list[str] = []
    ctx.schedule("快速任务", lambda: seen.append("tick") or _noop(), interval=0.2)
    await ctx.run_startup()
    await asyncio.sleep(0.7)
    await ctx.run_shutdown()
    check(len([x for x in seen if x == "tick"]) >= 2, "interval 任务按周期触发并随 shutdown 停止")

    try:
        ctx.schedule("坏任务", _noop, cron="* * * * *", interval=1)
        check(False, "cron/interval 二选一校验")
    except ValueError:
        check(True, "cron/interval 二选一校验")

    # 框架自持指令
    print("\n[10] 框架指令与元信息")
    ctx.plugin_metas.append({"name": "测试插件", "desc": "用于展示", "version": "9.9"})
    ctx.add_command("状态", _fake_status(ctx), desc="网关运行状态")
    ctx.add_command("插件", _fake_plugins(ctx), desc="已加载插件列表")
    r = await _call_cmd(ctx, "状态")
    check(r and "运行" in r, "#状态 返回运行摘要")
    r = await _call_cmd(ctx, "插件")
    check(r and "测试插件" in r, "#插件 列出元信息")
    check("#状态" in ctx.help_text() and "#记" in ctx.help_text(), "#帮助 总览含全部注册指令")

    print("\n[11] 通知中间件（agents）")
    from noti_gateway.models import Notification
    from noti_gateway.plugins.agents import AgentProcessor
    from noti_gateway.config import AgentsCfg

    cfg = AgentsCfg(sources=["zcode"], quiet_hours="23:00-07:30", quiet_levels=["info"],
                    templates={"zcode": "🛠 {{ title }}\n{{ body }}"})
    proc = AgentProcessor(cfg, hub=None)
    base = Notification(source="zcode", level="info", title="构建完成", body="耗时 3m")
    out = await proc.process(base)
    check(out is not None and out.title == "🛠 构建完成", "per-source 模板改写标题")
    check(out.body == "耗时 3m", "模板正文保留")

    other = Notification(source="ci", level="info", title="无关来源")
    check(await proc.process(other) is other, "不在 sources 内的原样通过")

    # 静默时段：把 quiet 窗口设成"全天"来测确定性
    cfg_all = AgentsCfg(sources=["zcode"], quiet_hours="00:00-23:59", quiet_levels=["info"])
    proc_all = AgentProcessor(cfg_all, hub=None)
    dropped = await proc_all.process(
        Notification(source="zcode", level="info", title="夜间通知"))
    check(dropped is None, "静默时段 info 级被丢弃")
    check(proc_all.stats["zcode"]["quiet_dropped"] == 1, "静默丢弃计数")

    warn_kept = await proc_all.process(
        Notification(source="zcode", level="warn", title="夜间告警"))
    check(warn_kept is not None, "静默时段只过滤配置的级别")

    errored = await proc_all.process(
        Notification(source="zcode", level="error", title="x"))
    if errored is None:
        check(False, "error 级不应被 info 静默过滤")
    else:
        check(True, "error 级不受 info 静默影响")


async def _noop() -> None:
    return None


async def _call_cmd(ctx, name: str) -> str | None:
    spec = next(s for s in ctx._commands if s.name == name)
    return await spec.handler("", {})


def _fake_status(ctx):
    async def h(args: str, event: dict) -> str:
        return f"运行 0h0m ｜ 收 0 发 0 ｜ 插件 {len(ctx.plugin_metas)}"
    return h


def _fake_plugins(ctx):
    async def h(args: str, event: dict) -> str:
        return "已加载插件：\n" + "\n".join(
            f"{m.get('name')} v{m.get('version')}" for m in ctx.plugin_metas)
    return h


# --------------------------------------------------------------------------
# HTTP 全链路
# --------------------------------------------------------------------------
async def e2e_checks(check, base: str, tmp: Path) -> None:
    print("\n[3] HTTP 创建 -> 触发 -> 投递")
    body, h = signed_json_headers({"title": "喝水提醒", "when": "2秒后"}, "/schedule", SECRET)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{base}/schedule", content=body, headers=h)
        check(r.status_code == 200, f"创建返回 200 (实际 {r.status_code})")
        j = r.json()
        check(j.get("ok") and isinstance(j.get("id"), int), f"返回 id (实际 {j})")
        check(j.get("rec") == "once" and j.get("next_human"), "返回规则与下次时间")

        ok = await wait_for(lambda: mock_state["sends"] >= 1, 8)
        check(ok, "2 秒后 mock QQ 收到投递")
        content = str(mock_state["last_body"].get("content", ""))
        check("喝水提醒" in content, "正文含标题")
        check("remind" in content, "来源标记 remind")
        check("提醒#1" in content and "设于" in content and "2秒后" in content,
              "投递正文含编号/规则/设于脚注")

        r = await c.get(f"{base}/schedule?state=done",
                        headers=sign_headers("GET", "/schedule", b"", SECRET))
        check(r.json().get("count") == 1, "触发后转 done")
        r = await c.get(f"{base}/schedule?state=active",
                        headers=sign_headers("GET", "/schedule", b"", SECRET))
        check(r.json().get("count") == 0, "active 清空")

        print("\n[4] 取消")
        body, h = signed_json_headers({"title": "临时提醒", "when": "60分钟后"}, "/schedule", SECRET)
        r = await c.post(f"{base}/schedule", content=body, headers=h)
        rid = r.json()["id"]
        r = await c.delete(f"{base}/schedule/{rid}",
                           headers=sign_headers("DELETE", f"/schedule/{rid}", b"", SECRET))
        check(r.status_code == 200 and r.json().get("ok"), f"取消成功 (实际 {r.status_code})")
        r = await c.delete(f"{base}/schedule/{rid}",
                           headers=sign_headers("DELETE", f"/schedule/{rid}", b"", SECRET))
        check(r.status_code == 404, "重复取消 -> 404")
        sends_before = mock_state["sends"]
        await asyncio.sleep(1.5)
        check(mock_state["sends"] == sends_before, "已取消的不再触发")

        print("\n[5] 鉴权与参数校验")
        r = await c.post(f"{base}/schedule", json={"title": "x", "when": "1分钟后"})
        check(r.status_code == 401, "无签名 -> 401")
        body, h = signed_json_headers({"title": "x", "when": "随便什么"}, "/schedule", SECRET)
        r = await c.post(f"{base}/schedule", content=body, headers=h)
        check(r.status_code == 400, "非法 when -> 400")
        body, h = signed_json_headers({"when": "1分钟后"}, "/schedule", SECRET)
        r = await c.post(f"{base}/schedule", content=body, headers=h)
        check(r.status_code == 400, "缺 title -> 400")
        body, h = signed_json_headers({"title": "x", "cron": "*/5 * * * *"}, "/schedule", SECRET)
        r = await c.post(f"{base}/schedule", content=body, headers=h)
        check(r.status_code == 200 and r.json()["rec"] == "cron", "结构化 cron 字段可用")
        await c.delete(f"{base}/schedule/{r.json()['id']}",
                       headers=sign_headers("DELETE", f"/schedule/{r.json()['id']}", b"", SECRET))

        print("\n[6] 错过补发与跳过")
        store = ReminderStore(str(Path(os.environ["NOTI_DB"])))
        past = time.time() - 300  # grace=30s，明显过期

        async def _force_past(rid: int) -> None:
            await asyncio.to_thread(
                lambda: (store._db.execute("UPDATE reminders SET next_at = ? WHERE id = ?", (past, rid)),
                         store._db.commit())
            )

        row = await asyncio.to_thread(store.create, ParsedWhen("once", past, None, "过期"), "迟到的提醒", "", "test")
        row2 = await asyncio.to_thread(store.create, ParsedWhen("cron", None, "* * * * *", "每分钟"), "每分钟的提醒", "", "test")
        await _force_past(row2["id"])  # create 按当下算的是未来时刻，人为拨回过去模拟停机跨过触发点
        ok = await wait_for(lambda: _missed_count(store) >= 1, 4)
        check(ok, "过期一次性提醒转 missed")
        r = await c.get(f"{base}/schedule?state=active",
                        headers=sign_headers("GET", "/schedule", b"", SECRET))
        items = r.json()["items"]
        check(len(items) == 1 and items[0]["rec"] == "cron", "重复提醒保持 active")
        check(items[0]["next_at"] > time.time(), "重复提醒 next_at 已推进到未来")
        check(store.get(row2["id"])["next_at"] > time.time(), "库内 next_at 已推进")
        store.close()


def _missed_count(store: ReminderStore) -> int:
    return len(store.list_items("missed"))


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="noti-remind-"))
    keys = tmp / "keys"
    keys.write_text(f"t1 {SECRET}\n", encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(port=PORT, qq_port=QQ_PORT, keys_file=keys.as_posix()),
        encoding="utf-8",
    )
    os.environ["NOTI_DB"] = str(tmp / "remind.db")

    failures: list[str] = []

    def check(cond: bool, name: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    unit_checks(check)
    await command_checks(check, tmp)
    await session_and_scheduler_checks(check, tmp)

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))
    servers = [
        uvicorn.Server(uvicorn.Config(build_mock(), host="127.0.0.1", port=QQ_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")),
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    base = f"http://127.0.0.1:{PORT}"
    try:
        if not await wait_up(f"{base}/_health"):
            print("FAIL: 网关未启动")
            return 1
        await e2e_checks(check, base, tmp)
    finally:
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)

    print("\n" + "=" * 54)
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
