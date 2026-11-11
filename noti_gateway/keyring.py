"""密钥表：实时读文件，格式错误静默忽略，随时编辑随时生效。

文件格式（UTF-8，一行一条，`#` 开头为注释，空行忽略）::

    <id> <secret> [kind=hmac] [source=NAME] [enabled=0|1]

- ``id``   ：密钥标识，``[A-Za-z0-9_-]{1,64}``
- ``secret``：HMAC 密钥（不可含空白）
- ``kind`` ：``hmac``（缺省即 hmac；保留该键仅为兼容既有密钥表格式）
- ``source``：归属来源；省略时取 ``id``。签名命中后 source 由本字段决定，不采信请求头。
- ``enabled``：``0/false/no/off`` 关闭该条而不删除（随时启用/停用）

解析规则：一行只要格式不对（缺字段、id/secret 非法、``key=value`` 非法或未知键、
kind/enabled 取值非法），该行**静默跳过**，不影响其余行；文件不存在/不可读/解码失败
返回空表。因此可以随时增删改行，改完即生效（每次鉴权都重新读盘，无缓存）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KNOWN_KEYS = {"kind", "source", "enabled"}
_MAX_FILE = 1024 * 1024  # 密钥文件 1MB 足够


@dataclass(frozen=True)
class Cred:
    id: str
    secret: str
    kind: str = "hmac"
    source: str = ""  # 归属来源；空 = 用 id
    enabled: bool = True

    @property
    def attribution(self) -> str:
        return (self.source or self.id).strip()[:64]


def _parse_line(line: str) -> Cred | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    # 行内注释：字段以 # 开头即视为注释起点，其后全部忽略
    toks: list[str] = []
    for t in s.split():
        if t.startswith("#"):
            break
        toks.append(t)

    if len(toks) < 2:
        return None  # 缺 id 或 secret

    cid, secret = toks[0], toks[1]
    if not _ID_RE.match(cid) or not secret:
        return None

    kind = "hmac"
    source = ""
    enabled = True
    for tok in toks[2:]:
        if "=" not in tok:
            return None  # 格式错误 → 整行忽略
        k, v = tok.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if k not in _KNOWN_KEYS:
            return None
        if k == "kind":
            if v != "hmac":
                return None
            kind = v
        elif k == "source":
            if not v:
                return None
            source = v
        else:  # enabled
            low = v.lower()
            if low in ("1", "true", "yes", "on"):
                enabled = True
            elif low in ("0", "false", "no", "off"):
                enabled = False
            else:
                return None

    return Cred(cid, secret, kind, source, enabled)


def parse_creds(text: str) -> list[Cred]:
    out: list[Cred] = []
    for line in text.splitlines():
        c = _parse_line(line)
        if c is not None and c.enabled:
            out.append(c)
    return out


def load_creds(path: str) -> list[Cred]:
    """实时读密钥文件并解析。任何异常都静默退化为空表（不抛、不打日志）。"""
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(_MAX_FILE)
    except (OSError, UnicodeDecodeError):
        return []
    return parse_creds(text)
