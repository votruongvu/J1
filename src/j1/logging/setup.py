import json
import logging
import sys
from datetime import datetime, timezone

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_RESERVED_LOGRECORD_KEYS = frozenset({
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
})


class JsonFormatter(logging.Formatter):
    """Formats log records as one JSON object per line.

    Standard fields: `ts`, `level`, `logger`, `msg`. Extra fields passed via
    `logger.info("...", extra={"key": value})` are merged in if JSON-encodable;
    others are coerced to strings.
    """

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = str(value)
        return json.dumps(out, default=str)


def configure_logging(
    level: str | int = "INFO",
    *,
    json_output: bool = False,
) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
