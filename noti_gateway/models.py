"""核心数据模型：统一事件 + 发送结果 + Target 协议。"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

LEVELS = ("debug", "info", "warn", "error")


@dataclass
class Notification:
    """所有入站方式（curl/HTTP/WS/Webhook）最终都收敛成这个结构。"""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    source: str = "default"
    level: str = "info"
    title: str = ""
    body: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    # 富媒体：{"b64": ..., "mime": ..., "name": ...}（内联）/ {"url": ...}（网关代下载）
    # 或 {"ref": <upload_id>, ...}（/u/* 分片上传后引用）
    image: dict[str, Any] | None = None
    file: dict[str, Any] | None = None
    video: dict[str, Any] | None = None   # file_type=2（mp4）
    audio: dict[str, Any] | None = None   # file_type=3（mp3/aac/wav/silk）
    # QQ bot 全消息类型（仅 QQ 目标消费；markdown+keyboard 组合成 msg_type=2，
    # markdown 含 template_id 时为 msg_type=3 模板，ark 为 msg_type=4 原样透传，
    # raw_qq 为完整 QQ 载荷透传——msg_id/msg_seq 等被动字段由调用方自带）
    markdown: dict[str, Any] | None = None
    keyboard: dict[str, Any] | None = None
    ark: dict[str, Any] | None = None
    raw_qq: dict[str, Any] | None = None
    route: str = "default"
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            self.level = "info"
        if not self.title and not self.body and not self.image and not self.file \
                and not self.video and not self.audio and not self.markdown \
                and not self.keyboard and not self.ark and not self.raw_qq:
            self.title = self.source
        if not self.title:
            self.title = self.body[:60]
        if self.raw_qq and not self.title:
            # raw_qq 不渲染标题，但去重键需要它——合成唯一值避免风暴折叠
            self.title = f"qq-raw-{uuid.uuid4().hex[:8]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def dedup_key(self) -> str:
        """聚合去重键：同源同级别同标题视为同一条风暴。"""
        return f"{self.source}|{self.level}|{self.title}"


@dataclass
class SendResult:
    """一次投递结果。retryable 决定失败后重试还是进死信。"""

    ok: bool
    code: int = 0
    retryable: bool = False
    detail: str = ""
    target: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class Target(Protocol):
    """出站适配器统一接口。新增平台只需实现这三个方法。"""

    id: str

    async def send(self, n: Notification) -> SendResult: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...


def ok(target: str, detail: str = "", elapsed_ms: int = 0) -> SendResult:
    return SendResult(True, 0, False, detail, target, elapsed_ms)


def fail(
    target: str,
    code: int = 0,
    detail: str = "",
    retryable: bool = False,
    elapsed_ms: int = 0,
) -> SendResult:
    return SendResult(False, code, retryable, detail, target, elapsed_ms)
