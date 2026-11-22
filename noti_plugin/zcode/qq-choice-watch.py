"""双端并行选择题监视器（分离式后台进程，由 qq-notify.sh choice 分支拉起）。

设计：AskUserQuestion 钩子立即放行（本地 UI 照常出题），本进程同时把按钮卡片发到 QQ：
  - QQ 先答（按钮回调 / 文本回复）→ 把答案经 continue-input.ps1 注入 ZCode 输入框发送，
    驱动下一轮按该答案继续；退出
  - 本地先动（cli 日志出现本会话 turn.started / v4 prompt admitted）→ 视为已在本地处理，
    静默退出，QQ 卡片自然作废
  - 30 分钟 TTL 后退出
日志: ~/.zcode/log/qq-choice-watch-<日期>.log
"""
import datetime
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import threading
import time

import websocket

HOOKS = os.path.dirname(os.path.abspath(__file__))
ZDIR = os.path.join(os.path.expanduser("~"), ".zcode")
LOG_DIR = os.path.join(ZDIR, "log")
CLI_LOG_DIR = os.path.join(ZDIR, "cli", "log")
TTL_S = 30 * 60
POLL_S = 1.0

_spec = importlib.util.spec_from_file_location("qao", os.path.join(HOOKS, "qq-ask-once.py"))
qao = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qao)

STOP = threading.Event()


def log(msg: str) -> None:
    now = datetime.datetime.now()
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"qq-choice-watch-{now:%Y-%m-%d}.log"), "a", encoding="utf-8") as f:
        f.write(f"[{now:%Y-%m-%d %H:%M:%S}] {msg}\n")


def today_file() -> str:
    d = datetime.date.today()
    return os.path.join(CLI_LOG_DIR, f"zcode-{d:%Y-%m-%d}.jsonl")


