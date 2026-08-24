"""Blue Billywig SAPI Python SDK."""
from bb_sapi.client import SapiClient
from bb_sapi.search import Filter, FilterOperator, FilterSet
from bb_sapi.upload import UploadResult
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
    "UploadResult",
    "FilterSet",
    "Filter",
    "FilterOperator",
    "SapiError",
    "SapiHTTPError",
    "SapiClientError",
    "SapiServerError",
    "SapiAuthError",
    "SapiNotFoundError",
    "SapiAnalyticsError",
]

__version__ = "0.2.0"
