"""Structured logging with automatic credential redaction.

Every record carries: timestamp, severity, component, and (when relevant)
job / assignment / student identifiers. A global filter strips anything
shaped like an API key, OAuth token, or client secret before a record is
written — keys are only ever represented by their fingerprint.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

RESERVED = logging.LogRecord("r", 0, "p", 1, "m", None, None).reserved_keys if hasattr(logging.LogRecord, "reserved_keys") else {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
}

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),            # legacy Gemini keys
    # The CURRENT Gemini key format: "AQ." + 50 base64url characters. It was
    # absent here, so every log line and every stored provider error that echoed
    # one of these keys was written out in full. A format-shaped pattern is the
    # only defence that does not need the key list to hand.
    re.compile(r"AQ\.[0-9A-Za-z_\-]{20,}"),            # current Gemini keys
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]{10,}"),         # Google OAuth client secrets
    re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"),          # Google access tokens
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),             # Google refresh tokens
    re.compile(r"(?i)(api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s*[=:]\s*\S+"),
]


def redact(message: str) -> str:
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    return message


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": record.name,
            "event": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            for key, value in context.items():
                if key in RESERVED:
                    continue
                entry[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            entry["error"] = record.exc_info[0].__name__
            entry["error_detail"] = redact(str(record.exc_info[1]))
        return redact(json.dumps(entry, ensure_ascii=False, default=str))


_lock = threading.Lock()
_configured = False


def setup_logging(level: int = logging.INFO) -> Path:
    """Configure root logging once; returns the log file path."""
    global _configured
    with _lock:
        if _configured:
            return DATA_DIR / "logs" / "app.log"

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "logs").mkdir(exist_ok=True)
        log_path = DATA_DIR / "logs" / "app.log"

        root = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)

        # Noisy third-party loggers stay quiet.
        for name in ("urllib3", "httpx", "google_genai", "google_auth_httplib2"):
            logging.getLogger(name).setLevel(logging.WARNING)

        _configured = True
        return log_path


def reset_logging() -> None:
    """Close and detach every logging handler so the log file is released.

    Windows refuses to delete a file a stream still holds open (WinError 32),
    so a factory reset must release ``data/logs/app.log`` before removing the
    logs directory — otherwise the reset silently leaves log data behind.
    Call :func:`setup_logging` afterwards to re-attach fresh handlers.
    """
    global _configured
    with _lock:
        root = logging.getLogger()
        for handler in list(root.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:  # noqa: BLE001 — closing must never fail the reset
                pass
            root.removeHandler(handler)
        _configured = False


def log_event(component: str, event: str, level: int = logging.INFO, **context) -> None:
    """Component-structured log with arbitrary context fields."""
    logger = logging.getLogger(component)
    logger.log(level, redact(event), extra={"context": context})
