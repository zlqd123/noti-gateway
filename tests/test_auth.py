"""鉴权测试：HMAC 多密钥 OR 验证 + 密钥文件实时解析 + 无签名请求一律 401。

覆盖最容易写错的点：
1. keyring 坏行静默忽略、enabled=0 停用、注释忽略（随时编辑随时启用）
2. HMAC 签名命中即通过，source 以命中的密钥为准（不采信请求头）
3. 重放（同 nonce 二次使用）被拒；时间窗外被拒；换体/坏签名被拒
4. 无签名（含旧 token 调用）一律 401；kind=token 行按坏行丢弃
5. /n/ 走真实 handler：单次读 body 同时供签名与解析（stream 不被消费两次）

运行：python tests/test_auth.py   或   pytest tests/test_auth.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noti_gateway.auth import AuthError, check_request, check_websocket  # noqa: E402
from noti_gateway.config import AuthCfg  # noqa: E402
from noti_gateway.keyring import load_creds, parse_creds  # noqa: E402

# 测试专用假密钥（真实密钥只存在于本地/服务器 keys.conf，绝不入库）
SECRET_DSH = "test-dsh-secret-do-not-use-000000000000000000000000000000"
SECRET_3 = "test-3-secret-do-not-use-0000000000000000000000000000000000"


def _req(method="POST", path="/n/dsh", headers=None, query=None, ip="127.0.0.1"):
    # 归一化为小写键：模拟 Starlette Headers 的大小写不敏感查找
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    return SimpleNamespace(
        method=method,
        headers=h,
        query_params=query or {},
        client=SimpleNamespace(host=ip),
        url=SimpleNamespace(path=path),
    )


def _sign(secret: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    body_hex = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join([method.upper(), path, ts, nonce, body_hex]).encode("utf-8")
    return "v1=" + hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _hmac_headers(secret: str, method="POST", path="/n/dsh", body=b'{"a":1}',
                  nonce="n-1", ts=None, source="dsh", key_id=None, sig=None):
    ts = ts if ts is not None else str(int(time.time()))
    body_hex = hashlib.sha256(body or b"").hexdigest()
    hdrs = {
        "x-noti-signature": sig if sig is not None else _sign(secret, method, path, ts, nonce, body),
        "x-noti-timestamp": ts,
        "x-noti-nonce": nonce,
        "x-noti-content-sha256": body_hex,
        "x-source": source,
    }
    if key_id:
        hdrs["x-noti-key-id"] = key_id
    return hdrs


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, name: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    # ---- 临时密钥文件（十组 + 停用 + 坏行 + 注释）----
    tmp = Path(tempfile.mkdtemp(prefix="noti-auth-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(
        "# 注释行\n"
        f"dsh {SECRET_DSH} kind=hmac source=dsh\n"
        f"3 {SECRET_3} kind=hmac source=3\n"
        "legacy test-legacy-token-do-not-use kind=token source=default\n"
        "off deadbeef kind=hmac enabled=0\n"
        "坏行没有secret\n"
        "badkind xyz kind=wat\n"
        "  \n",
        encoding="utf-8",
    )

    cfg = AuthCfg(
        keys_file=str(keys_path),
        hmac_window_sec=300,
        strict_source=False,
    )

    print("\n[1] keyring 实时解析")
    creds = load_creds(str(keys_path))
    ids = {c.id for c in creds}
    check(ids == {"dsh", "3"}, f"坏行/停用/kind=token 被丢弃，只剩有效项 (实际 {ids})")
    dsh = next(c for c in creds if c.id == "dsh")
    check(dsh.kind == "hmac" and dsh.source == "dsh", "hmac 项解析出 kind/source")
    check(dsh.attribution == "dsh", "attribution 回退到 id/source")
    # 热更新：改文件后立即生效（无缓存）
    keys_path.write_text(f"dsh {SECRET_DSH} kind=hmac source=NEW\n", encoding="utf-8")
    c2 = load_creds(str(keys_path))
    check(len(c2) == 1 and c2[0].source == "NEW", "编辑文件后实时生效（重读、无缓存）")
    keys_path.write_text(
        f"dsh {SECRET_DSH} kind=hmac source=dsh\n"
        f"3 {SECRET_3} kind=hmac source=3\n"
        "legacy test-legacy-token-do-not-use kind=token source=default\n",
        encoding="utf-8",
    )
    pc = parse_creds("# c\ngood sec1\nbroken\n")
    check(len(pc) == 1 and pc[0].id == "good", "parse_creds 坏行静默跳过")

    print("\n[2] HMAC 签名命中 + source 归属")
    body = b'{"title":"hi","body":"there"}'
    h = _hmac_headers(SECRET_DSH, body=body, nonce="nA", source="ZZZ")
    r = _req(headers=h, path="/n/dsh")
    try:
        src = check_request(r, cfg, body)
        check(src == "dsh", f"HMAC 命中返回密钥 source，而非请求头 (实际 {src})")
    except AuthError as e:
        check(False, f"HMAC 命中却抛错: {e.message}")

    print("\n[3] 重放 / 时间窗 / 坏签名 / 换体")
    # 重放：同 nonce 二次
    h2 = _hmac_headers(SECRET_DSH, body=body, nonce="nA", source="dsh")
    try:
        check_request(_req(headers=h2), cfg, body)
        check(False, "同 nonce 重放被拒")
    except AuthError as e:
        check(e.status == 401 and "replay" in e.message, f"同 nonce 重放被拒 ({e.message})")
    # 时间窗外
    old_ts = str(int(time.time()) - 4000)
    h3 = _hmac_headers(SECRET_DSH, body=body, nonce="nB", ts=old_ts, source="dsh")
    try:
        check_request(_req(headers=h3), cfg, body)
        check(False, "时间窗外被拒")
    except AuthError as e:
        check(e.status == 401, f"时间窗外被拒 ({e.message})")
    # 换体：签名对应 body，但实际传别的 body
    h4 = _hmac_headers(SECRET_DSH, body=b'{"a":1}', nonce="nC", source="dsh")
    try:
        check_request(_req(headers=h4), cfg, b'{"a":2}')
        check(False, "换体被拒")
    except AuthError as e:
        check(e.status == 401, f"换体被拒 ({e.message})")
    # 错误密钥签名
    h5 = _hmac_headers("wrong-secret", body=body, nonce="nD", source="dsh")
    try:
        check_request(_req(headers=h5), cfg, body)
        check(False, "错误密钥被拒")
    except AuthError as e:
        check(e.status == 401, f"错误密钥被拒 ({e.message})")

    print("\n[4] 无签名请求一律 401（token 通道已删除）")
    try:
        check_request(_req(headers={"Authorization": "Bearer shared-old-token",
                                    "X-Source": "ci"}), cfg, b"")
        check(False, "Bearer 旧口令被拒")
    except AuthError as e:
        check(e.status == 401, f"Bearer 旧口令被拒 ({e.message})")
    try:
        check_request(_req(query={"token": "test-legacy-token-do-not-use"}), cfg, b"")
        check(False, "?token= 旧口令被拒")
    except AuthError as e:
        check(e.status == 401, f"?token= 旧口令被拒 ({e.message})")
    try:
        check_request(_req(), cfg, b"")
        check(False, "无凭据被拒")
    except AuthError as e:
        check(e.status == 401, f"无凭据被拒 ({e.message})")

    print("\n[5] WebSocket 握手鉴权（GET 空体签名）")

    def _ws(headers=None, query=None):
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        return SimpleNamespace(
            headers=h,
            query_params=dict(query or {}),
            client=SimpleNamespace(host="127.0.0.1"),
            url=SimpleNamespace(path="/ws"),
        )

    wh = _hmac_headers(SECRET_DSH, method="GET", path="/ws", body=b"", nonce="ws-1", source="desk")
    try:
        src = check_websocket(_ws(headers=wh), cfg)
        check(src == "desk", f"WS 请求头签名通过 (实际 {src})")
    except AuthError as e:
        check(False, f"WS 请求头签名被拒: {e.message}")
    # 浏览器式：签名头走查询串
    ts = str(int(time.time()))
    sig = _sign(SECRET_DSH, "GET", "/ws", ts, "ws-2", b"")
    try:
        src = check_websocket(_ws(query={"sig": sig, "ts": ts, "nonce": "ws-2",
                                         "key_id": "dsh", "source": "browser"}), cfg)
        check(src == "browser", f"WS 查询串签名通过 (实际 {src})")
    except AuthError as e:
        check(False, f"WS 查询串签名被拒: {e.message}")
    try:
        check_websocket(_ws(query={"sig": sig, "ts": ts, "nonce": "ws-2"}), cfg)
        check(False, "WS 同 nonce 重放被拒")
    except AuthError as e:
        check(e.status == 401 and "replay" in e.message, f"WS 同 nonce 重放被拒 ({e.message})")
    try:
        check_websocket(_ws(query={"token": "anything"}), cfg)
        check(False, "WS 无签名被拒")
    except AuthError as e:
        check(e.status == 401, f"WS 无签名被拒 ({e.message})")

    print("\n[6] strict_source 强制请求头与密钥相符")
    s_cfg = AuthCfg(keys_file=str(keys_path), strict_source=True)
    try:
        check_request(_req(headers=_hmac_headers(SECRET_DSH, body=body, nonce="nE", source="WRONG")),
                      s_cfg, body)
        check(False, "strict_source 不符被拒")
    except AuthError as e:
        check(e.status == 403, f"strict_source 不符被拒 ({e.message})")
    try:
        src = check_request(_req(headers=_hmac_headers(SECRET_DSH, body=body, nonce="nF", source="dsh")),
                            s_cfg, body)
        check(src == "dsh", "strict_source 相符通过")
    except AuthError as e:
        check(False, f"strict_source 相符却抛错: {e.message}")

    # ---- e2e：走真实 handler，验证单次读 body + HMAC 通过、无签名一律 401 ----
    print("\n[7] e2e 真实 handler（单次读 body）")
    rc = asyncio.run(_e2e(str(keys_path)))
    for ok, name in rc:
        check(ok, name)

    print("\n" + "=" * 54)
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


async def _e2e(keys_path: str) -> list[tuple[bool, str]]:
    """起真实 app（ASGI 内存传输，不占端口），验证 /n/ 全链路鉴权。"""
    import yaml
    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="noti-auth-e2e-"))
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "server: { host: 127.0.0.1, port: 18099 }\n"
        "auth:\n"
        f"  keys_file: \"{keys_path.replace(chr(92), '/') }\"\n"
        "  hmac_window_sec: 300\n"
        "limits: { per_source_per_minute: 600, per_target_per_minute: 600, per_target_per_day: 100000 }\n"
        "dedup: { enabled: false, window_seconds: 1 }\n"
        "dispatch: { poll_interval: 0.2, concurrency: 2, max_attempts: 1 }\n"
        "qq: { app_id: \"T\", client_secret: \"S\", api_base: \"http://127.0.0.1:9\" }\n"
        "targets:\n"
        "  - id: hook:noop\n"
        "    type: webhook\n"
        "    url: \"http://127.0.0.1:9/noop\"\n"
        "    method: POST\n"
        "routes:\n"
        "  - source: [\"*\"]\n"
        "    targets: [hook:noop]\n",
        encoding="utf-8",
    )
    os.environ["NOTI_DB"] = str(tmp / "e2e.db")
    app = create_app(load_config(str(cfg_path)))

    out: list[tuple[bool, str]] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        body = b'{"title":"hmac-e2e","body":"b","level":"info"}'
        # HMAC 签名通过
        h = _hmac_headers(SECRET_DSH, method="POST", path="/n/dsh", body=body,
                          nonce="e2e-1", source="dsh")
        r = await c.post("/n/dsh", content=body, headers={**h, "content-type": "application/json"})
        out.append((r.status_code == 202, f"HMAC 签名 -> 202 (实际 {r.status_code})"))
        # 无签名的旧 token 调用被拒
        r = await c.post("/n/ci", content=body,
                         headers={"Authorization": "Bearer e2e-old-token", "X-Source": "ci",
                                  "content-type": "application/json"})
        out.append((r.status_code == 401, f"无签名旧调用 -> 401 (实际 {r.status_code})"))
        # 无凭据被拒
        r = await c.post("/n/dsh", content=body, headers={"content-type": "application/json"})
        out.append((r.status_code == 401, f"无凭据 -> 401 (实际 {r.status_code})"))
        # 换体（签名 body 与实际 body 不一致）被拒
        h2 = _hmac_headers(SECRET_DSH, method="POST", path="/n/dsh", body=b'{"x":1}',
                           nonce="e2e-2", source="dsh")
        r = await c.post("/n/dsh", content=body,
                         headers={**h2, "content-type": "application/json"})
        out.append((r.status_code == 401, f"e2e 换体 -> 401 (实际 {r.status_code})"))
    return out


def test_auth() -> None:
    """pytest 入口：复用自跑式 main()。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
