"""一次性 QQ 按钮监听器（按需拉起，随钩子进程退出，不常驻、无需开机启动）。

接入协议/端点/HMAC 签名速记：同目录《接入文档.md》（noti-gateway 仓库根同名文件）。

由 qq-notify.sh 的 ask 分支在权限请求时临时启动，取代常驻的 qq-bot-listener.py：
    python qq-ask-once.py <rid> <等待秒数>
    python qq-ask-once.py selftest    # 只验证 SSE 订阅就绪，不发卡片

流程（零 QQ 直连——事件经 noti-gateway SSE 订阅，发送经网关 raw_qq）：
  1. 订阅网关 /stream?types=qq_event（HMAC v1 签名），连接建立即写
     ~/.zcode/.qq-ws-ready-<rid>；shell 看到就绪文件才发卡片，保证点击
     不会落在"无连接窗口"里丢失（网关 qqws 插件保持 QQ WS 在线）
  2. 收到 INTERACTION_CREATE 且 button_data 为 allow:<rid>/deny:<rid>（权限确认）
     或 ans:<rid>:<base64url选项>（选择题选项）时，写 ~/.zcode/.qq-answer-<rid>；
     交互回应由网关自动完成，QQ 客户端不转圈；
     收到 C2C_MESSAGE_CREATE 文本回复时写 ~/.zcode/.qq-msg-<rid>（自定义回答/多选）
  3. 超时或连接异常时退出 code 1，shell 回退本地确认框
日志: ~/.zcode/log/qq-ask-once-<日期>.log
依赖: pip install httpx
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time

import httpx

GATEWAY_URL = "http://<网关地址>"        # 阿里云 noti-gateway（事件订阅 + raw_qq 发送）
KEY_ID = "zcode"
GATEWAY_SECRET = "<网关 keys.conf 里 zcode 行的 HMAC secret>"

ZDIR = os.path.join(os.path.expanduser("~"), ".zcode")
LOG_DIR = os.path.join(ZDIR, "log")
RID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def log(msg: str) -> None:
    now = datetime.datetime.now()
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"qq-ask-once-{now:%Y-%m-%d}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{now:%Y-%m-%d %H:%M:%S}] {msg}\n")


def find_key(obj, key):
    """递归查找 JSON 字段（交互事件 payload 结构随场景变化，防御性解析）。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def sig_headers(method: str, path: str) -> dict:
    """HMAC v1 签名（canonical: METHOD + chr(10) + PATH + ... 空体）。"""
    ts = str(int(time.time()))
    nonce = str(random.randint(1, 10 ** 12))
    canonical = chr(10).join([method, path, ts, nonce, hashlib.sha256(b"").hexdigest()])
    sig = hmac.new(GATEWAY_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"X-Noti-Signature": "v1=" + sig, "X-Noti-Timestamp": ts,
            "X-Noti-Nonce": nonce, "X-Noti-Key-Id": KEY_ID}


