import json
import logging
import re
from datetime import UTC, datetime

from src.api.context import request_id_var
from src.config import settings

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(r"authorization|api[-_]?key|token|secret|password|signature|cookie", re.IGNORECASE)
_SENSITIVE_TEXT = [
    (re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), f"Bearer {REDACTED}"),
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), f"bot{REDACTED}"),
    (re.compile(r"\b((?:api[-_]?key|signature|\w*token|password|secret)=)[^&\s'\"]+", re.IGNORECASE), rf"\1{REDACTED}"),
]
# Attributes every LogRecord has; anything else on a record came from `extra=`
_RECORD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "request_id"}


def sanitize_text(text: str) -> str:
    for pattern, replacement in _SENSITIVE_TEXT:
        text = pattern.sub(replacement, text)
    return text


def sanitize(value):
    if isinstance(value, dict):
        return {k: REDACTED if _SENSITIVE_KEY.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS})
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(sanitize(entry), default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_text(super().format(record))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if settings.LOG_FORMAT == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.LOG_LEVEL)

    # uvicorn installs its own handlers before importing the app; route them through ours instead
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # RequestContextMiddleware writes the access log
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