class LocalActivityWatch:
    """尾随 cli 日志，侦测本会话的本地新动态（用户已在本地处理/输入）。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.fd = None
        self.pos = 0
        try:
            self.fd = open(today_file(), encoding="utf-8", errors="replace")
            self.pos = self.fd.seek(0, 2)  # 从当前文件尾开始
        except OSError:
            self.fd = None

    def poll(self) -> bool:
        if self.fd is None:
            try:
                self.fd = open(today_file(), encoding="utf-8", errors="replace")
                self.pos = self.fd.seek(0, 2)
            except OSError:
                return False
        got = False
        for line in self.fd:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("sessionId") != self.session_id:
                continue
            if rec.get("event") == "turn.started" or rec.get("message") == "v4 prompt admitted":
                got = True
        return got


def inject_answer(text: str, rid: str) -> bool:
    """把答案写入 UTF-8 文件后经 continue-input.ps1 注入 ZCode（中文走命令行会乱码）。"""
    answer_file = os.path.join(ZDIR, f".qq-choice-inject-{rid}.txt")
    with open(answer_file, "w", encoding="utf-8") as f:
        f.write("（QQ远程代答，请依据此回答继续后续工作，不要再次询问）" + text)
    ps1 = os.path.join(ZDIR, "hooks", "continue-input.ps1")  # 注意在 hooks 子目录
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
             "-File", ps1, "-TextFile", answer_file],
            capture_output=True, timeout=30)
        ok = r.returncode == 0 and b"TYPED" in r.stdout
        log(f"rid={rid} 注入答案{'成功' if ok else f'失败 exit={r.returncode} out={r.stdout[:60]!r}'}")
        return ok
    except Exception as e:
        log(f"rid={rid} 注入异常: {e!r}")
        return False
    finally:
        try:
            os.remove(answer_file)
        except OSError:
            pass


def main(session_id: str, rid: str, plan_path: str) -> int:
    questions = qao.load_questions(plan_path)
    if not questions:
        log(f"rid={rid} 计划无题目，退出")
        return 1
    token = qao.get_token()
    gw = json.loads(qao.urllib.request.urlopen(
        qao.urllib.request.Request(qao.GATEWAY_URL, headers={"Authorization": f"QQBot {token}"}),
        timeout=10).read().decode())["url"]
    ws = websocket.create_connection(gw, timeout=15)
    hello = json.loads(ws.recv())
    interval = (hello.get("d") or {}).get("heartbeat_interval", 40000) / 1000
    ws.send(json.dumps({"op": 2, "d": {
        "token": f"QQBot {token}", "intents": qao.INTENTS, "shard": [0, 1],
        "properties": {"$os": "windows"}}}))
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        ws.settimeout(max(1, min(10, deadline - time.time())))
        try:
            m = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if m.get("op") == 9:
            log(f"rid={rid} 会话无效（op9），退出")
            return 1
        if m.get("op") == 0 and m.get("t") == "READY":
            ready = True
            break
    if not ready:
        log(f"rid={rid} 等 READY 超时，退出（本地 UI 不受影响）")
        return 1

    if not qao.build_question_cards(token, rid, questions):
        log(f"rid={rid} 卡片未发出（不降级纯文本），退出（本地 UI 不受影响）")
        return 1

    interval_lock = threading.Lock()

    def heartbeat() -> None:
        while not STOP.is_set():
            time.sleep(interval)
            try:
                with interval_lock:
                    ws.send(json.dumps({"op": 1, "d": None}))
            except Exception:
                return

    threading.Thread(target=heartbeat, daemon=True).start()
    log(f"rid={rid} 双端监视开始（本地活动侦测 session={session_id[:20]}…，TTL {TTL_S // 60} 分钟）")

    local = LocalActivityWatch(session_id)
    collector = qao.ChoiceCollector(questions)
    answer = None
    deadline = time.time() + TTL_S
    while time.time() < deadline:
        if local.poll():  # 本地先动：已在电脑前处理，QQ 卡片作废
            log(f"rid={rid} 检测到本地新活动（turn.started/prompt admitted），放弃远程代答")
            return 0
        ws.settimeout(min(2.0, max(0.5, deadline - time.time())))
        try:
            m = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            log(f"rid={rid} WS 异常: {e!r}（本地 UI 不受影响）")
            return 1
        op, t, d = m.get("op"), m.get("t"), m.get("d") or {}
        if op == 9 or op == 7:
            log(f"rid={rid} WS 要求重连（op{op}），短周期进程退出")
            return 1
        if op == 0 and t == "INTERACTION_CREATE":
            res = qao.parse_interaction(rid, d)
            iid = d.get("id") or qao.find_key(d, "interaction_id")
            if iid:
                try:
                    qao.ack_interaction(token, iid)
                except Exception as e:
                    log(f"rid={rid} 回应交互失败: {e!r}")
            if isinstance(res, tuple):
                collector.add_button(res[0], res[1])
                log(f"rid={rid} 已收 {len(collector.answers)}/{len(questions)} 题按钮答案")
                if collector.done():
                    answer = collector.render()
                    log(f"rid={rid} QQ 按钮完成全部作答 -> {answer[:60]}")
                    break
        elif op == 0 and t == "C2C_MESSAGE_CREATE":
            content = str(d.get("content") or "").strip()
            if content:
                collector.add_text(content)
                if collector.done():  # 全部题目答完，或用户以自由文本主动定稿
                    answer = collector.render()
                    log(f"rid={rid} QQ 文本回复完成作答 -> {answer[:60]}")
                    break
                log(f"rid={rid} 文本已记 {len(collector.answers)}/{len(questions)} 题，继续等待")
    def feedback(text: str) -> None:
        """在 QQ 里反馈提交结果（普通通知，非卡片）。"""
        qao.send_card(token, {"msg_type": 0, "content": text,
                              "msg_seq": random.randint(1, 10**6)})

    if answer:
        ok = inject_answer(answer, rid)
        feedback(("✅ 已收到你的回答，已提交给 ZCode：" if ok else
                  "⚠️ 回答注入 ZCode 失败（窗口不可用？），请回电脑手动处理：") + answer[:80])
        qao.ack_drain(ws, 45, token, rid)  # 宽限期 ack 迟到点击，防 QQ 失败提示
        return 0 if ok else 1
    if collector.answers or collector.free_text is not None:
        answer = collector.render() + "（部分回答，超时）"
        log(f"rid={rid} TTL 到期，按部分回答注入")
        ok = inject_answer(answer, rid)
        feedback(("✅ 超时，已按部分回答提交给 ZCode：" if ok else
                  "⚠️ 部分回答注入 ZCode 失败，请回电脑手动处理：") + answer[:80])
        qao.ack_drain(ws, 45, token, rid)
        return 0 if ok else 1
    log(f"rid={rid} TTL 到期无人代答，退出")
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 4:
            code = main(sys.argv[1], sys.argv[2], sys.argv[3])
        else:
            code = 1
            log(f"参数无效 argv={sys.argv}")
    except Exception as e:
        log(f"进程异常退出: {e!r}")
        code = 1
    STOP.set()
    sys.exit(code)
