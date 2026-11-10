"""入站规范化：把 curl / HTTP / WS / 各平台原生 webhook 收敛成 Notification。"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

from .models import Notification, LEVELS

MAX_BODY = 256 * 1024

TITLE_KEYS = ("title", "subject", "msg", "message", "text", "content", "summary", "event")
BODY_KEYS = ("body", "content", "message", "desc", "description", "detail", "details", "text")
LEVEL_KEYS = ("level", "severity", "status", "priority", "loglevel")
ROUTE_KEYS = ("route", "channel", "topic")
SOURCE_KEYS = ("source", "service", "app", "from", "sender")


def _pick(d: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return default


def _norm_level(v: str) -> str:
    v = (v or "").strip().lower()
    mapping = {
        "err": "error", "fatal": "error", "critical": "error", "crit": "error",
        "warn": "warn", "warning": "warn", "alert": "warn",
        "dbg": "debug", "trace": "debug",
        "notice": "info", "ok": "info",
    }
    v = mapping.get(v, v)
    return v if v in LEVELS else "info"


def _unwrap(payload: dict) -> dict:
    """处理常见包裹结构：{"alerts":[{...}]} / {"text":{...}} 等。"""
    for key in ("alerts", "events", "records", "data", "result"):
        v = payload.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return {**{k: val for k, val in payload.items() if k != key}, **v[0]}
        if isinstance(v, dict) and any(k in v for k in TITLE_KEYS):
            return {**{k: val for k, val in payload.items() if k != key}, **v}
    return payload


async def _read_body(req: Request) -> bytes:
    body = b""
    async for chunk in req.stream():
        body += chunk
        if len(body) > MAX_BODY:
            break
    return body


def _decode_body(raw: bytes) -> str:
    """Windows 下 curl/脚本把中文放命令行参数会变成 GBK 字节，且不写 charset。
    解码顺序：严格 UTF-8 -> 严格 GBK -> UTF-8 容错替换。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", "replace")


def _parse_body(raw: bytes, ctype: str) -> Any:
    if not raw:
        return {}
    text = _decode_body(raw).strip()
    if not text:
        return {}
    if "json" in ctype or text[:1] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "x-www-form-urlencoded" in ctype or "=" in text and " " not in text.split("\n")[0]:
        out: dict[str, Any] = {}
        for pair in text.split("&"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            from urllib.parse import unquote_plus

            out[unquote_plus(k)] = unquote_plus(v)
        if out:
            return out
    return text


def build_notification(
    payload: Any,
    source: str,
    route: str,
    query: dict[str, str] | None = None,
    meta: dict[str, Any] | None = None,
) -> Notification:
    """从任意解析结果构造 Notification。query 优先级高于 body（同键时以 query 为准）。"""
    meta = dict(meta or {})
    data: dict[str, Any] = {}

    if isinstance(payload, str):
        lines = payload.splitlines()
        title = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        data = {"title": title, "body": body, "fields": {}}
    elif isinstance(payload, dict):
        d = _unwrap(payload)
        meta.setdefault("raw", payload)
        title = _pick(d, TITLE_KEYS)
        body = _pick(d, BODY_KEYS)
        if title == body:
            body = ""
        consumed = set(TITLE_KEYS) | set(BODY_KEYS) | set(LEVEL_KEYS) | set(ROUTE_KEYS) | set(SOURCE_KEYS)
        fields = {k: v for k, v in d.items() if k not in consumed and not isinstance(v, (dict, list))}
        for k, v in d.items():
            if k not in consumed and isinstance(v, (dict, list)):
                fields[k] = v
        data = {
            "title": title,
            "body": body,
            "level": _norm_level(_pick(d, LEVEL_KEYS)),
            "route": _pick(d, ROUTE_KEYS),
            "fields": fields,
        }
    else:
        data = {"title": str(payload), "body": "", "fields": {}}

    # query 参数作为显式覆盖（?level=error&route=alerts 优先级最高）
    query = {k: v for k, v in (query or {}).items() if k not in ("token", "wait")}
    if "level" in query:
        data["level"] = _norm_level(query["level"])
    if query.get("route"):
        data["route"] = query["route"]
    if query.get("title"):
        data["title"] = query["title"]
    if query.get("body"):
        data["body"] = query["body"]
    if query.get("source"):
        source = query["source"]
    for k, v in query.items():
        if k not in ("level", "route", "title", "body", "source"):
            data["fields"][k] = v

    if not data.get("title") and data.get("body"):
        data["title"] = data["body"].splitlines()[0][:80]

    return Notification(
        source=source,
        level=data.get("level") or "info",
        title=data.get("title") or "",
        body=data.get("body") or "",
        fields=data.get("fields") or {},
        route=data.get("route") or route,
        meta=meta,
    )


async def parse_http(req: Request, source: str, route: str, raw: bytes | None = None) -> Notification:
    # raw 复用调用方已读出的原始字节（HMAC 需对原始体签名，避免二次消费 stream）
    if raw is None:
        raw = await _read_body(req)
    payload = _parse_body(raw, (req.headers.get("content-type") or "").lower())
    return build_notification(payload, source, route, dict(req.query_params))
