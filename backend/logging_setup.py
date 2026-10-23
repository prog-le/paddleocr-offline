"""日志：控制台 + 滚动文件 logs/app.log。

任务相关日志统一带 [task:<id>] 前缀，便于 grep 定位推理报错。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_configured = False
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


class _TaskIdFilter(logging.Filter):
    """让 %(task_id)s 始终存在，未绑定时为空串。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        if not hasattr(record, "task_id"):
            record.task_id = ""
        return True


def setup_logging(log_dir: Path, level: str = "INFO", max_bytes: int = 10 * 1024 * 1024,
                  backup_count: int = 5) -> None:
    global _configured
    if _configured:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    fmt = logging.Formatter(LOG_FORMAT)
    flt = _TaskIdFilter()

    file_handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(flt)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(flt)
    root.addHandler(console)

    # 压掉第三方库的噪声日志
    for noisy in ("uvicorn.access", "paddlex", "paddle", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    _configured = True


def get_logger(name: str, task_id: Optional[str] = None) -> logging.LoggerAdapter:
    logger = logging.getLogger(name)
    return logging.LoggerAdapter(logger, {"task_id": task_id or ""})


def task_logger(task_id: str) -> logging.LoggerAdapter:
    """任务日志：自动在消息前加 [task:xxx]，文件名/页码由调用方写入消息。"""
    logger = logging.getLogger("ocr.task")
    adapter = logging.LoggerAdapter(logger, {"task_id": task_id})

    class _Fmt(logging.LoggerAdapter):
        def process(self, msg, kwargs):  # noqa: D102
            return f"[task:{task_id}] {msg}", kwargs

    return _Fmt(logger, {})
