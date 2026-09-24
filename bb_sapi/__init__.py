"""Blue Billywig SAPI Python SDK."""
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

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
from bb_sapi.upload import MediaType, UploadResult, UploadStatus

__all__ = [
    "MediaType",
    "SapiAnalyticsError",
    "SapiAuthError",
    "SapiClient",
    "SapiClientError",
    "SapiError",
    "SapiHTTPError",
    "SapiNotFoundError",
    "SapiServerError",
    "UploadResult",
    "UploadStatus",
]

# Read from the installed distribution rather than a second hand-maintained
# literal. The release workflow stamps the version into pyproject.toml from the
# git tag and never touches this file, so a literal here would drift from what
# was actually published — which is how the repo ended up on 0.1.0 while PyPI
# served 1.0.0.
try:
    __version__ = _distribution_version("bb-sapi-python-sdk")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0.0.0+unknown"
