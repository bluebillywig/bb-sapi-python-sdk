"""Blue Billywig SAPI Python SDK."""
from bb_sapi.client import SapiClient
from bb_sapi.exceptions import (
    SapiAnalyticsError,
    SapiAuthError,
    SapiClientError,
    SapiError,
    SapiHTTPError,
    SapiNotFoundError,
    SapiServerError,
)

__all__ = [
    "SapiClient",
    "SapiError",
    "SapiHTTPError",
    "SapiClientError",
    "SapiServerError",
    "SapiAuthError",
    "SapiNotFoundError",
    "SapiAnalyticsError",
]

__version__ = "0.1.0"
