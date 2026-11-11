"""富媒体 / 文件 / 分片上传 端到端测试。

运行：python tests/test_media.py
不需要真实 QQ 凭证，也不需要联网。

覆盖：
  [1] 内联图片 + 文本（文本一条 + msg_type 7 一条）
  [2] 纯图片不发多余文本
  [3] 内联文件（file_type=4）
  [4] /u/* 分片收件 -> ref 引用 -> QQ 分片通道
  [5] 大文件 12MB 多片拼接（三哈希 prepare / PUT / part_finish / merge）
  [6] 校验与鉴权错误（400/401）+ 简单 /files 表单通道全程不被调用
      （生产 api.bot.qq.com 简单上传稳定 40093007，已废除，上传恒走分片）
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
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试专用密钥（写进临时 keys.conf；真实密钥绝不入库）
KEY_ID = "media-test"
SECRET = "media-test-secret-do-not-use-1111111111111111111111111111"
PORT = 10099        # 网关
QQ_PORT = 10100     # 假 QQ API

QQ_BASE = f"http://127.0.0.1:{QQ_PORT}"

# 假 QQ 侧收到的一切（断言用）
STATE = {
    "messages": [],     # /v2/users/{uid}/messages 的 body
    "simple": [],       # 简单表单上传被调用记录（应恒为空：该通道已废除）
    "merges": [],       # /files JSON 合并（分片通道）
    "preps": [],        # upload_prepare body
    "cos": {},          # 分片字节：(upload_id, part_index) -> bytes
    "cos_ct": {},       # 分片 Content-Type：(upload_id, part_index) -> str
    "part_finish": [],  # upload_part_finish body
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

    @app.get("/_state")
    async def state_ep():
        return {
            "messages": STATE["messages"],
            "simple": STATE["simple"],
            "merges": STATE["merges"],
            "preps": STATE["preps"],
            "cos": {f"{k[0]}/{k[1]}": len(v) for k, v in STATE["cos"].items()},
            "part_finish": STATE["part_finish"],
        }

    @app.post("/app/getAppAccessToken")
    async def token():
        return {"access_token": "FAKE", "expires_in": 7200}

    @app.post("/v2/users/{uid}/messages")
    async def send(uid: str, request: Request):
        body = await request.json()
        STATE["messages"].append(body)
        return {"id": f"MSG{len(STATE['messages'])}"}

    @app.post("/v2/users/{uid}/files")
    async def files(uid: str, request: Request):
        raw = await request.body()
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            body = json.loads(raw)
            STATE["merges"].append(body)
            return {"file_info": f"FIC{len(STATE['merges'])}", "raw_url": "http://fake/raw",
                    "ttl": 86400, "id": f"m{len(STATE['merges'])}"}
        # 简单表单上传已废除（生产稳定 40093007）：记录调用并原样回错误，测试末尾断言为空
        STATE["simple"].append({"ctype": ctype[:60], "bytes": len(raw)})
        return JSONResponse({"message": "富媒体文件下载失败", "code": 40093007}, status_code=400)

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
            # 仿生产：block_size 回字符串（网关 int() 必须兼容）
            "block_size": str(block),
            "parts": [{"index": i, "presigned_url": f"{QQ_BASE}/cos/{up}/{i}"} for i in range(n)],
        }

    @app.put("/cos/{up}/{i}")
    async def cos(up: str, i: int, request: Request):
        raw = await request.body()
        STATE["cos"][(up, i)] = raw
        STATE["cos_ct"][(up, i)] = request.headers.get("content-type", "")
        return {"ok": True}

    @app.post("/v2/users/{uid}/upload_part_finish")
    async def part_finish(uid: str, request: Request):
        STATE["part_finish"].append(await request.json())
        return {"code": 0}

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


async def wait_up(url: str, tries: int = 80) -> bool:
    async with httpx.AsyncClient(timeout=1) as c:
        for _ in range(tries):
            try:
                await c.get(url)
                return True
            except Exception:
                await asyncio.sleep(0.25)
    return False


async def wait_until(cond, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return bool(cond())


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="noti-media-"))
    keys_path = tmp / "keys.conf"
    keys_path.write_text(f"{KEY_ID} {SECRET} kind=hmac source=media-test\n", encoding="utf-8")
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

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    pdf = b"%PDF-1.4\nfake pdf content for test\n" + b"x" * 64

    async def post_json(c: httpx.AsyncClient, path: str, obj: dict, source: str = "media-test"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return await c.post(f"{base}{path}", content=body,
                            headers={**_sign("POST", path, body), "X-Source": source,
                                     "content-type": "application/json"})

    async def upload_chunked(c: httpx.AsyncClient, data: bytes, name: str, mime: str, kind: str) -> str:
        """走 /u/* 分片收件，返回 ref。顺带自检完整性。"""
        r = await post_json(c, "/u/init", {"name": name, "mime": mime, "kind": kind, "size": len(data)})
        info = r.json()
        chunk_size = info["chunk_size"]
        chunks = info["chunks"]
        upload_id = info["upload_id"]
        for i in range(chunks):
            part = data[i * chunk_size:(i + 1) * chunk_size]
            p = f"/u/{upload_id}/part/{i}"
            rr = await c.post(f"{base}{p}", content=part,
                              headers={**_sign("POST", p, part), "content-type": "application/octet-stream"})
            assert rr.status_code == 200, f"part{i} -> {rr.status_code} {rr.text[:120]}"
        r2 = await post_json(c, f"/u/{upload_id}/complete", {})
        return r2.json()["ref"]

    async with httpx.AsyncClient(timeout=30) as c:
        print("\n[1] 内联图片 + 文本")
        obj = {"title": "带图标题", "body": "说明文字",
               "image": {"b64": base64.b64encode(png).decode(), "mime": "image/png", "name": "a.png"}}
        r = await post_json(c, "/n/test", obj)
        check(r.status_code in (200, 202), f"通知受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= 2), "文本 + 媒体共 2 条消息")
        msgs = STATE["messages"]
        texts = [m for m in msgs if m.get("msg_type") == 0]
        medias = [m for m in msgs if m.get("msg_type") == 7]
        check(any("带图标题" in str(m.get("content", "")) for m in texts), "文本消息带标题")
        check(len(medias) >= 1 and medias[0].get("media", {}).get("file_info") == "FIC1",
              "媒体消息 msg_type=7 且带 file_info")
        check(await wait_until(lambda: len(STATE["merges"]) >= 1), "分片通道上传完成")
        p0 = STATE["preps"][0]
        check(p0.get("file_type") == 1 and p0.get("file_name") == "a.png"
              and all(k in p0 for k in ("md5", "sha1", "md5_10m")),
              "prepare file_type=1、文件名与三哈希齐全")
        m0 = STATE["merges"][0]
        check(m0.get("file_type") == 1 and m0.get("file_name") == "a.png"
              and m0.get("srv_send_msg") is False,
              "merge file_type=1、文件名一致")
        check(STATE["cos"].get(("up1", 0)) == png, "上传字节与原图一致")

        print("\n[2] 纯图片不发多余文本")
        n0 = len(msgs)
        obj = {"image": {"b64": base64.b64encode(png).decode(), "mime": "image/png", "name": "b.png"}}
        r = await post_json(c, "/n/test", obj)
        check(r.status_code in (200, 202), f"纯图片通知受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n0 + 1), "媒体消息已发")
        check(await wait_until(lambda: len(STATE["merges"]) >= 2), "上传完成（分片通道）")
        new_msgs = STATE["messages"][n0:]
        check(all(m.get("msg_type") == 7 for m in new_msgs), "只有 msg_type=7，无多余文本")
        check(STATE["merges"][1].get("file_name") == "b.png" and STATE["cos"].get(("up2", 0)) == png,
              "b.png 文件名与字节一致")

        print("\n[3] 内联文件（file_type=4）")
        n0 = len(msgs)
        obj = {"body": "带附件", "file": {"b64": base64.b64encode(pdf).decode(),
                                       "mime": "application/pdf", "name": "report.pdf"}}
        r = await post_json(c, "/n/test", obj)
        check(r.status_code in (200, 202), f"文件通知受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["messages"]) >= n0 + 2), "文本 + 文件媒体 2 条")
        check(await wait_until(lambda: len(STATE["merges"]) >= 3), "上传完成（分片通道）")
        m3 = STATE["merges"][2]
        check(m3.get("file_type") == 4 and m3.get("file_name") == "report.pdf"
              and STATE["preps"][2].get("file_type") == 4,
              "file_type=4、文件名一致")
        check(STATE["cos"].get(("up3", 0)) == pdf, "PDF 字节一致")

        print("\n[4] /u/* 分片收件 -> ref -> QQ 分片通道")
        from noti_gateway.uploads import get as get_store

        data5 = os.urandom(5 * 1024 * 1024)
        ref = await upload_chunked(c, data5, "big.jpg", "image/jpeg", "image")
        loaded, meta = get_store().load(ref)
        check(loaded == data5 and meta["kind"] == "image" and meta["name"] == "big.jpg",
              "分片合并完整性（客户端 5MB / 3 片）")
        n0 = len(msgs)
        r = await post_json(c, "/n/test", {"image": {"ref": ref}})
        check(r.status_code in (200, 202), f"ref 引用通知受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["merges"]) >= 4), "QQ 侧分片上传完成")
        check(await wait_until(lambda: len(STATE["messages"]) >= n0 + 1), "媒体消息已发")
        m4 = STATE["merges"][3]
        check(m4.get("file_name") == "big.jpg" and m4.get("file_type") == 1,
              "merge 文件名与 file_type=1")
        keys4 = sorted(k for k in STATE["cos"] if k[0] == "up4")
        parts4 = [STATE["cos"][k] for k in keys4]
        check(len(parts4) == 2 and b"".join(parts4) == data5,
              f"QQ 侧 2 片拼回 == 原文件 (实际 {len(parts4)} 片)")

        print("\n[5] 大文件 12MB -> QQ 分片多片拼接")
        data12 = os.urandom(12 * 1024 * 1024)
        ref = await upload_chunked(c, data12, "huge.bin", "application/octet-stream", "file")
        n0 = len(msgs)
        r = await post_json(c, "/n/test", {"file": {"ref": ref}})
        check(r.status_code in (200, 202), f"大文件通知受理 (实际 {r.status_code})")
        check(await wait_until(lambda: len(STATE["merges"]) >= 5, timeout=15), "分片通道 merge 完成")
        p5 = STATE["preps"][4]
        check(p5 and all(k in p5 for k in ("md5", "sha1", "md5_10m")),
              "upload_prepare 三哈希齐全（md5/sha1/md5_10m）")
        check(p5.get("file_type") == 4 and p5.get("file_name") == "huge.bin",
              "prepare file_type=4、file_name 正确")
        keys5 = sorted(k for k in STATE["cos"] if k[0] == "up5")
        check([k[1] for k in keys5] == [0, 1, 2], f"3 个分片 PUT (实际 {[k[1] for k in keys5]})")
        check(all(STATE["cos_ct"][k] for k in keys5), "每个 PUT 都带 Content-Type")
        assembled = b"".join(STATE["cos"][k] for k in keys5)
        check(assembled == data12, "分片字节拼回 == 原文件")
        fin5 = [pf for pf in STATE["part_finish"] if pf.get("upload_id") == "up5"]
        check(len(fin5) == 3 and all(
            pf.get("md5") == hashlib.md5(STATE["cos"][("up5", pf["part_index"])]).hexdigest()
            and pf.get("block_size") == len(STATE["cos"][("up5", pf["part_index"])])
            for pf in fin5), "每片 part_finish 带片 md5 与 block_size")
        check(STATE["merges"][4].get("upload_id") == "up5"
              and STATE["merges"][4].get("srv_send_msg") is False
              and STATE["merges"][4].get("file_type") == 4,
              "merge 用 upload_id + srv_send_msg=false + file_type=4")
        check(await wait_until(lambda: any(m.get("msg_type") == 7 for m in STATE["messages"][n0:])),
              "最终发出 msg_type=7 媒体消息")

        print("\n[6] 校验与鉴权")
        r = await post_json(c, "/n/test", {"title": "t", "image": {"b64": "@@not-base64@@", "mime": "image/png"}})
        check(r.status_code == 400, f"坏 base64 -> 400 (实际 {r.status_code})")
        r = await post_json(c, "/n/test", {"title": "t",
                                           "image": {"b64": base64.b64encode(png).decode(), "mime": "application/pdf"}})
        check(r.status_code == 400, f"图片 mime 不支持 -> 400 (实际 {r.status_code})")
        big = base64.b64encode(os.urandom(10 * 1024 * 1024 + 512)).decode()
        r = await post_json(c, "/n/test", {"title": "t", "file": {"b64": big}})
        check(r.status_code == 400 and "ref" in r.text, f"内联超 10MB -> 400 并提示走 ref (实际 {r.status_code})")

        r = await c.get(f"{base}/_receipt/nope")
        check(r.status_code == 401, f"无签名 -> 401 (实际 {r.status_code})")

        r = await post_json(c, "/u/init", {"name": "x", "kind": "image", "size": 0})
        check(r.status_code == 400, f"size=0 -> 400 (实际 {r.status_code})")
        r = await post_json(c, "/u/init", {"name": "x", "kind": "video", "size": 100})
        check(r.status_code == 400, f"kind 非法 -> 400 (实际 {r.status_code})")
        r = await post_json(c, "/u/init", {"name": "x", "kind": "file", "size": 190 * 1024 * 1024 + 1})
        check(r.status_code == 400, f"超上限 -> 400 (实际 {r.status_code})")

        # 分片流程错误路径
        r = await post_json(c, "/u/init", {"name": "y.bin", "kind": "file", "size": 3 * 1024 * 1024})
        uid = r.json()["upload_id"]
        p = f"/u/{uid}/part/0"
        rr = await c.post(f"{base}{p}", content=b"short",
                          headers={**_sign("POST", p, b"short"), "content-type": "application/octet-stream"})
        check(rr.status_code == 400, f"分片大小不符 -> 400 (实际 {rr.status_code})")
        p = f"/u/{uid}/part/9"
        rr = await c.post(f"{base}{p}", content=b"x" * (2 * 1024 * 1024),
                          headers={**_sign("POST", p, b"x" * (2 * 1024 * 1024)),
                                   "content-type": "application/octet-stream"})
        check(rr.status_code == 400, f"越界分片 -> 400 (实际 {rr.status_code})")
        r = await post_json(c, f"/u/{uid}/complete", {})
        check(r.status_code == 400, f"缺片 complete -> 400 (实际 {r.status_code})")
        p = "/u/zzz/part/0"
        rr = await c.post(f"{base}{p}", content=b"",
                          headers={**_sign("POST", p, b""), "content-type": "application/octet-stream"})
        check(rr.status_code in (400, 404), f"非法 upload_id -> 400/404 (实际 {rr.status_code})")

        check(STATE["simple"] == [], "简单 /files 表单通道全程未被调用（生产已废除）")

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
