"""多机器人 / 中文备注 / 旧式兼容 配置解析自测。

运行：python tests/test_config_bots.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from noti_gateway.config import load_config  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

failures: list[str] = []


def check(cond: bool, name: str) -> None:
    print(("  ok " if cond else " FAIL ") + name)
    if not cond:
        failures.append(name)


def with_config(body: str) -> object:
    fd, p = tempfile.mkstemp(suffix=".yaml")
    try:
        os.write(fd, body.encode("utf-8"))
        os.close(fd)
        return load_config(p)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


MULTI = """
auth: {keys_file: k}
bots:
  - id: main
    remark: "主力机器人"
    app_id: A1
    client_secret: S1
    targets:
      - {id: qq:me, remark: "手机单聊", user_openid: u1}
      - {id: qq:grp, remark: "测试群", group_openid: g1, rate_per_minute: 25}
  - id: backup
    remark: "备用机器人"
    app_id: A2
    client_secret: S2
    targets:
      - {id: qq:bak, user_openid: u2}
targets:
  - {id: hook:extra, type: webhook, url: "http://x", remark: "外挂"}
routes:
  - {source: ["*"], targets: [qq:me, qq:bak]}
chatdownload: {enabled: true, bot: backup}
"""

OLD = """
auth: {keys_file: k}
qq: {app_id: A, client_secret: B}
targets:
  - {id: qq:only, type: qq, group_openid: g1}
routes:
  - {source: ["*"], targets: [qq:only]}
"""


def main() -> None:
    cfg = with_config(MULTI)
    check([b.id for b in cfg.bots] == ["main", "backup"], "bots 解析（顺序保留）")
    check(cfg.bots[0].remark == "主力机器人", "bot 中文备注")
    check(len(cfg.targets) == 4, "嵌套目标平铺进 targets (实际 %d)" % len(cfg.targets))
    t = cfg.target("qq:me")
    check(t is not None and t.bot == "main", "目标自动归属 bot")
    check(t is not None and t.remark == "手机单聊", "目标中文备注")
    check(cfg.qq_for(t).app_id == "A1", "qq_for 用所属 bot 凭据")
    check(cfg.qq_for(cfg.target("qq:bak")).app_id == "A2", "第二台 bot 凭据独立")
    check(cfg.qq_for(cfg.target("hook:extra")).app_id == "", "非 bot 目标回落全局 qq 段")
    bid, qq = cfg.chatdownload_bot()
    check(bid == "backup" and qq.app_id == "A2", "chatdownload 显式绑定 bot (实际 %s)" % bid)

    cfg2 = with_config(MULTI.replace("chatdownload: {enabled: true, bot: backup}",
                                     "chatdownload: {enabled: true}"))
    bid2, qq2 = cfg2.chatdownload_bot()
    check(bid2 == "main" and qq2.app_id == "A1", "chatdownload 留空取第一台 bot (实际 %s)" % bid2)

    c3 = with_config(OLD)
    check(c3.bots == [], "旧式配置 bots 为空")
    check(c3.qq_for(c3.targets[0]).app_id == "A", "旧式 qq 段回落")
    bid3, qq3 = c3.chatdownload_bot()
    check(bid3 == "" and qq3.app_id == "A", "旧式 chatdownload 回落 qq 段")

    # 目标缺 type 时 bot 名下默认 qq
    c4 = with_config("""
auth: {keys_file: k}
bots:
  - id: b1
    app_id: A
    targets:
      - {id: q1, user_openid: u}
routes:
  - {source: ["*"], targets: [q1]}
""")
    check(c4.target("q1").type == "qq", "bot 名下目标缺省 type=qq")

    bad = [
        ("bot 缺 id", """
auth: {keys_file: k}
bots:
  - {app_id: A}
routes: []
""", "bot 缺少 id"),
        ("目标 id 重复", """
auth: {keys_file: k}
bots:
  - id: b1
    targets: [{id: dup}]
targets: [{id: dup, type: qq}]
routes:
  - {source: ["*"], targets: [dup]}
""", "target id 重复"),
        ("bot 引用不存在", """
auth: {keys_file: k}
targets: [{id: x, type: qq, bot: nope}]
routes:
  - {source: ["*"], targets: [x]}
""", "引用了不存在的 bot"),
        ("chatdownload.bot 引用不存在", """
auth: {keys_file: k}
targets: [{id: x, type: qq}]
routes:
  - {source: ["*"], targets: [x]}
chatdownload: {enabled: true, bot: ghost}
""", "引用了不存在的 bot"),
    ]
    for name, body, frag in bad:
        try:
            with_config(body)
            check(False, name)
        except SystemExit as e:
            check(frag in str(e), name + " (%s)" % e)

    # 仓库真实 config.yaml 冒烟：能加载且结构符合预期
    live = load_config(str(REPO / "config.yaml"))
    check(all(b.id for b in live.bots) and all(t.id for t in live.targets),
          "config.yaml 冒烟加载")

    print("ALL TESTS PASSED" if not failures else "FAILURES: %s" % failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
