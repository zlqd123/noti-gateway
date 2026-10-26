"""noti-gateway：统一通知网关。

入站：HTTP / curl / Webhook / WebSocket 上行
出站：QQ 官方机器人 / 通用 Webhook / 出站 WebSocket 连接池

启动：uvicorn "noti_gateway.server:get_app" --factory --host 0.0.0.0 --port 100086
"""

from .config import Config, load_config
from .models import Notification, SendResult

__all__ = ["Config", "load_config", "Notification", "SendResult", "create_app"]
__version__ = "0.1.0"


def create_app(*args, **kwargs):
    """延迟导入，避免仅使用模型/配置时也要求存在可用的 config.yaml。"""
    from .server import create_app as _create

    return _create(*args, **kwargs)
