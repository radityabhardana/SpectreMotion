"""Driver registry: pick concrete adapters from config.

Callers (services, pipeline, worker, web) call `get_queue()` / `get_storage()`
etc. and never import a concrete adapter. To swap to Redis/S3, add a new class
and a branch here — no call-site changes.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app import config

log = logging.getLogger("spectre.registry")


@lru_cache(maxsize=1)
def get_queue():
    if config.QUEUE_DRIVER == "sqlite":
        from app.adapters.queue_sqlite import SqliteQueue
        return SqliteQueue()
    raise NotImplementedError(f"queue driver belum didukung: {config.QUEUE_DRIVER}")


@lru_cache(maxsize=1)
def get_storage():
    if config.STORAGE_DRIVER == "local":
        from app.adapters.storage_local import LocalStorage
        return LocalStorage()
    raise NotImplementedError(f"storage driver belum didukung: {config.STORAGE_DRIVER}")


@lru_cache(maxsize=1)
def get_events():
    from app.adapters.events_memory import EventBus
    return EventBus()


@lru_cache(maxsize=1)
def get_transcriber():
    if config.TRANSCRIBER_DRIVER == "faster_whisper":
        from app.adapters.transcriber_whisper import WhisperTranscriberAdapter
        return WhisperTranscriberAdapter()
    if config.TRANSCRIBER_DRIVER == "fake":
        from app.adapters.fakes import FakeTranscriber
        return FakeTranscriber()
    raise NotImplementedError(f"transcriber driver belum didukung: {config.TRANSCRIBER_DRIVER}")


@lru_cache(maxsize=1)
def get_llm():
    if config.LLM_DRIVER == "openai":
        from app.adapters.llm_openai import OpenAICompatLLM
        return OpenAICompatLLM()
    if config.LLM_DRIVER == "fake":
        from app.adapters.fakes import FakeLLM
        return FakeLLM()
    raise NotImplementedError(f"llm driver belum didukung: {config.LLM_DRIVER}")


def reset() -> None:
    """Clear cached drivers (used by tests after config monkeypatching)."""
    get_queue.cache_clear()
    get_storage.cache_clear()
    get_events.cache_clear()
    get_transcriber.cache_clear()
    get_llm.cache_clear()
