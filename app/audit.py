"""Append-only audit log for compliance-relevant actions.

Written as JSONL so it is trivially appendable and inspectable. Never raises
into the caller — auditing must not break the feature it audits.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from app import config

log = logging.getLogger("spectre.audit")

AUDIT_PATH = config.DATA_DIR / "audit.jsonl"


def record(action: str, *, subject: str = "", detail: Optional[dict[str, Any]] = None,
           actor: str = "system") -> None:
    entry = {
        "at": time.time(),
        "actor": actor,
        "action": action,
        "subject": subject,
        "detail": detail or {},
    }
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # never break the caller
        log.warning("Gagal menulis audit log: %s", exc)


def tail(limit: int = 100) -> list[dict[str, Any]]:
    if not AUDIT_PATH.is_file():
        return []
    lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
