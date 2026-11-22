"""接收侧多形态端到端测试：multipart / 裸二进制 /f / url 代下载 / WS 媒体。

运行：python tests/test_receive_variants.py
不需要真实 QQ 凭证，也不需要联网（url 测试指向本地假 QQ 的下载路由）。

覆盖：
  [1] multipart /n：zip 二进制文件字段 + 文本字段（email 解析二进制回转）
  [2] multipart GBK 文本字段（Windows curl 场景）
  [3] /f 裸二进制 + send=1：直接构造成通知发送
  [4] /f 仅落盘返回 ref，再 /n 用 ref 引用发送
  [5] /n {"file": {"url": ...}} 网关代下载（Content-Disposition 文件名生效）
  [6] url 下载失败（404）-> 400 带错误信息
  [7] WS 上行 {"file": {"url": ...}} 同样代下载
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import uvicorn
import websockets
from fastapi import FastAPI, Request, Response
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/ 下的 _hmac 助手

KEY_ID = "recv-test"
SECRET = "recv-test-secret-do-not-use-2222222222222222222222222222"
PORT = 10101        # 网关
QQ_PORT = 10102     # 假 QQ API（含下载路由）
QQ_BASE = f"http://127.0.0.1:{QQ_PORT}"

STATE = {
    "messages": [],
    "merges": [],
    "preps": [],
    "cos": {},
    "downloads": [],
}

DL_FILES = {
    "doc1": (b"PK\x03\x04 url-download-content-" + b"u" * 32, "application/zip", "report(1).zip"),
}


def _sign(method: str, path: str, body: bytes) -> dict:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_hex = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join([method.upper(), path, ts, nonce, body_hex]).encode("utf-8")
    sig = "v1=" + hmac.new(SECRET.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return {
        "X-Noti-Signature": sig,
        "X-Noti-Timestamp": ts,
        "X-Noti-Nonce": nonce,
        "X-Noti-Key-Id": KEY_ID,
    }


def build_mock_qq() -> FastAPI:
    app = FastAPI()

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.post("/v2/users/{uid}/messages")
    async def send(uid: str, request: Request):
        STATE["messages"].append(await request.json())
        return {"id": f"MSG{len(STATE['messages'])}"}

    @app.post("/v2/users/{uid}/upload_prepare")
    async def prep(uid: str, request: Request):
        body = await request.json()
        STATE["preps"].append(body)
        size = int(body["file_size"])
        block = 4 * 1024 * 1024
        n = (size + block - 1) // block
        up = f"up{len(STATE['preps'])}"
        return {
            "upload_id": up,
            "block_size": str(block),
            "parts": [{"index": i, "presigned_url": f"{QQ_BASE}/cos/{up}/{i}"} for i in range(n)],
        }

    @app.put("/cos/{up}/{i}")
    async def cos(up: str, i: int, request: Request):
        STATE["cos"][(up, int(i))] = await request.body()
        return {"ok": True}

    @app.post("/v2/users/{uid}/upload_part_finish")
    async def part_finish(uid: str, request: Request):
        return {"code": 0}

    @app.post("/v2/users/{uid}/files")
    async def files(uid: str, request: Request):
        body = await request.json()
        if "file_data" in body:
            # 直传通道：测试环境模拟 bot 域名恒 400，验证自动降级分片
            STATE.setdefault("simple", []).append({"file_type": body.get("file_type"),
                                                   "name": body.get("file_name")})
            return __import__("fastapi").responses.JSONResponse(
                {"message": "富媒体文件下载失败", "code": 40093007}, status_code=400)
        STATE["merges"].append(body)
        return {"file_info": f"FIC{len(STATE['merges'])}", "raw_url": "http://fake/raw", "ttl": 86400}

    @app.get("/dl/{key}")
    async def dl(key: str):
        if key not in DL_FILES:
            return Response(status_code=404)
        data, mime, name = DL_FILES[key]
        STATE["downloads"].append(key)
        return Response(
            content=data, media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return app


def make_config(keys_path: str) -> str:
    return f"""
