"""入站规范化：把 curl / HTTP / WS / 各平台原生 webhook 收敛成 Notification。"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from starlette.requests import Request

from .models import Notification, LEVELS

# 内联 base64 图/文件（解码后 ≤10MB）单请求可容；更大走 /u/* 分片上传用 ref 引用
MAX_BODY = 16 * 1024 * 1024

TITLE_KEYS = ("title", "subject", "msg", "message", "text", "content", "summary", "event")
BODY_KEYS = ("body", "content", "message", "desc", "description", "detail", "details", "text")
LEVEL_KEYS = ("level", "severity", "status", "priority", "loglevel")
ROUTE_KEYS = ("route", "channel", "topic")
SOURCE_KEYS = ("source", "service", "app", "from", "sender")
MEDIA_KEYS = ("image", "file")

IMAGE_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
MAX_INLINE_BYTES = 10 * 1024 * 1024  # QQ 简单上传（/files file_data 内联）实测上限
MAX_URL_BYTES = 20 * 1024 * 1024     # {"file": {"url": ...}} 网关代下载上限


def _norm_media(v: Any, kind: str) -> dict | None:
    """规范化 image/file 字段：{"url"} / {"b64", mime?, name?} 或 {"ref"}。非法抛 ValueError。"""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError(f"{kind} 必须是对象 {{url | b64, mime?, name?}} 或 {{ref}}")
    if v.get("ref") is not None:
        ref = str(v.get("ref") or "").strip()
        if not ref:
            raise ValueError(f"{kind}.ref 不能为空")
        return {"ref": ref}
    url = str(v.get("url") or "").strip()
    if url:
        if not re.match(r"^https?://", url, re.I):
            raise ValueError(f"{kind}.url 必须是 http(s) 地址: {url[:120]}")
        from .uploads import safe_name

        return {
            "url": url,
            "name": safe_name(str(v.get("name") or ""), ""),  # 留空 = 下载阶段从 URL/响应头推断
            "mime": (str(v.get("mime") or "")).strip().lower(),
        }
    b64 = v.get("b64")
    if not isinstance(b64, str) or not b64.strip():
        raise ValueError(f"{kind}.b64 必须是非空 base64 字符串（或改用 ref / url）")
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        raise ValueError(f"{kind}.b64 不是合法 base64") from None
    if not raw:
        raise ValueError(f"{kind}.b64 解码后为空")
    if len(raw) > MAX_INLINE_BYTES:
        raise ValueError(
            f"{kind} 解码后 {len(raw)} 字节超过内联上限 {MAX_INLINE_BYTES}，请走 /u/* 分片上传再用 ref 引用"
        )
    mime = (str(v.get("mime") or "")).strip().lower()
    if kind == "image":
        mime = mime or "image/jpeg"
        if mime not in IMAGE_MIME_EXT:
            raise ValueError(f"image.mime 不支持: {mime}（支持 {', '.join(IMAGE_MIME_EXT)}）")
        default_name = f"image{IMAGE_MIME_EXT[mime]}"
    else:
        mime = mime or "application/octet-stream"
        default_name = "file.bin"
    from .uploads import safe_name

    name = safe_name(str(v.get("name") or ""), default_name)
    return {"b64": b64, "mime": mime, "name": name}


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
        # raw 只做排障用：剔除媒体大字段，避免 base64 在队列里存两份
        meta.setdefault("raw", {k: v for k, v in payload.items() if k not in MEDIA_KEYS})
        title = _pick(d, TITLE_KEYS)
        body = _pick(d, BODY_KEYS)
        if title == body:
            body = ""
        consumed = (
            set(TITLE_KEYS) | set(BODY_KEYS) | set(LEVEL_KEYS)
            | set(ROUTE_KEYS) | set(SOURCE_KEYS) | set(MEDIA_KEYS)
        )
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
            "image": _norm_media(d.get("image"), "image"),
            "file": _norm_media(d.get("file"), "file"),
        }
    else:
        data = {"title": str(payload), "body": "", "fields": {}, "image": None, "file": None}

    # query 参数作为显式覆盖（?level=error&route=alerts 优先级最高）
    query = {k: v for k, v in (query or {}).items() if k not in ("sig", "ts", "nonce", "key_id", "wait")}
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
        image=data.get("image"),
        file=data.get("file"),
        route=data.get("route") or route,
        meta=meta,
    )


async def parse_http(req: Request, source: str, route: str, raw: bytes | None = None) -> Notification:
    # raw 复用调用方已读出的原始字节（HMAC 需对原始体签名，避免二次消费 stream）
    if raw is None:
        raw = await _read_body(req)
    ctype = (req.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype:
        payload = await parse_multipart(raw, ctype)
    else:
        payload = _parse_body(raw, ctype)
    return build_notification(payload, source, route, dict(req.query_params))


# ---------------- multipart/form-data（浏览器 / curl -F 友好） ----------------

def _decode_field(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset)
    except (UnicodeDecodeError, LookupError):
        pass
    try:
        return raw.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", "replace")


def parse_multipart_sync(raw: bytes, ctype_header: str) -> dict[str, Any]:
    """解析 multipart/form-data（标准库 email，二进制安全，零新增依赖）。

    文本字段进 payload（title/body/level/route 等）；带 filename 的字段按字段名
    归入媒体：``image`` -> image，其余 -> file。返回 payload dict。
    """
    from email.parser import BytesParser

    msg = BytesParser().parsebytes(
        b"Content-Type: " + ctype_header.encode("latin-1")
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
    )
    out: dict[str, Any] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = (part.get_param("name", header="content-disposition") or "").strip().strip('"')
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        if filename:
            kind = "image" if name == "image" else "file"
            mime = (part.get_content_type() or "").lower()
            out[kind] = {"_bytes": payload, "name": filename, "mime": mime}
        else:
            charset = part.get_content_charset() or "utf-8"
            out[name or "body"] = _decode_field(payload, charset)
    return out


async def parse_multipart(raw: bytes, ctype_header: str) -> dict[str, Any]:
    """parse_multipart_sync 的异步包装：超内联上限的文件字段落盘为 ref。"""
    from .uploads import get as get_store, safe_name

    out = parse_multipart_sync(raw, ctype_header)
    for kind in ("file", "image"):
        media = out.get(kind)
        if not isinstance(media, dict) or "_bytes" not in media:
            continue
        data: bytes = media.pop("_bytes")
        if not data:
            raise ValueError(f"{kind} 字段内容为空")
        if len(data) > MAX_INLINE_BYTES:
            store = get_store()
            mime = media.get("mime") or ("image/jpeg" if kind == "image" else "application/octet-stream")
            meta = await asyncio.to_thread(
                store.init, safe_name(media.get("name") or "", "file.bin"), mime, kind, len(data)
            )
            uid = meta["upload_id"]
            step = max(1, int(meta.get("chunk_size") or 4 * 1024 * 1024))
            for i in range((len(data) + step - 1) // step):
                await asyncio.to_thread(store.put_part, uid, i, data[i * step:(i + 1) * step])
            ref = await asyncio.to_thread(store.complete, uid)
            out[kind] = {"ref": ref["ref"]}
        else:
            media["b64"] = base64.b64encode(data).decode()
    return out


# ---------------- {"file": {"url": ...}} 网关代下载 ----------------

async def resolve_media_urls(n: Notification, cli: httpx.AsyncClient | None = None) -> Notification:
    """把 {"url": ...} 形式的 image/file 代下载并落盘为 ref，替换原字段。

    只处理 url 引用（b64/ref 原样跳过）；失败抛 ValueError（调用方转 4xx/错误回执）。
    """
    from .uploads import get as get_store, safe_name

    for kind in ("image", "file"):
        media = getattr(n, kind)
        if not isinstance(media, dict) or not media.get("url") or media.get("ref") or media.get("b64"):
            continue
        url = str(media["url"])
        own = cli is None
        client = cli or httpx.AsyncClient(timeout=30, follow_redirects=True)
        try:
            try:
                r = await client.get(url)
            except Exception as e:
                raise ValueError(f"{kind}.url 下载失败: {e!r} ({url[:120]})") from None
            if r.status_code >= 400:
                raise ValueError(f"{kind}.url 下载失败 http {r.status_code}: {url[:120]}")
            raw = r.content
            if not raw:
                raise ValueError(f"{kind}.url 下载内容为空: {url[:120]}")
            if len(raw) > MAX_URL_BYTES:
                raise ValueError(f"{kind}.url 下载 {len(raw)} 字节超过上限 {MAX_URL_BYTES}")
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            cd = r.headers.get("content-disposition") or ""
            m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
            disp_name = unquote(m.group(1)).strip() if m else ""
            path_name = urlparse(url).path.rsplit("/", 1)[-1]
            guessed = (mimetypes.guess_type(path_name)[0] or "").lower()
            name = media.get("name") or disp_name or path_name
            mime = media.get("mime") or ctype or guessed
            if kind == "image":
                mime = mime or "image/jpeg"
                if mime not in IMAGE_MIME_EXT:
                    raise ValueError(f"{kind}.url 的 mime 不支持: {mime}（支持 {', '.join(IMAGE_MIME_EXT)}）")
                name = name or f"image{IMAGE_MIME_EXT[mime]}"
            else:
                mime = mime or "application/octet-stream"
                name = name or "file.bin"
            store = get_store()
            meta = await asyncio.to_thread(store.init, safe_name(name, "file.bin"), mime, kind, len(raw))
            uid = meta["upload_id"]
            step = max(1, int(meta.get("chunk_size") or 4 * 1024 * 1024))
            for i in range((len(raw) + step - 1) // step):
                await asyncio.to_thread(store.put_part, uid, i, raw[i * step:(i + 1) * step])
            ref = await asyncio.to_thread(store.complete, uid)
            setattr(n, kind, {"ref": ref["ref"]})
        finally:
            if own:
                await client.aclose()
    return n
