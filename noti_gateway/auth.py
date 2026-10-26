"""鉴权：共享 Token + 可选来源 IP 白名单。

注意：只有一个共享 Token 时，source 不能信 body，必须由调用方通过
X-Source 头显式声明，否则任何人都能伪装成别的服务刷配额。
"""

from __future__ import annotations

import hmac
import ipaddress

from fastapi import Request, WebSocket

from .config import AuthCfg


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


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


def check_request(req: Request, cfg: AuthCfg) -> str:
    """HTTP 请求鉴权，返回调用方声明的 source。"""
    token = _extract_token(req)
    if not token or not hmac.compare_digest(token, cfg.token):
        raise AuthError(401, "invalid or missing token")

    ip = _client_ip(req)
    if not _ip_allowed(ip, cfg.ip_allow):
        raise AuthError(403, f"ip {ip} not allowed")

    return (req.headers.get("x-source") or "default").strip()[:64]


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
    if not token or not hmac.compare_digest(token, cfg.token):
        raise AuthError(401, "invalid or missing token")

    ip = ws.client.host if ws.client else ""
    if not _ip_allowed(ip, cfg.ip_allow):
        raise AuthError(403, f"ip {ip} not allowed")

    return (ws.headers.get("x-source") or ws.query_params.get("source") or "default").strip()[:64]
