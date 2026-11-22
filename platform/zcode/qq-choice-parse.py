"""解析 PreToolUse(AskUserQuestion) 事件，输出选择题计划（双端并行模式，无空闲门控）。

用法: python qq-choice-parse.py <input.json>
输出:
  SKIP                    —— 无 questions 字段/解析失败 → 放行走本地 UI
  SID\t<session_id>       —— 会话 id（监视器用于侦测本地活动后自动退出）
  Q<TAB>序号<TAB>单选|多选<TAB>题干<TAB>选项1...
"""
import json
import sys


def main() -> None:
    try:
        raw = open(sys.argv[1], encoding="utf-8", errors="replace").read()
        j = json.loads(raw)
        qs = ((j.get("tool_input") or {}).get("questions")) or []
        sid = str(j.get("session_id") or "")
    except Exception:
        print("SKIP")
        return
    if not qs:
        print("SKIP")
        return
    clean = lambda s: str(s).replace("\t", " ").replace("\n", " ")  # noqa: E731
    print("CHOICE")
    print("SID\t" + sid)
    for i, q in enumerate(qs):
        ms = "多选" if q.get("multiSelect") else "单选"
        text = clean(q.get("question") or q.get("header") or "").strip()
        labels = [clean(o.get("label") or "").strip() for o in (q.get("options") or [])][:6]
        print("\t".join(["Q", str(i), ms, text[:100]] + labels))


if __name__ == "__main__":
    main()
