#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行发送 QQ 通知，供 Hook(command 类型) 或手动调用。

凭据单一来源：直接读取 ~/.workbuddy/mcp.json 中通知连接器的 env
（qq-notify 已改名为 gate-notify，按候选名 + 兜底扫全部 server 解析，
  不再依赖某个固定名字），避免重复维护。也允许用环境变量覆盖（便于在其他上下文调用）。

卡片实现与 ZCode 现行方案 (~/.zcode/qq-notify.sh) 一致：
Markdown + keyboard 回调按钮（旧 Ark 模板 23 客户端不渲染，已弃用），
按钮点击/文本回复经本机 WebSocket 监听 (~/.zcode/qq-bot-listener.py) 回传。

用法:
  python notify_qq.py "消息内容"               # 纯文本单聊 (msg_type 0)
  python notify_qq.py --markdown "内容"        # 纯 Markdown (msg_type 2)
  python notify_qq.py --card "问题?" "a;b;c"   # Markdown 卡片 + 选项按钮
  python notify_qq.py --ask "问题?" "a;b"      # 发卡片并等待按钮/回复（默认 90s，QQ_ASK_WAIT 可调）
  python notify_qq.py --preset task_done       # 预设卡片（ASCII 参数，Hook 专用，规避
                                               #   Windows 命令行传非 ASCII 的乱码问题）
  python notify_qq.py --event                  # 转发模式：读取 Hook 经 stdin 传入的事件 JSON
  python notify_qq.py --stop                   # 智能模式：仅当代理像在提问/等确认时发提问卡片
  python notify_qq.py --perm                   # 权限请求钩子：读 PermissionRequest 载荷，
                                               #   后台异步发「等你授权」，自身立即退出（不阻塞弹窗）

版式（所有模式统一，实现在 qq_notifier_mcp.decorate / card_header）:
  纯文本: 第 1 行 "🗂 <会话标题>"，第 2 行正文（折叠成一行、<=100 字符）
  卡片  : 标题行尾部追加 " · 🗂 <会话标题>"，正文同上限
