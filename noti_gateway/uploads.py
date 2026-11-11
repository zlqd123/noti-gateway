"""分片上传收件（/u/*）：客户端把大文件分片推给网关，通知里用 ref 引用。

流程：
    POST /u/init                {name, mime?, kind: image|file, size}
        -> {upload_id, chunk_size, chunks}
    POST /u/{upload_id}/part/{index}   （原始字节，HMAC 对原始体签名）
        -> {ok, received, total}
    POST /u/{upload_id}/complete
        -> {ok, ref, name, mime, kind, size}

之后 POST /n/{source} 的 image/file 字段用 {"ref": <ref>} 引用；
小件可走内联 {"b64": ...}，两条路最终都汇到 QQ 富媒体发送。

存储布局（dir 下）：
    <id>.meta.json   元数据（name/mime/kind/size/chunks/created）
    <id>.p00000      分片（complete 后删除）
    <id>.bin         合并后的完整文件
过期（ttl）在每次 init/complete 时清理。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .config import UploadsCfg

_SAFE_NAME = re.compile(r"[^\w.\-\u4e00-\u9fa5 ()]+")


def safe_name(name: str, default: str = "file.bin") -> str:
    """文件名只保留无害字符，防路径/头注入（QQ 侧也要求 file_name）。"""
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("_", name).strip(" .")
    return name[:120] or default


class UploadError(ValueError):
    """客户端错误（HTTP 层按 status 映射，默认 400）。"""

    status = 400


class UploadNotFound(UploadError):
    """upload_id 不存在 / 已过期 / 文件缺失。"""

    status = 404


class UploadStore:
    def __init__(self, cfg: UploadsCfg, base_dir: str | Path | None = None) -> None:
        base = Path(cfg.dir) if cfg.dir else (Path(base_dir or ".") / "uploads")
        self.dir = base
        self.ttl = int(cfg.ttl)
        self.chunk_size = int(cfg.chunk_size)
        self.max_size = int(cfg.max_size)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 内部 ----------------

    def _meta_path(self, upload_id: str) -> Path:
        return self.dir / f"{upload_id}.meta.json"

    def _bin_path(self, upload_id: str) -> Path:
        return self.dir / f"{upload_id}.bin"

    def _part_path(self, upload_id: str, index: int) -> Path:
        return self.dir / f"{upload_id}.p{index:05d}"

    @staticmethod
    def _check_id(upload_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
            raise UploadError("upload_id 不合法")
        return upload_id

    def _load_meta(self, upload_id: str) -> dict[str, Any]:
        self._check_id(upload_id)
        p = self._meta_path(upload_id)
        if not p.exists():
            raise UploadNotFound("upload_id 不存在或已过期")
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            raise UploadNotFound("upload_id 元数据损坏") from None

    def _save_meta(self, meta: dict[str, Any]) -> None:
        self._meta_path(meta["id"]).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

    def cleanup(self) -> int:
        """删除过期上传件（含半成品）。返回删除的上传件数。"""
        now = time.time()
        removed = 0
        for p in self.dir.glob("*.meta.json"):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                if now - float(meta.get("created", 0)) <= self.ttl:
                    continue
            except Exception:
                pass
            upload_id = p.name[: -len(".meta.json")]
            self._remove(upload_id)
            removed += 1
        return removed

    def _remove(self, upload_id: str) -> None:
        for p in self.dir.glob(f"{upload_id}.*"):
            try:
                p.unlink()
            except OSError:
                pass

    # ---------------- 对外 ----------------

    def init(self, name: str, mime: str, kind: str, size: int) -> dict[str, Any]:
        self.cleanup()
        kind = (kind or "").strip().lower() or "image"
        if kind not in ("image", "file"):
            raise UploadError('kind 必须是 "image" 或 "file"')
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise UploadError("size 必须是整数") from None
        if size <= 0:
            raise UploadError("size 必须大于 0")
        if size > self.max_size:
            raise UploadError(f"size 超过上限 {self.max_size} 字节")
        chunks = (size + self.chunk_size - 1) // self.chunk_size
        meta = {
            "id": secrets.token_hex(16),
            "name": safe_name(name, "image.bin" if kind == "image" else "file.bin"),
            "mime": (mime or "").strip().lower() or (
                "image/jpeg" if kind == "image" else "application/octet-stream"
            ),
            "kind": kind,
            "size": size,
            "chunk_size": self.chunk_size,
            "chunks": chunks,
            "received": 0,
            "parts": [],
            "completed": False,
            "created": time.time(),
        }
        self._save_meta(meta)
        return {
            "upload_id": meta["id"],
            "chunk_size": self.chunk_size,
            "chunks": chunks,
            "ttl": self.ttl,
        }

    def put_part(self, upload_id: str, index: int, data: bytes) -> dict[str, Any]:
        meta = self._load_meta(upload_id)
        if meta.get("completed"):
            raise UploadError("upload 已 complete，不能再传分片")
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise UploadError("index 必须是整数") from None
        chunks = int(meta["chunks"])
        if not (0 <= index < chunks):
            raise UploadError(f"index 必须在 [0, {chunks})")
        if index in meta["parts"]:
            raise UploadError(f"分片 {index} 已上传过")
        if not data:
            raise UploadError("分片内容为空")
        last = chunks - 1
        expect = int(meta["chunk_size"]) if index != last else (
            int(meta["size"]) - int(meta["chunk_size"]) * last
        )
        if len(data) != expect:
            raise UploadError(f"分片 {index} 应为 {expect} 字节，实际 {len(data)}")
        self._part_path(upload_id, index).write_bytes(data)
        meta["parts"] = sorted(meta["parts"] + [index])
        meta["received"] = int(meta.get("received", 0)) + len(data)
        self._save_meta(meta)
        return {"ok": True, "received": meta["received"], "total": meta["size"]}

    def complete(self, upload_id: str) -> dict[str, Any]:
        meta = self._load_meta(upload_id)
        if meta.get("completed"):
            return self._ref_dict(meta)
        chunks = int(meta["chunks"])
        if sorted(meta["parts"]) != list(range(chunks)):
            missing = [i for i in range(chunks) if i not in meta["parts"]]
            raise UploadError(f"分片不完整，缺 {missing}")
        out = self._bin_path(upload_id)
        with out.open("wb") as f:
            for i in range(chunks):
                f.write(self._part_path(upload_id, i).read_bytes())
        total = out.stat().st_size
        if total != int(meta["size"]):
            out.unlink(missing_ok=True)
            raise UploadError(f"合并后 {total} 字节与声明 {meta['size']} 不符")
        for i in range(chunks):
            self._part_path(upload_id, i).unlink(missing_ok=True)
        meta["completed"] = True
        self._save_meta(meta)
        return self._ref_dict(meta)

    def _ref_dict(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "ref": meta["id"],
            "name": meta["name"],
            "mime": meta["mime"],
            "kind": meta["kind"],
            "size": meta["size"],
        }

    def load(self, ref: str) -> tuple[bytes, dict[str, Any]]:
        """取回完整文件：返回 (字节, 元数据)。引用无效抛 UploadError/UploadNotFound。"""
        meta = self._load_meta(ref)
        if not meta.get("completed"):
            raise UploadError("upload 尚未 complete")
        if time.time() - float(meta.get("created", 0)) > self.ttl:
            self._remove(ref)
            raise UploadNotFound("upload 已过期")
        p = self._bin_path(ref)
        if not p.exists():
            raise UploadNotFound("upload 文件缺失")
        return p.read_bytes(), meta


# 全局实例：server 启动时 configure；targets 发送时 get()
_store: UploadStore | None = None


def configure(cfg: UploadsCfg, base_dir: str | Path | None = None) -> UploadStore:
    global _store
    _store = UploadStore(cfg, base_dir)
    return _store


def get() -> UploadStore:
    if _store is None:
        raise UploadError("上传存储未初始化")
    return _store
