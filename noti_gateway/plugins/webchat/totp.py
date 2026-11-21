"""TOTP 动态码与会话令牌（RFC 6238，纯标准库实现，不引第三方依赖）。

- verify_totp：校验 6 位动态码，容忍 ±1 个 30 秒窗口的时钟偏差
- issue_session / verify_session：登录成功后签发无状态会话令牌
  （HMAC-SHA256 签名 + 签发时间，重启不失效，改密钥即全部失效）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # 允许 ±1 个时间窗（±30 秒）的时钟偏差
SESSION_DAYS = 7  # 会话令牌有效期（天），可被配置覆盖


def generate_secret() -> str:
    """生成 160 位随机密钥的 Base32 编码（无填充），供验证器导入。"""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return str(code % 10**digits).zfill(digits)


def verify_totp(secret_b32: str, code: str) -> bool:
    """校验动态码；前后各 1 个时间窗内均视为有效。"""
    try:
        key = base64.b32decode(secret_b32, casefold=True)
    except Exception:  # noqa: BLE001
        return False
    normalized = code.strip().replace(" ", "")
    if len(normalized) != TOTP_DIGITS or not normalized.isdigit():
        return False
    counter_now = int(time.time()) // TOTP_STEP
    for delta in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        if hmac.compare_digest(_hotp(key, counter_now + delta), normalized):
            return True
    return False


# ------------------------------------------------------------------ 会话

_SESSION_PREFIX = "webchat-session:"


def _session_key(secret_b32: str) -> bytes:
    return (_SESSION_PREFIX + secret_b32).encode("utf-8")


def issue_session(secret_b32: str, days: int = SESSION_DAYS) -> tuple[str, int]:
    """签发会话令牌，返回 (token, 有效期秒数)。"""
    issued = int(time.time())
    signature = hmac.new(
        _session_key(secret_b32), str(issued).encode(), hashlib.sha256
    ).hexdigest()
    return f"{issued}.{signature}", days * 86400


def verify_session(secret_b32: str, token: str, days: int) -> bool:
    """校验会话令牌签名与有效期（无状态，重启不失效）。"""
    try:
        issued_s, signature = token.split(".", 1)
        issued = int(issued_s)
        expect = hmac.new(
            _session_key(secret_b32), str(issued).encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expect):
            return False
        return time.time() - issued < days * 86400
    except (ValueError, AttributeError):
        return False
