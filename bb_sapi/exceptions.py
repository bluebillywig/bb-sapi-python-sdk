"""Exceptions raised by the BB SAPI Python SDK."""
from __future__ import annotations


class SapiError(Exception):
    """Base exception for all SAPI SDK errors."""


class SapiHTTPError(SapiError):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, url: str = "") -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code}: {message} (url={url})")


class SapiClientError(SapiHTTPError):
    """4xx response."""


class SapiServerError(SapiHTTPError):
    """5xx response."""


class SapiAuthError(SapiHTTPError):
    """Authentication failure (401 / 403 / missing permissions)."""


class SapiNotFoundError(SapiClientError):
    """404 – entity not found."""


class SapiAnalyticsError(SapiError):
    """Analytics API returned an error body."""
