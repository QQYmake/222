"""结构化日志：全项目统一日志接口。

禁止记录：上游 API Key、网关 API Key、完整身份/记忆/用户消息正文。
"""
from __future__ import annotations

import logging
import os
import sys

_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_configured = False


def _ensure_configured() -> None:
    global _configured
    if _configured:
        return
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """返回一个配置好的 Logger 实例。"""
    _ensure_configured()
    return logging.getLogger(name)
