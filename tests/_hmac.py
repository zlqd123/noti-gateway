"""测试共用 HMAC v1 签名助手。

canonical 与 auth.py 严格一致：METHOD\\nPATH\\nTS\\nNONCE\\nBODY_SHA256_HEX。
供 test_reminders / test_retry / test_connections / selftest 等在 HMAC-only 鉴权下构造请求头。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

import httpx


def sign_headers(
    method: str,
    path: str,
    body: bytes = b"",
    secret: str = "test-secret",
    key_id: str = "t1",
) -> dict[str, str]:
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = "\n".join(
        [method.upper(), path, ts, nonce, hashlib.sha256(body or b"").hexdigest()]
    )
    sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Noti-Signature": f"v1={sig}",
        "X-Noti-Timestamp": ts,
        "X-Noti-Nonce": nonce,
        "X-Noti-Key-Id": key_id,
    }


def signed_json_headers(obj, path: str, secret: str = "test-secret", key_id: str = "t1") -> tuple[bytes, dict[str, str]]:
    """签名一组 JSON 请求：返回 (请求体字节, 请求头)。调用方 body 必须原样传这些字节。"""
    import json

    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return body, {**sign_headers("POST", path, body, secret, key_id), "Content-Type": "application/json"}


def sig_query(method: str, path: str, secret: str = "test-secret", key_id: str = "t1") -> dict[str, str]:
    """GET/SSE/WS 握手用的查询串签名（auth.py 对空体按相同 canonical 验签）。"""
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(b"").hexdigest()])
    sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"sig": f"v1={sig}", "ts": ts, "nonce": nonce, "key_id": key_id}


class HmacClient(httpx.AsyncClient):
    """自动给每个请求签名的测试客户端：调用点无需逐个改 headers。

    body 以实际发出的字节（req.content）为准，json=/data=/content= 均可。
    """

    def __init__(self, *, secret: str = "test-secret", key_id: str = "t1", **kw) -> None:
        super().__init__(**kw)
        self._secret = secret
        self._key_id = key_id

    def build_request(self, method, url, *, content=None, json=None, **kw):  # noqa: A002
        req = super().build_request(method, url, content=content, json=json, **kw)
        req.headers.update(
            sign_headers(req.method, req.url.path, req.content or b"", self._secret, self._key_id)
        )
        return req
