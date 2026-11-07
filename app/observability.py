"""Structured, secret-safe application logging."""
import json
import logging
import re
from datetime import datetime, timezone

_SECRET = re.compile(r"(?i)(api[_-]?key|password|token|authorization|cookie)(\s*[=:]\s*)([^\s,;]+)")
_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|password|token|authorization|cookie)")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*")


def redact(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _SECRET_NAME.search(str(key)) else redact(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = _SECRET.sub(r"\1\2[REDACTED]", value)
        return _BEARER.sub("Bearer [REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "job_id", "candidate_id", "media_path", "phase", "status", "error_type"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False)


def configure_logging():
    root = logging.getLogger()
    if not any(getattr(handler, "_subtitle_json", False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._subtitle_json = True
        root.addHandler(handler)
    root.setLevel(logging.INFO)


logger = logging.getLogger("subtitle_manager")
