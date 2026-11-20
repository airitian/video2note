"""结构化日志：JSON 一行式输出。

旧形态下日志全靠 print 与各模块自带的 logging.getLogger("uvicorn.error")，
格式不一、无法被日志采集系统按字段检索。这里提供统一入口：

    from .log import get_logger
    log = get_logger("v2n.access")
    log.info("GET /api/tasks -> 200 (12.3ms)")

每行一个 JSON 对象：ts / level / logger / msg（异常时附 exc）。
不接管 uvicorn 自身的日志配置，只给应用自己的结构化通道用。
"""
from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象的 formatter。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """取（并按需配置）一个输出 JSON 行到 stderr 的 logger。"""
    logger = logging.getLogger(name)
    if not getattr(logger, "_v2n_configured", False):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger._v2n_configured = True  # type: ignore[attr-defined]
    return logger
