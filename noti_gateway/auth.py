"""鉴权：HMAC 多密钥签名（任一符合即接受）+ 可选来源 IP 白名单。

密钥来自实时读取的密钥文件（见 keyring.py），格式错误的行静默忽略，随时增删改即生效。

HMAC 签名（v1）::
    canonical = METHOD\\nPATH\\nTS\\nNONCE\\nBODY_SHA256_HEX
    X-Noti-Signature: v1=<HMAC_SHA256(secret, canonical) 的 hex>

其中 PATH 为请求路径（不含查询串），BODY_SHA256_HEX 为原始请求体的 sha256 十六进制。
命中某条 hmac 密钥后，source 以该密钥的 source（缺省取 id）为准，不采信请求头；
``strict_source`` 开启时还会强制请求头 X-Source 与之相符。

未签名请求一律 401（不存在共享口令的旁路通道）。WebSocket 握手即 GET 空体请求，
按相同算法对空体签名；浏览器无法自定义握手请求头时，签名头可用查询串
``sig``/``ts``/``nonce``/``key_id`` 传入（签名一次性、带时间窗，不是长期凭据）。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from collections.abc import Callable

from fastapi import Request, WebSocket

from .config import AuthCfg
from .keyring import Cred, load_creds

SIG_PREFIX = "v1="

# nonce 防重放缓存：key -> 过期时间戳（进程内；TTL = window，容量到阈值统一清理）
_NONCE: dict[str, float] = {}


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _b(s: str) -> bytes:
    return (s or "").encode("utf-8")


def _client_ip(req: Request) -> str:
    # 只有在明确信任上游代理时才采信 X-Forwarded-For
    return req.client.host if req.client else ""


def _ip_allowed(ip: str, allow: list[str]) -> bool:
    if not allow:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for rule in allow:
        try:
            if "/" in rule:
                if addr in ipaddress.ip_network(rule, strict=False):
                    return True
            elif addr == ipaddress.ip_address(rule):
                return True
        except ValueError:
            continue
    return False


def _body_sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw or b"").hexdigest()


def _canonical(method: str, path: str, ts: str, nonce: str, body_hex: str) -> bytes:
    return "\n".join([method.upper(), path, ts, nonce, body_hex]).encode("utf-8")


def _sign(secret: str, canonical: bytes) -> str:
    return hmac.new(_b(secret), canonical, hashlib.sha256).hexdigest()


def _prune_nonces(now: float) -> None:
    if len(_NONCE) < 4096:
        return
    for k in [k for k, exp in _NONCE.items() if exp <= now]:
        _NONCE.pop(k, None)


def _hmac_source(get_hdr: Callable[[str], str], method: str, path: str, raw: bytes, creds: list[Cred], window: int, strict: bool) -> str:
    """HMAC v1 验签并返回 source。``get_hdr(name)`` 读取签名头，HTTP 与 WebSocket 复用。"""
    sig_hdr = (get_hdr("x-noti-signature") or "").strip()
    if not sig_hdr:
        raise AuthError(401, "hmac signature required")
    if not sig_hdr.startswith(SIG_PREFIX):
        raise AuthError(401, "bad signature header")
    sig = sig_hdr[len(SIG_PREFIX):].strip()
    ts = (get_hdr("x-noti-timestamp") or "").strip()
    nonce = (get_hdr("x-noti-nonce") or "").strip()
    if not ts or not nonce:
        raise AuthError(401, "missing timestamp or nonce")
    try:
        ts_i = int(ts)
    except ValueError:
        raise AuthError(401, "bad timestamp") from None

    now = time.time()
    if abs(now - ts_i) > window:
        raise AuthError(401, "timestamp out of window")

    body_hex = _body_sha256_hex(raw)
    # 独立 body 指纹头（若提供须一致）：防中间人换体
    claim = (get_hdr("x-noti-content-sha256") or "").strip().lower()
    if claim and claim != body_hex:
        raise AuthError(401, "body hash mismatch")

    canonical = _canonical(method, path, ts, nonce, body_hex)

    hmac_creds = [c for c in creds if c.kind == "hmac"]
    hint = (get_hdr("x-noti-key-id") or "").strip()
    # OR 匹配：hint 命中的先试，其余全量；全程 compare_digest，不提前 break 以平衡时序
    ordered = hmac_creds
    if hint:
        ordered = [c for c in hmac_creds if c.id == hint] + [c for c in hmac_creds if c.id != hint]

    matched: Cred | None = None
    for c in ordered:
        expect = _sign(c.secret, canonical)
        if hmac.compare_digest(_b(expect), _b(sig)) and matched is None:
            matched = c
    if matched is None:
        raise AuthError(401, "signature mismatch")

    # nonce 防重放（同一密钥 + nonce 窗口内只放一次）
    key = f"{matched.id}:{nonce}"
    now2 = time.time()
    if _NONCE.get(key, 0) > now2:
        raise AuthError(401, "replay detected")
    _NONCE[key] = now2 + window
    _prune_nonces(now2)

    src = (matched.source or matched.id)[:64]
    declared = (get_hdr("x-source") or "").strip()[:64]
    if strict and declared and declared != src:
        raise AuthError(403, f"source mismatch: declared {declared}, key {src}")
    return src


def check_request(req: Request, cfg: AuthCfg, raw: bytes = b"") -> str:
    """HTTP 请求鉴权（HMAC 签名是唯一通道），返回 source。

    ``raw`` 为已读出的原始请求体（HMAC 对其签名；GET 端点传空字节即可）。
    """
    ip = _client_ip(req)
    if not _ip_allowed(ip, cfg.ip_allow):
        raise AuthError(403, f"ip {ip} not allowed")

    creds = load_creds(cfg.keys_file) if getattr(cfg, "keys_file", "") else []
    return _hmac_source(req.headers.get, req.method or "GET", req.url.path or "/", raw, creds, cfg.hmac_window_sec, cfg.strict_source)


_WS_SIG_Q = {
    "x-noti-signature": "sig",
    "x-noti-timestamp": "ts",
    "x-noti-nonce": "nonce",
    "x-noti-key-id": "key_id",
    "x-source": "source",
}


def check_websocket(ws: WebSocket, cfg: AuthCfg) -> str:
    """WebSocket 握手鉴权：握手即 GET 空体请求，按 HMAC v1 验签，返回 source。

    浏览器无法自定义 WebSocket 握手请求头，签名头可用查询串
    ``sig``/``ts``/``nonce``/``key_id`` 传入（签名一次性、带时间窗）。
    """
    ip = ws.client.host if ws.client else ""
    if not _ip_allowed(ip, cfg.ip_allow):
        raise AuthError(403, f"ip {ip} not allowed")

    def get_hdr(name: str) -> str:
        v = ws.headers.get(name)
        if v:
            return v
        qn = _WS_SIG_Q.get(name)
        return (ws.query_params.get(qn) or "") if qn else ""

    creds = load_creds(cfg.keys_file) if getattr(cfg, "keys_file", "") else []
    src = _hmac_source(get_hdr, "GET", ws.url.path or "/ws", b"", creds, cfg.hmac_window_sec, cfg.strict_source)
    # 显式 X-Source / ?source= 可声明归属（与旧版一致），缺省取密钥归属
    declared = (ws.headers.get("x-source") or ws.query_params.get("source") or "").strip()[:64]
    return (declared or src)[:64]
