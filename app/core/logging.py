import json
import logging
import sys
from datetime import UTC, datetime

from app.core.request_context import request_id_var

_HANDLER_FLAG = "_news_json_handler"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # An explicit `extra={"request_id": ...}` wins: the error handler runs after the
            # request-ID middleware has already reset the context variable.
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_FLAG, True)
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's loggers through the root JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