"""

import os
import sys
import time

try:  # Windows 控制器/重定向环境下防止 emoji 触发 UnicodeEncodeError 导致 Hook 静默失败
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

MCP_JSON = os.path.expanduser("~/.workbuddy/mcp.json")
# Hook 的 stdout/stderr 会被吞掉，出错要落到文件里，否则映射一断就是"静默失联"
LOG_FILE = os.path.expanduser("~/.workbuddy/logs/qq-notify-hook.log")


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _log_fail(msg: str) -> None:
    _log(msg)


# mcp.json 里这个连接器改过名(qq-notify -> gate-notify)，服务名和脚本目录都会变。
# 这里只按"候选名优先 + 兜底扫全部 server"来解析，改名后不用再来回改脚本。
SERVER_NAMES = ("gate-notify", "qq-notify", "qq-notifier")
ENV_KEYS = (
    "QQ_BOT_APPID", "QQ_BOT_SECRET", "QQ_TARGET_OPENID",
    "QQ_BOT_API_BASE", "QQ_BOT_TOKEN_URL", "QQ_NOTIFY_DRY_RUN",
    # 统一网关(noti-gateway)：不补这三个，Hook 侧就会绕过网关直连 QQ
    "QQ_GATEWAY_URL", "QQ_GATEWAY_SECRET", "QQ_GATEWAY_KEY_ID", "QQ_GATEWAY_SOURCE",
)

# 判断「代理在上一条消息里像是在问用户 / 等用户确认」的关键词
ASK_HINTS = [
    "？", "?", "是否", "能否", "可以吗", "对吗", "对不对", "是不是",
    "确认", "等你", "需要你", "待你", "你决定", "你选", "请确认", "请回复",
    "回复我", "告诉我", "你的意见", "怎么看", "如何选择", "批准", "审核",
]

# Hook 专用预设（命令行只传 ASCII，文案在这里生成）
PRESETS = {
    "task_done": {
        "header": "✅ **WorkBuddy：任务已完成**",
        "question": "请查看对话中的结果。有问题直接回复本消息。",
        "options": "👍 收到",
    },
    "session_end": {
        "header": "👋 **WorkBuddy：本次会话已结束**",
        "question": "需要继续请回到 WorkBuddy。",
        "options": "👍 收到",
    },
}


def _iter_envs():
    """按候选名优先、其余兜底的顺序产出 (server名, env)，用于解析凭据。"""
    try:
        import json
        with open(MCP_JSON, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:  # noqa: BLE001
        print("读取 mcp.json 失败:", e, file=sys.stderr)
        return
    servers = d.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return
    order = [n for n in SERVER_NAMES if n in servers]
    order += [n for n in servers if n not in order]
    for name in order:
        env = (servers.get(name) or {}).get("env")
        if isinstance(env, dict):
            yield name, env


def load_cfg():
    """环境变量优先；缺的键从 mcp.json 里"第一个配了 QQ_BOT_APPID 的 server"补齐。"""
    cfg = {k: os.environ.get(k, "") for k in ENV_KEYS}
    if cfg["QQ_BOT_APPID"] and cfg["QQ_TARGET_OPENID"]:
        return cfg
    for _name, env in _iter_envs():
        if not env.get("QQ_BOT_APPID"):
            continue
        for k in ENV_KEYS:
            if not cfg[k] and k in env:
                cfg[k] = env[k]
        if cfg["QQ_BOT_APPID"] and cfg["QQ_TARGET_OPENID"]:
            break
    return cfg


def read_stdin_json():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        data = sys.stdin.read()
        if not data.strip():
            return None
        import json
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


def last_assistant_text(event):
    import json
    for key in ("message", "content", "text", "last_message"):
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            return v
    tp = event.get("transcript_path") or event.get("transcriptPath")
    if tp and os.path.exists(tp):
        try:
            last = ""
            with open(tp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    role = None
                    msg = obj.get("message") or obj
                    if isinstance(msg, dict):
                        role = msg.get("role")
                        content = msg.get("content")
                        if role == "assistant" and isinstance(content, str):
                            last = content
                        elif role == "assistant" and isinstance(content, list):
                            txt = "".join(
                                c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                            if txt:
                                last = txt
            return last
        except Exception:  # noqa: BLE001
            return ""
    return ""


def looks_like_question(text):
    if not text:
        return False
    return any(h in text for h in ASK_HINTS)


def _ensure_cfg():
    cfg = load_cfg()
    for k, v in cfg.items():
        if v:
            os.environ[k] = v
    if not cfg["QQ_BOT_APPID"] or not cfg["QQ_TARGET_OPENID"]:
        msg = "错误: 缺少 QQ_BOT_APPID 或 QQ_TARGET_OPENID（请在 mcp.json 配置）"
        print(msg, file=sys.stderr)
        _log_fail(f"{msg} | 已尝试 server: {[n for n, _ in _iter_envs()] or '无'}")
        sys.exit(2)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import qq_notifier_mcp as q
    return q, q.TARGET_OPENID


def send(content):
    q, target = _ensure_cfg()
    print(q.send_c2c(target, q.decorate(content)))


def mode_markdown(text):
    q, target = _ensure_cfg()
    print(q.send_markdown(target, q.decorate(text or "WorkBuddy 通知")))


def _card_md(q, question, options, header):
    """构造卡片正文；header 自动追加当前会话标题。"""
    return q.build_question_card(
        q.clip_text(question or "你收到了吗？"),
        options or "✅ 收到;❌ 没收到",
        q.card_header(header),
    )


def mode_card(question, options=None, header="❓ **WorkBuddy 提问**"):
    q, target = _ensure_cfg()
    md, plain, buttons, input_button = _card_md(q, question, options, header)
    print(q.send_card(target, md, buttons, input_button, plain_text=plain))


def mode_ask(question, options=None):
    q, target = _ensure_cfg()
    md, plain, buttons, input_button = _card_md(
        q, question, options, "❓ **WorkBuddy 提问**")
    t0 = int(q.time.time() * 1000)
    print(q.send_card(target, md, buttons, input_button, plain_text=plain))
    wait = int(os.environ.get("QQ_ASK_WAIT", "90"))
    if not q.listener_alive():
        print("⚠️ 本机监听服务未运行，无法等待按钮点击；请直接看对话")
        return
    ans = q.wait_qq_answer(wait, t0_ms=t0)
    if ans:
        print(f"👉 用户回答（{ans[0]}）: {ans[1]}")
    else:
        print(f"⏳ {wait}s 内未收到回答")


# ---------- 权限请求通知 (PermissionRequest 钩子) ----------
# 桌面端的授权弹窗只发 PermissionRequest 事件（Notification 里的 permission_prompt
# 仅在 CLI REPL 路径触发），所以必须挂这个事件才会有人收到「等你授权」。
# 注意：该钩子的返回值会影响授权决策——stdout 必须保持为空，否则可能被当成
# 决策载荷解析；同时它会被授权流程同步等待，所以发送放到后台进程，钩子立即返回。

def _spawn_worker(argv):
    """起一个完全脱离的子进程（不继承管道，钩子可立即退出）。"""
    import subprocess
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True,
              cwd=os.path.dirname(os.path.abspath(__file__)))
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def _tool_brief(event):
    """从 PermissionRequest 载荷里抽出「工具名 + 一句可读的操作摘要」。"""
    tool = str(event.get("tool_name") or "工具")
    ti = event.get("tool_input")
    detail = ""
    if isinstance(ti, dict):
        for key in ("command", "file_path", "path", "notebook_path", "url",
                    "pattern", "query", "prompt", "description"):
            v = ti.get(key)
            if isinstance(v, str) and v.strip():
                detail = v.strip()
                break
        if not detail:
            try:
                import json as _json
                detail = _json.dumps(ti, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                detail = ""
    elif isinstance(ti, str):
        detail = ti.strip()
    if detail in ("{}", "[]", '""'):
        detail = ""
    return tool, detail


def mode_perm():
    """PermissionRequest 钩子入口：落盘载荷 -> 后台发送 -> 立即 exit 0。"""
    event = read_stdin_json() or {}
    import json
    import tempfile
    fd, path = tempfile.mkstemp(prefix="qq-perm-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(event, f, ensure_ascii=False)
    if os.environ.get("QQ_PERM_SYNC") == "1":  # 便于本地联调，不脱离进程
        mode_perm_send(path)
        return
    try:
        _spawn_worker([sys.executable or "python",
                       os.path.abspath(__file__), "--perm-send", path])
    except Exception:  # noqa: BLE001
        import traceback
        _log_fail("权限通知后台进程启动失败:\n" + traceback.format_exc())


def mode_perm_send(path):
    """后台工作进程：读载荷、发一条「等你授权」的纯文本通知。"""
    import json
    event = {}
    try:
        with open(path, encoding="utf-8") as f:
            event = json.load(f) or {}
    except Exception:  # noqa: BLE001
        pass
    try:
        os.unlink(path)
    except Exception:  # noqa: BLE001
        pass
    tool, detail = _tool_brief(event if isinstance(event, dict) else {})
    content = f"🔐 等你授权：{tool}"
    if detail:
        content += f" · {detail}"
    try:
        q, target = _ensure_cfg()
        resp = q.send_c2c(target, q.decorate(content))
        _log(f"[perm] 已发送 tool={tool} | {str(resp)[:160]}")
    except SystemExit:
        _log_fail(f"[perm] 凭据缺失，权限通知未发送（tool={tool}）")
    except Exception:  # noqa: BLE001
        import traceback
        _log_fail("[perm] 权限通知发送失败:\n" + traceback.format_exc())


def mode_event():
    event = read_stdin_json() or {}
    content = None
    for key in ("message", "title", "content", "text", "notification", "summary"):
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            content = v.strip()
            break
    if not content:
        content = "🔔 WorkBuddy 提醒：请查看当前进度 / 待办"
    send(content)


def mode_stop():
    event = read_stdin_json() or {}
    text = last_assistant_text(event)
    if looks_like_question(text):
        q, target = _ensure_cfg()
        md, plain, buttons, input_button = _card_md(
            q, "我需要你的输入 / 确认，请回到 WorkBuddy 查看对话。",
            "✅ 去看看", "❓ **WorkBuddy 提问**")
        print(q.send_card(target, md, buttons, input_button, plain_text=plain))
    else:
        sys.exit(0)


def _dispatch(args):
    if args and args[0] == "--perm":
        mode_perm()
        return
    if args and args[0] == "--perm-send":
        mode_perm_send(args[1] if len(args) > 1 else "")
        return
    if args and args[0] == "--event":
        mode_event()
        return
    if args and args[0] == "--stop":
        mode_stop()
        return
    if args and args[0] == "--markdown":
        mode_markdown(" ".join(args[1:]))
        return
    if args and args[0] == "--card":
        mode_card(" ".join(args[1:2]), args[2] if len(args) > 2 else None)
        return
    if args and args[0] == "--ask":
        mode_ask(" ".join(args[1:2]), args[2] if len(args) > 2 else None)
        return
    if args and args[0] == "--preset":
        name = args[1] if len(args) > 1 else ""
        if name not in PRESETS:
            print(f"未知预设: {name}（可选: {'/'.join(PRESETS)}）", file=sys.stderr)
            sys.exit(2)
        p = PRESETS[name]
        mode_card(p["question"], p["options"], header=p["header"])
        return
    content = " ".join(args) or "WorkBuddy 通知"
    send(content)


def main():
    try:
        _dispatch(sys.argv[1:])
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        _log_fail("未捕获异常:\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