server: {{ host: 127.0.0.1, port: {PORT} }}
auth: {{ keys_file: "{keys_path}" }}
limits: {{ per_source_per_minute: 120, per_target_per_minute: 60, per_target_per_day: 1000 }}
dedup: {{ enabled: false }}
dispatch: {{ poll_interval: 0.1, concurrency: 4 }}
qq: {{ app_id: "T", client_secret: "S", api_base: "http://127.0.0.1:{QQ_PORT}" }}
ingest: {{ max_ws_clients: 8, max_ws_per_source: 2, ws_keepalive: 0.5, ws_send_timeout: 5, sse_keepalive: 0.5, sse_buffer: 64 }}
uploads: {{ chunk_size: 2097152, ttl: 3600 }}

targets:
  - id: qq:default
    type: qq
    user_openid: "u1"

routes:
  - source: ["*"]
    targets: [qq:default]
"""


def build_multipart(fields: dict, file_field: str, filename: str, mime: str, data: bytes) -> tuple[bytes, str]:
    """手工构造 multipart/form-data；字段值可为 str（UTF-8）或 bytes（原样，GBK 场景）。"""
    boundary = "----notiboundary" + secrets.token_hex(8)
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode("ascii")
        out += (v.encode("utf-8") if isinstance(v, str) else v) + b"\r\n"
    out += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("ascii")
    out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode("ascii")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


async def wait_up(url: str, tries: int = 80) -> bool:
    async with httpx.AsyncClient(timeout=1) as c:
        for _ in range(tries):
            try:
                await c.get(url)
                return True
            except Exception:
                await asyncio.sleep(0.25)
    return False


async def wait_until(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return bool(cond())


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="noti-recv-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(f"{KEY_ID} {SECRET} kind=hmac source=recv-test\n", encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(make_config(str(keys_path).replace("\\", "/")), encoding="utf-8")
    os.environ["NOTI_DB"] = str(tmp / "test.db")

    from noti_gateway.config import load_config
    from noti_gateway.server import create_app

    app = create_app(load_config(str(cfg_path)))
    servers = [
        uvicorn.Server(uvicorn.Config(build_mock_qq(), host="127.0.0.1", port=QQ_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")),
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    base = f"http://127.0.0.1:{PORT}"
    if not await wait_up(f"{base}/_health"):
        print("FAIL: 网关未启动")
        for t in tasks:
            t.cancel()
        return 1

    failures: list[str] = []

    def check(cond: bool, name: str) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    def make_zip() -> bytes:
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "二进制回转验证\n" + "x" * 200)
            z.writestr("data.bin", bytes(range(256)) * 8)
        return buf.getvalue()

    zip_bytes = make_zip()

    async with httpx.AsyncClient(timeout=30) as c:
        async def wait_files(n: int):
            return await wait_until(lambda: len(STATE["merges"]) >= n)

        print("\n[1] multipart /n：zip 文件字段（二进制回转）")
        body, ctype = build_multipart(
            {"title": "multipart 投递", "body": "zip 附件说明"}, "file", "release.zip", "application/zip", zip_bytes
        )
        r = await c.post(f"{base}/n/recv", content=body,
                         headers={**_sign("POST", "/n/recv", body), "content-type": ctype})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code}: {r.text[:120]})")
        check(await wait_files(1), "分片上传完成")
        check(STATE["preps"][0].get("file_type") == 4 and STATE["preps"][0].get("file_name") == "release.zip",
              "prepare file_type=4、文件名一致")
        check(STATE["cos"].get(("up1", 0)) == zip_bytes, "COS 字节与原 zip 完全一致（二进制回转）")
        await wait_until(lambda: len(STATE["messages"]) >= 2)
        medias = [m for m in STATE["messages"] if m.get("msg_type") == 7]
        check(len(medias) >= 1, "QQ 收到 msg_type=7 媒体消息")

        print("\n[2] multipart GBK 文本字段")
        n_before = len(STATE["merges"])
        gbk_title = "中文标题测试".encode("gbk")
        body, ctype = build_multipart({"title": gbk_title, "body": "GBK 正文".encode("gbk")},
                                      "file", "g.txt", "text/plain", b"hello")
        r = await c.post(f"{base}/n/recv", content=body,
                         headers={**_sign("POST", "/n/recv", body), "content-type": ctype})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        await wait_files(n_before + 1)
        texts = [m for m in STATE["messages"] if m.get("msg_type") == 0]
        check(any("中文标题测试" in str(m.get("content", "")) for m in texts), "GBK 标题正确解码")

        print("\n[3] /f 裸二进制 + send=1")
        n_before = len(STATE["merges"])
        r = await c.post(f"{base}/f/recv?name=blob.bin&mime=application/octet-stream&send=1"
                         f"&title=%E8%A3%B8%E4%BA%8C%E8%BF%9B%E5%88%B6&wait=1",
                         content=zip_bytes[:1000], headers=_sign("POST", "/f/recv", zip_bytes[:1000]))
        check(r.status_code == 200 and r.json().get("ok") is True, f"同步回执 ok (实际 {r.status_code}: {r.text[:120]})")
        check(await wait_files(n_before + 1), "QQ 侧完成上传")

        print("\n[4] /f 仅落盘返回 ref，再 /n 引用")
        r = await c.post(f"{base}/f/recv?name=stage.zip&mime=application/zip",
                         content=zip_bytes, headers=_sign("POST", "/f/recv", zip_bytes))
        ref = r.json().get("ref")
        check(r.status_code == 200 and r.json().get("ok") is True and bool(ref), f"返回 ref (实际 {r.text[:120]})")
        n_before = len(STATE["merges"])
        r = await c.post(f"{base}/n/recv", content=json.dumps(
            {"title": "ref 引用投递", "file": {"ref": ref}}, ensure_ascii=False).encode(),
            headers={**_sign("POST", "/n/recv", json.dumps(
                {"title": "ref 引用投递", "file": {"ref": ref}}, ensure_ascii=False).encode()),
                "content-type": "application/json"})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        check(await wait_files(n_before + 1), "分片上传完成")
        up_n = f"up{n_before + 1}"
        check(STATE["cos"].get((up_n, 0)) == zip_bytes, "ref 引用字节一致")

        print("\n[5] /n file.url 网关代下载")
        n_before = len(STATE["merges"])
        r = await c.post(f"{base}/n/recv", content=json.dumps(
            {"title": "url 代下载", "file": {"url": f"{QQ_BASE}/dl/doc1"}}, ensure_ascii=False).encode("utf-8"),
            headers={**_sign("POST", "/n/recv", json.dumps(
                {"title": "url 代下载", "file": {"url": f"{QQ_BASE}/dl/doc1"}}, ensure_ascii=False).encode("utf-8")),
                "content-type": "application/json"})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code}: {r.text[:150]})")
        check(await wait_files(n_before + 1), "分片上传完成")
        prep = STATE["preps"][-1]
        data, _, name = DL_FILES["doc1"]
        check(prep.get("file_name") == name, f"Content-Disposition 文件名生效 ({prep.get('file_name')})")
        up_n = f"up{n_before + 1}"
        check(STATE["cos"].get((up_n, 0)) == data, "下载字节一致")

        print("\n[6] url 下载失败 -> 400")
        r = await c.post(f"{base}/n/recv", content=json.dumps(
            {"title": "坏 url", "file": {"url": f"{QQ_BASE}/dl/nope"}}, ensure_ascii=False).encode("utf-8"),
            headers={**_sign("POST", "/n/recv", json.dumps(
                {"title": "坏 url", "file": {"url": f"{QQ_BASE}/dl/nope"}}, ensure_ascii=False).encode("utf-8")),
                "content-type": "application/json"})
        check(r.status_code == 400 and "下载失败" in r.text, f"400 带错误信息 (实际 {r.status_code}: {r.text[:120]})")

        print("\n[7] WS 上行 file.url")
        from _hmac import sig_query

        n_preps = len(STATE["preps"])
        ws_query = urlencode(sig_query("GET", "/ws", SECRET, key_id=KEY_ID))
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws?route=recv&{ws_query}") as ws:
            await ws.send(json.dumps({"title": "WS url", "file": {"url": f"{QQ_BASE}/dl/doc1"}}, ensure_ascii=False))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        check(reply.get("ok") is True or "event_id" in reply, f"WS 回执 (实际 {str(reply)[:120]})")
        check(await wait_until(lambda: len(STATE["preps"]) >= n_preps + 1), "url 经 WS 亦触发代下载上传")

        print("\n[8] markdown + keyboard（msg_type=2 组合，不重复发文本）")
        async def post_obj(obj: dict, path: str = "/n/recv"):
            body_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            return await c.post(f"{base}{path}", content=body_bytes,
                                headers={**_sign("POST", path, body_bytes),
                                         "content-type": "application/json"})

        n_msg = len(STATE["messages"])
        r = await post_obj({"title": "md标题", "markdown": {"content": "**hello md**"},
                            "keyboard": {"rows": [[{"id": "b1", "render_data": {"label": "按钮"},
                                                    "action": {"type": 2, "data": "x"}}]]}})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code}: {r.text[:120]})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n_msg + 1), "恰好一条消息")
        msg = STATE["messages"][-1]
        check(msg.get("msg_type") == 2, "msg_type=2")
        check(msg.get("markdown", {}).get("content") == "**hello md**", "markdown.content 原样")
        check(msg.get("keyboard", {}).get("content", {}).get("rows", [[]])[0][0].get("id") == "b1",
              "keyboard rows 自动包 content")
        check(all(m.get("msg_type") == 2 for m in STATE["messages"][n_msg:]), "未额外发送文本消息")

        print("\n[9] markdown 字符串简写 + 模板 msg_type=3")
        n_msg = len(STATE["messages"])
        r = await post_obj({"markdown": "string md"})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n_msg + 1), "投递完成")
        check(STATE["messages"][-1].get("markdown", {}).get("content") == "string md", "字符串转 content")
        n_msg = len(STATE["messages"])
        r = await post_obj({"markdown": {"template_id": "TID9", "params": [{"key": "k", "values": ["v"]}]}})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n_msg + 1), "投递完成")
        check(STATE["messages"][-1].get("msg_type") == 3, "template_id -> msg_type=3")

        print("\n[10] ark 透传（msg_type=4）")
        n_msg = len(STATE["messages"])
        r = await post_obj({"ark": {"template_id": 23, "kv": [{"key": "k", "values": ["v"]}]}})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n_msg + 1), "投递完成")
        msg = STATE["messages"][-1]
        check(msg.get("msg_type") == 4 and msg.get("ark", {}).get("template_id") == 23, "ark 原样")

        print("\n[11] raw_qq 透传（被动 msg_id/msg_seq 保留）")
        n_msg = len(STATE["messages"])
        r = await post_obj({"raw_qq": {"msg_type": 0, "content": "raw-pass",
                                       "msg_id": "PASSIVE_ID", "msg_seq": 3}})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n_msg + 1), "投递完成")
        msg = STATE["messages"][-1]
        check(msg.get("content") == "raw-pass" and msg.get("msg_id") == "PASSIVE_ID"
              and msg.get("msg_seq") == 3, "载荷原样（被动字段不丢）")

        print("\n[12] raw_qq 非法 msg_type -> 400")
        r = await post_obj({"raw_qq": {"msg_type": 7, "media": {"file_info": "x"}}})
        check(r.status_code == 400, f"400 (实际 {r.status_code}: {r.text[:120]})")

        print("\n[13] video/audio 富媒体（file_type=2/3）")
        n_preps = len(STATE["preps"])
        r = await post_obj({"title": "影音", "video": {"b64": base64.b64encode(b"VID0" * 16).decode(),
                                                       "mime": "video/mp4", "name": "v.mp4"},
                            "audio": {"b64": base64.b64encode(b"AUD0" * 16).decode(),
                                      "mime": "audio/mpeg", "name": "a.mp3"}})
        check(r.status_code in (200, 202), f"受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["preps"]) >= n_preps + 2), "两个媒体上传完成")
        types = sorted(pp["file_type"] for pp in STATE["preps"][n_preps:])
        check(types == [2, 3], f"file_type=2 与 3 (实际 {types})")

    for s in servers:
        s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)

    print("\n" + "=" * 54)
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
