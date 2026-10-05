"""Port definitions (the seams that let backends be swapped without touching callers).

Callers depend on these Protocols only. Concrete drivers live in
`app/adapters/` and are selected by `app/adapters/registry.py` from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterator, Optional, Protocol, runtime_checkable


# ---- Queue ---------------------------------------------------------------

QUEUE_QUEUED = "queued"
QUEUE_LEASED = "leased"
QUEUE_RUNNING = "running"
QUEUE_SUCCEEDED = "succeeded"
QUEUE_FAILED = "failed"
QUEUE_CANCELED = "canceled"

LANE_HEAVY = "heavy"
LANE_LIGHT = "light"


@dataclass
class JobLease:
    job_id: str
    lane: str
    owner: str
    attempt: int
    lease_expires_at: float
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class QueuePort(Protocol):
    def enqueue(
        self, kind: str, payload: dict[str, Any], *,
        dedupe_key: Optional[str] = None, priority: int = 5,
        lane: str = LANE_HEAVY, max_attempts: int = 3,
    ) -> str: ...

    def claim(self, lanes: list[str], owner: str, lease_seconds: int) -> Optional[JobLease]: ...

    def heartbeat(self, lease: JobLease, lease_seconds: int) -> bool: ...

    def complete(self, lease: JobLease, result: Optional[dict[str, Any]] = None) -> None: ...

    def fail(self, lease: JobLease, error: str, *, retryable: bool = False) -> None: ...

    def requeue_expired(self, now: float) -> int: ...

    def cancel(self, job_id: str) -> bool: ...


# ---- Storage -------------------------------------------------------------

@dataclass
class StoredBlob:
    key: str
    size: int
    content_type: str


@dataclass
class BlobMeta:
    key: str
    size: int
    exists: bool


@runtime_checkable
class StoragePort(Protocol):
    def put(self, key: str, data: BinaryIO, *, content_type: str = "application/octet-stream") -> StoredBlob: ...
    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> StoredBlob: ...
    def get_bytes(self, key: str) -> bytes: ...
    def open(self, key: str) -> BinaryIO: ...
    def local_path(self, key: str, *, scratch: Optional[Any] = None) -> str: ...
    def url(self, key: str, *, ttl: int = 3600, download: bool = False) -> str: ...
    def delete(self, key: str) -> None: ...
    def stat(self, key: str) -> BlobMeta: ...


# ---- Events --------------------------------------------------------------

@runtime_checkable
class EventPort(Protocol):
    def publish(self, channel: str, event: dict[str, Any]) -> None: ...
    def subscribe(self, channel: str, last_seq: int = 0) -> Iterator[dict[str, Any]]: ...


# ---- LLM / Transcriber (drivers) -----------------------------------------

@dataclass
class ChatMessage:
    role: str
    content: str


@runtime_checkable
class LLMPort(Protocol):
    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = True) -> str: ...


@runtime_checkable
class TranscriberPort(Protocol):
    def transcribe(self, video_path: str, work_dir: str) -> Any: ...
