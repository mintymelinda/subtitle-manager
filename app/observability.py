"""Structured, secret-safe application logging."""
import json
import logging
import re
import time
from contextlib import contextmanager
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
        for key in ("event", "job_id", "candidate_id", "media_path", "phase", "status", "error_type",
                     "error_code", "retryable", "http_status", "error_message", "provider_error_detail",
                     "provider_count", "result_count", "accepted_count", "filtered_count",
                     "filter_reason", "language", "file_count", "file_name", "file_id",
                     "match_score", "match_reasons", "moviehash_match", "download_count",
                     "candidate_limit", "selected_count", "remaining_count", "log_level", "stage", "elapsed_ms",
                     "bytes_processed", "file_size_bytes", "stream_index", "stream_count", "tool",
                     "database_operation", "batch_size", "operation_count", "queue_depth"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False)


def configure_logging(level: str = "INFO"):
    root = logging.getLogger()
    if not any(getattr(handler, "_subtitle_json", False) for handler in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._subtitle_json = True
        root.addHandler(handler)
    normalized = str(level or "INFO").upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        normalized = "INFO"
    root.setLevel(getattr(logging, normalized))


logger = logging.getLogger("subtitle_manager")


def safe_file_size(path):
    """Return a file size for telemetry when available, without affecting processing."""
    try:
        return path.stat().st_size
    except OSError:
        return None


@contextmanager
def timed_stage(stage: str, **context):
    """Log elapsed time for one processing stage without changing its behavior."""
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.info(
            "stage_timing",
            extra={
                "event": "stage_timing",
                "stage": stage,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                **context,
            },
        )
