"""Structured logging with request/job correlation IDs.

- Console (human) + rotating file (persistent) handlers.
- `correlation_id` contextvar so every log line can be tied to a request or job.
- A redacting formatter to keep secrets out of logs.
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Optional

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

_REDACT_PATTERNS = [
    re.compile(r"(?i)(authorization|api[_-]?key|token|password|secret)\s*[=:]\s*\S+"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+"),
]


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id.get()
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for pat in _REDACT_PATTERNS:
            text = pat.sub(lambda m: m.group(0).split("=")[0] + "=***", text)
        return text


_configured = False


def setup_logging(
    *,
    level: int = logging.INFO,
    log_dir: Optional[Path] = None,
    log_file: str = "spectre.log",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    force: bool = False,
) -> None:
    """Configure the root logger. Idempotent unless `force=True`."""
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = "%(asctime)s %(levelname)s [%(name)s] [%(correlation_id)s] %(message)s"
    ctx_filter = _ContextFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_RedactingFormatter(fmt))
    console.addFilter(ctx_filter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / log_file, maxBytes=max_bytes,
            backupCount=backup_count, encoding="utf-8",
        )
        file_handler.setFormatter(_RedactingFormatter(fmt))
        file_handler.addFilter(ctx_filter)
        root.addHandler(file_handler)

    # Quiet down noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "urllib3", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def new_correlation_id(prefix: str = "") -> str:
    """Generate and set a short correlation id; returns it."""
    import uuid
    cid = (prefix + "-" if prefix else "") + uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


def set_correlation_id(value: str) -> None:
    correlation_id.set(value or "-")
