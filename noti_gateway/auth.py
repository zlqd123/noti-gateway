"""鉴权：HMAC 多密钥签名（任一符合即接受）+ 共享 Token 双通道 + 可选来源 IP 白名单。

密钥来自实时读取的密钥文件（见 keyring.py），格式错误的行静默忽略，随时增删改即生效。

HMAC 签名（v1）::
    canonical = METHOD\\nPATH\\nTS\\nNONCE\\nBODY_SHA256_HEX
    X-Noti-Signature: v1=<HMAC_SHA256(secret, canonical) 的 hex>

其中 PATH 为请求路径（不含查询串），BODY_SHA256_HEX 为原始请求体的 sha256 十六进制。
命中某条 hmac 密钥后，source 以该密钥的 source（缺省取 id）为准，不采信请求头；
``strict_source`` 开启时还会强制请求头 X-Source 与之相符。

兼容性：config.yaml 的 ``auth.token`` 与密钥文件里的 ``kind=token`` 条目继续被接受
（旧服务依赖共享 token，永不硬破坏）；带签名头但验签失败时，若未强制 HMAC 会回退
token 通道，避免切换期误伤。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time

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


def _extract_token(req: Request) -> str:
    auth = req.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 自定义头
    h = req.headers.get("x-noti-token")
    if h:
        return h.strip()
    if getattr(req, "query_params", None) is not None:
        return req.query_params.get("token", "")
    return ""


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


def _hmac_source(req: Request, raw: bytes, creds: list[Cred], window: int, strict: bool) -> str:
    sig_hdr = (req.headers.get("x-noti-signature") or "").strip()
    if not sig_hdr.startswith(SIG_PREFIX):
        raise AuthError(401, "bad signature header")
    sig = sig_hdr[len(SIG_PREFIX):].strip()
    ts = (req.headers.get("x-noti-timestamp") or "").strip()
    nonce = (req.headers.get("x-noti-nonce") or "").strip()
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
    claim = (req.headers.get("x-noti-content-sha256") or "").strip().lower()
    if claim and claim != body_hex:
        raise AuthError(401, "body hash mismatch")

    method = req.method or "GET"
    path = req.url.path or "/"
    canonical = _canonical(method, path, ts, nonce, body_hex)

    hmac_creds = [c for c in creds if c.kind == "hmac"]
    hint = (req.headers.get("x-noti-key-id") or "").strip()
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
    declared = (req.headers.get("x-source") or "").strip()[:64]
    if strict and declared and declared != src:
        raise AuthError(403, f"source mismatch: declared {declared}, key {src}")
    return src


def _token_source(req: Request, cfg: AuthCfg, creds: list[Cred]) -> str:
    token = _extract_token(req)
    if not token:
        raise AuthError(401, "invalid or missing token")
    ok = bool(cfg.token) and hmac.compare_digest(_b(token), _b(cfg.token))
    if not ok:
        for c in creds:
            if c.kind == "token" and hmac.compare_digest(_b(token), _b(c.secret)):
                ok = True
                break
    if not ok:
        raise AuthError(401, "invalid or missing token")
    return (req.headers.get("x-source") or "default").strip()[:64]


def check_request(req: Request, cfg: AuthCfg, raw: bytes = b"") -> str:
    """HTTP 请求鉴权，返回 source。

    ``raw`` 为已读出的原始请求体（HMAC 对其签名；GET 端点传空字节即可）。
    """
    ip = _client_ip(req)
    if not _ip_allowed(ip, cfg.ip_allow):
        raise AuthError(403, f"ip {ip} not allowed")

    creds = load_creds(cfg.keys_file) if getattr(cfg, "keys_file", "") else []

    sig = (req.headers.get("x-noti-signature") or "").strip()
    if getattr(cfg, "require_hmac", False) and not sig:
        raise AuthError(401, "hmac signature required")
    if sig:
        # 带签名头则 HMAC 是唯一权威：验签失败（重放/换体/超窗/strict 不符）直接拒，
        # 绝不降级到 token，避免签名通道被悄悄绕过。旧服务不发签名头，仍走下方 token 通道。
        return _hmac_source(req, raw, creds, cfg.hmac_window_sec, cfg.strict_source)
    return _token_source(req, cfg, creds)


def check_websocket(ws: WebSocket, cfg: AuthCfg) -> str:
    """WebSocket 鉴权（浏览器端无法自定义 header，故允许 query token）。"""
    if cfg.allow_query_token:
        token = ws.query_params.get("token", "")
    else:
        token = ""
    if not token:
        auth = ws.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise AuthError(401, "invalid or missing token")

    creds = load_creds(cfg.keys_file) if getattr(cfg, "keys_file", "") else []
    ok = bool(cfg.token) and hmac.compare_digest(_b(token), _b(cfg.token))
    if not ok:
        for c in creds:
            if c.kind == "token" and hmac.compare_digest(_b(token), _b(c.secret)):
                ok = True
                break
    if not ok:
        raise AuthError(401, "invalid or missing token")

    ip = ws.client.host if ws.client else ""
    if not _ip_allowed(ip, cfg.ip_allow):
        raise AuthError(403, f"ip {ip} not allowed")

    return (ws.headers.get("x-source") or ws.query_params.get("source") or "default").strip()[:64]