def gw_raw_send(payload: dict) -> bool:
    """经网关 raw_qq 透传发送 QQ 载荷（卡片 / 确认文本），wait=1 拿真实回执。"""
    body = ('{"raw_qq":' + json.dumps(payload, ensure_ascii=False) + "}").encode("utf-8")
    ts = str(int(time.time()))
    nonce = str(random.randint(1, 10 ** 12))
    canonical = chr(10).join(["POST", "/n/zcode", ts, nonce, hashlib.sha256(body).hexdigest()]).encode("utf-8")
    sig = hmac.new(GATEWAY_SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    try:
        r = httpx.post(GATEWAY_URL + "/n/zcode?wait=1", content=body, timeout=30, trust_env=False,
                       headers={"Content-Type": "application/json; charset=utf-8",
                                "X-Noti-Signature": "v1=" + sig, "X-Noti-Timestamp": ts,
                                "X-Noti-Nonce": nonce, "X-Noti-Key-Id": KEY_ID})
        resp = r.json()
        res = (resp.get("results") or {}).get("qq:me") or {}
        if not (resp.get("ok") and res.get("ok")):
            log(f"网关投递失败: {json.dumps(resp, ensure_ascii=False)[:200]}")
        return bool(resp.get("ok")) and bool(res.get("ok"))
    except Exception as e:
        log(f"网关发送异常: {e!r}")
        return False
def send_card(payload: dict) -> bool:
    ok = gw_raw_send(payload)
    if not ok:
        log(f"卡片发送失败: {json.dumps(payload, ensure_ascii=False)[:160]}")
    return ok

def _btn(btn_id: str, label: str, data: str, style: int) -> dict:
    return {"id": btn_id, "render_data": {"label": label, "visited_label": "已选择"},
            "action": {"type": 1, "data": data, "permission": {"type": 2}}}


def build_question_cards(rid: str, questions: list) -> bool:
    """多题分别发卡（每题一张，避免单卡选项过多）：全部发送成功返回 True。
    每张卡：单选=回调按钮 ans:<rid>:<题下标0based>:<b64> 直接定稿该题；
    多选=回调按钮「mul:rid:题下标:b64」，点击即累加该选项；单题按钮行超键盘上限时该卡仅文本引导。
    选项序号用 ①②③ 书写——避免 QQ markdown 把有序列表重新连号。
    点齐全部题目（或任意一条文本回复 Q1=1 Q2=2）即终结。"""
    marks = "①②③④⑤⑥"
    now = datetime.datetime.now().strftime("%H:%M:%S")
    msg_seq = random.randint(1, 10**6)
    n = len(questions)
    ok_all = True
    for qi, q in enumerate(questions, 1):
        lines = [f"❓ **Zcode 提问（选择题 {qi}/{n}）**  🕐 {now}", "",
                 f"**【Q{qi}·{q['ms']}】**{q['text']}"]
        for j, lb in enumerate(q["labels"], 1):
            lines.append(f"{marks[j - 1:j] or '?'} {lb}")
        rows = []
        if q["labels"] and -(-len(q["labels"]) // 3) + 1 <= 5:  # 按钮行数(含自定义行)不超键盘上限
            bid = 1
            for c in range(0, len(q["labels"]), 3):
                row = []
                for k, lb in zip(range(c + 1, c + 4), q["labels"][c:c + 3]):
                    if q["ms"].startswith("多"):  # 多选：回调按钮，点击即累加该选项（网关自动回应）
                        row.append(_btn(str(bid), f"{k} {lb[:8]}",
                                        "mul:" + rid + f":{qi - 1}:" + base64.urlsafe_b64encode(lb.encode("utf-8")).decode().rstrip("="),
                                        1))
                    else:  # 单选：回调按钮直接定稿该题
                        row.append(_btn(str(bid), f"{k} {lb[:8]}",
                                        "ans:" + rid + f":{qi - 1}:" + base64.urlsafe_b64encode(lb.encode("utf-8")).decode().rstrip("="),
                                        1))
                    bid += 1
                rows.append({"buttons": row})
            rows.append({"buttons": [{"id": "9", "render_data": {"label": "✍️ 自定义回答", "visited_label": "请输入"},
                                      "action": {"type": 2, "data": f"回答: Q{qi}=", "permission": {"type": 2}}}]})
            if n > 1:
                lines.append(f"👉 第 {qi}/{n} 题：点选项作答，或回复文本 Q{qi}=内容（全部题目答完自动提交）")
            elif q["ms"].startswith("多"):
                lines.append("👉 多选题：点选项即记录（可多点多项），或回复文本 Q2=内容 一并定稿")
            else:
                lines.append("👉 点选项作答；也可回复文本直接作答（内容原样采用，数字不转选项）")
        else:
            # 开放题（无选项）或选项过多：只给「✍️ 自定义回答」预填按钮
            tip = "选项过多按钮放不下，" if q["labels"] else ""
            lines.append(f"👉 {tip}请回复文本 Q{qi}=自定义内容，或点下方按钮预填后发送")
            rows.append({"buttons": [{"id": "9",
                                      "render_data": {"label": "✍️ 自定义回答", "visited_label": "请输入"},
                                      "action": {"type": 2, "data": f"回答: Q{qi}=", "permission": {"type": 2}}}]})
        content = "\n".join(lines)
        payload = {"msg_type": 2, "markdown": {"content": content}, "msg_seq": msg_seq + qi}
        if rows:
            payload["keyboard"] = {"content": {"rows": rows}}
        if send_card(payload):
            log(f"rid={rid} 第 {qi}/{n} 题卡片已发送（按钮 {len(rows)} 行）")
        else:
            ok_all = False
            log(f"rid={rid} 第 {qi}/{n} 题卡片发送失败")
        if qi < n:
            time.sleep(0.5)  # 连发限速，防 QQ 限流
    return ok_all


class ChoiceCollector:
    """多题答案收集器：单选题靠按钮回调逐题累积，多选/开放题靠文本回复；
    全部就绪（或文本终结）即 finalize。超时可输出部分答案（未答标'未答'）。"""

    def __init__(self, questions: list):
        self.questions = questions
        self.answers = {}       # 题号(1-based) -> 答案文本
        self.free_text = None   # 未解析成 Q=x 格式的自由文本

    def add_button(self, qi0: int, label: str) -> None:
        qi = qi0 + 1
        if self.questions[qi0]["ms"].startswith("多"):
            # 多选：同一题累加选项，去重
            cur = (self.answers.get(qi) or "")
            parts = [x for x in cur.replace("，", ",").split(",") if x] if cur else []
            if label not in parts:
                parts.append(label)
            self.answers[qi] = ",".join(parts)
        else:
            self.answers[qi] = label

    def add_text(self, text: str) -> None:
        """文本回复=完全自定义答案（不做"数字→选项"映射，选项只走按钮回调）：
        按 Q<题号>= 切段逐题取原文；无 Q= 结构时整体视为自由文本。"""
        text = text.strip()
        if not text:
            return
        if text.startswith("回答:"):
            text = text[len("回答:"):].strip()
        consumed = False
        segs = re.split(r"[Qq](\d+)\s*[=：:]\s*", text)
        # segs = [前缀, 题号1, 值1, 题号2, 值2, ...]
        for qi_s, val in zip(segs[1::2], segs[2::2]):
            if not qi_s.isdigit():
                continue
            qi = int(qi_s)
            if not 1 <= qi <= len(self.questions):
                continue
            val = val.strip().strip("，,")
            if not val:
                continue
            self.answers[qi] = val  # 字面内容即答案（完全自定义）
            consumed = True
        leftover = segs[0].strip() if segs else ""
        if consumed:
            if leftover:
                self.free_text = leftover  # 段前缀残留的自由说明一并保留
        else:
            self.free_text = text

    def done(self) -> bool:
        """全部题目都有答案（按钮或 Q=文本）→ 提交；纯自由文本=用户主动定稿 → 提交。"""
        return self.free_text is not None or len(self.answers) >= len(self.questions)

    def render(self) -> str:
        if self.free_text is not None:
            if self.answers:
                extra = "；补充: " + self.free_text
                return "；".join(f"Q{i}: {self.answers[i]}" for i in sorted(self.answers)) + extra
            return self.free_text
        return "；".join(
            f"Q{i}: {self.answers.get(i, '未答')}" for i in range(1, len(self.questions) + 1))


def parse_interaction(rid: str, d: dict):
    """解析按钮回调。返回 ('allow'|'deny')、(题下标0based, 选项文本) 或 None；不落盘。"""
    button_data = str(find_key(d, "button_data") or "")
    if ":" not in button_data:
        return None
    head, rest = button_data.split(":", 1)
    if head in ("allow", "deny"):
        if rest == rid and RID_RE.match(rid):
            return head
        return None
    if head not in ("ans", "mul"):
        return None
    parts = rest.split(":")
    if not parts or parts[0] != rid:
        return None
    if len(parts) == 2:  # 旧格式 ans:<rid>:<b64>（单题）
        qi0, b64 = 0, parts[1]
    elif len(parts) == 3:  # 新格式 ans:<rid>:<题下标>:<b64>
        qi0 = int(parts[1]) if parts[1].isdigit() else 0
        b64 = parts[2]
    else:
        return None
    try:
        label = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
    except Exception as e:
        log(f"rid={rid} 按钮数据解码失败: {e!r}")
        return None
    return (qi0, label)


def assemble_text_answer(text: str, questions: list) -> str:
    """（已废弃，保留兼容）文本回复原样返回——数字不再映射为选项。"""
    text = text.strip()
    return text[len("回答:"):].strip() if text.startswith("回答:") else text


def handle_interaction(token: str, rid: str, d: dict) -> None:
    """ask 权限模式专用：allow/deny 写应答文件；ans 按钮写选项文本（旧单题格式）。"""
    res = parse_interaction(rid, d)
    if res is None:
        pass
    elif res in ("allow", "deny"):
        _write_answer(rid, res)
    else:
        _write_answer(rid, res[1])
def _write_answer(rid: str, value: str) -> None:
    with open(os.path.join(ZDIR, f".qq-answer-{rid}"), "w", encoding="utf-8") as f:
        f.write(value)
    log(f"rid={rid} 回传 -> {value[:60]}")


def handle_message(rid: str, d: dict) -> None:
    """C2C 文本回复：写入 .qq-msg-<rid>（选择题的自定义回答/多选回复）。"""
    content = str(d.get("content") or "").strip()
    if content:
        with open(os.path.join(ZDIR, f".qq-msg-{rid}"), "w", encoding="utf-8") as f:
            f.write(content)
        log(f"rid={rid} 文本回复 -> {content[:60]}")


def load_questions(plan_path: str) -> list:
    """解析 choice 计划文件：首行 CHOICE/AWAY，SID 行跳过，其后 Q<TAB>序号<TAB>单选|多选<TAB>题干<TAB>选项..."""
    questions = []
    with open(plan_path, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    if not lines or lines[0] not in ("CHOICE", "AWAY"):
        return []
    for ln in lines[1:]:
        p = ln.split("\t")
        if p and p[0] == "Q" and len(p) >= 4:
            try:
                questions.append({"idx": int(p[1]), "ms": p[2], "text": p[3], "labels": p[4:]})
            except ValueError:
                pass
    return questions


def main(rid: str, wait_seconds: float, selftest: bool, plan: str = None) -> int:
    deadline = time.time() + wait_seconds
    log(f"rid={rid} 连接网关 SSE stream（{GATEWAY_URL}）")

    try:
        cm = httpx.stream("GET", GATEWAY_URL + "/stream?types=qq_event",
                          headers=sig_headers("GET", "/stream"), trust_env=False,
                          timeout=httpx.Timeout(10, read=25))
    except Exception as e:
        log(f"rid={rid} SSE 连接异常: {e!r}")
        return 1

    with cm as r:
        if r.status_code != 200:
            log(f"rid={rid} SSE HTTP {r.status_code}")
            return 1
        it = r.iter_lines()
        # 连接就绪（SSE 首条注释行）→ 写就绪文件 / 发选择题卡片
        connected = False
        for line in it:
            if line.startswith(": connected"):
                connected = True
                break
            if time.time() > deadline:
                break
        if not connected:
            log(f"rid={rid} 等待 SSE 就绪超时")
            return 1
        log(f"rid={rid} SSE 就绪")
        if selftest:
            time.sleep(2)
            return 0
        questions, collector = [], None
        if plan:
            questions = load_questions(plan)
            if not questions or not build_question_cards(rid, questions):
                log(f"rid={rid} 选择题卡片未发出，放弃代答")
                return 1
            collector = ChoiceCollector(questions)
        else:
            with open(os.path.join(ZDIR, f".qq-ws-ready-{rid}"), "w", encoding="utf-8") as f:
                f.write(str(int(time.time() * 1000)))

        # 主循环：等按钮点击或文本回复（事件经网关 SSE 转发）
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                if collector is not None and (collector.answers or collector.free_text is not None):
                    final = collector.render() + "（部分回答，超时）"
                    _write_answer(rid, final)
                    send_card({"msg_type": 0, "content": f"⚠️ 超时，已按部分回答提交给 ZCode：{final[:80]}",
                               "msg_seq": random.randint(1, 10**6)})
                    log(f"rid={rid} 超时，按已收集的部分回答终结")
                    return 0
                log(f"rid={rid} 等待点击超时")
                return 1
            try:
                line = next(it)
            except StopIteration:
                log(f"rid={rid} SSE 流结束")
                return 1
            except Exception as e:
                log(f"rid={rid} SSE 异常: {e!r}")
                return 1
            if not line.startswith("data: "):
                continue
            try:
                m = json.loads(line[6:])
            except Exception:
                continue
            ev, d = m.get("event"), m.get("data") or {}
            if ev == "INTERACTION_CREATE":
                log(f"rid={rid} 交互事件: {json.dumps(d, ensure_ascii=False)[:200]}")
                if plan:
                    res = parse_interaction(rid, d)
                    if isinstance(res, tuple):
                        collector.add_button(res[0], res[1])
                        log(f"rid={rid} 已收 {len(collector.answers)}/{len(questions)} 题按钮答案")
                        if collector.done():
                            final = collector.render()
                            _write_answer(rid, final)
                            send_card({"msg_type": 0, "content": f"✅ 已收到你的回答，已提交给 ZCode：{final[:80]}",
                                       "msg_seq": random.randint(1, 10**6)})
                            return 0
                else:
                    res = parse_interaction(rid, d)
                    if res in ("allow", "deny"):
                        _write_answer(rid, res)
                        return 0
            elif ev == "C2C_MESSAGE_CREATE":
                if plan:
                    content = str(d.get("content") or "").strip()
                    if content:
                        collector.add_text(content)
                        if collector.done():
                            final = collector.render()
                            _write_answer(rid, final)
                            send_card({"msg_type": 0, "content": f"✅ 已收到你的回答，已提交给 ZCode：{final[:80]}",
                                       "msg_seq": random.randint(1, 10**6)})
                            return 0
                        log(f"rid={rid} 文本已记 {len(collector.answers)}/{len(questions)} 题，继续等待剩余题目")
                else:
                    handle_message(rid, d)


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
            code = main("selftest", 20, selftest=True)
        elif len(sys.argv) >= 5 and sys.argv[1] == "choice" and RID_RE.match(sys.argv[2]):
            code = main(sys.argv[2], float(sys.argv[3]), selftest=False, plan=sys.argv[4])
        elif len(sys.argv) >= 3 and RID_RE.match(sys.argv[1]):
            code = main(sys.argv[1], float(sys.argv[2]), selftest=False)
        else:
            code = 1
            log(f"参数无效 argv={sys.argv}")
    except Exception as e:
        log(f"进程异常退出: {e!r}")
        code = 1
    sys.exit(code)
