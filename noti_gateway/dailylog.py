"""按天分文件的日志 Handler：log/noti-gateway-YYYY-MM-DD.log，跨零点自动切换。

不依赖 logging.handlers 的轮转后缀命名，文件名直接带日期，
与 ~/.zcode/log/ 下的日志风格保持一致。
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import IO


class DailyFileHandler(logging.Handler):
    def __init__(self, directory: str, prefix: str = "noti-gateway",
                 encoding: str = "utf-8") -> None:
        super().__init__()
        self.directory = directory
        self.prefix = prefix
        self.encoding = encoding
        self._day: datetime.date | None = None
        self._fh: IO[str] | None = None
        os.makedirs(directory, exist_ok=True)

    def _ensure(self) -> None:
        today = datetime.date.today()
        if today == self._day and self._fh is not None:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, f"{self.prefix}-{today:%Y-%m-%d}.log")
        self._fh = open(path, "a", encoding=self.encoding)
        self._day = today

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure()
            assert self._fh is not None
            self._fh.write(self.format(record) + "\n")
            self._fh.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        super().close()
