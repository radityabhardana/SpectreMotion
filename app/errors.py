"""Typed exception hierarchy + HTTP mapping.

Every layer raises a typed error; the API layer translates it to an HTTP
status exactly once. This keeps routers thin and makes error handling
predictable and testable.
"""

from __future__ import annotations

from typing import Any, Optional


class SpectreError(Exception):
    """Base class for all application errors."""

    code = "error"
    http_status = 500

    def __init__(self, message: str, *, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "detail": self.detail}


# ---- 400-class ------------------------------------------------------------

class ValidationError(SpectreError):
    code = "validation_error"
    http_status = 400


class NotFoundError(SpectreError):
    code = "not_found"
    http_status = 404


class ConflictError(SpectreError):
    code = "conflict"
    http_status = 409


class RateLimitedError(SpectreError):
    code = "rate_limited"
    http_status = 429


# ---- 401/403 --------------------------------------------------------------

class AuthError(SpectreError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(SpectreError):
    code = "forbidden"
    http_status = 403


class OwnershipError(NotFoundError):
    """Cross-tenant access. Deliberately maps to 404 so existence never leaks."""

    code = "not_found"


# ---- 4xx upload / ingest --------------------------------------------------

class UnsupportedMediaError(ValidationError):
    code = "unsupported_media"


class PayloadTooLargeError(SpectreError):
    code = "payload_too_large"
    http_status = 413


# ---- 5xx domain -----------------------------------------------------------

class PipelineError(SpectreError):
    code = "pipeline_error"
    http_status = 500


class TranscriptionError(PipelineError):
    code = "transcription_error"


class AnalyzerError(PipelineError):
    code = "analyzer_error"


class RenderError(PipelineError):
    code = "render_error"


class IngestError(PipelineError):
    code = "ingest_error"


class BackendError(SpectreError):
    """Infrastructure failure (DB/queue/storage down)."""

    code = "backend_error"
    http_status = 503
