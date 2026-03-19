"""
HOTP-based authentication for Blue Billywig SAPI.

The SAPI uses HMAC-SHA1 with a 120-second time window.
Shared secret format: "{id}-{hex_secret}"
Header sent: rpctoken: {id}-{hmac_sha1_hex}
"""
from __future__ import annotations

import hashlib
import hmac
import struct
import time


class HotpAuth:
    """Generates HOTP rpctoken headers for SAPI authentication."""

    WINDOW_SECONDS = 120

    def __init__(self, shared_secret: str) -> None:
        """
        Args:
            shared_secret: SAPI shared secret in "{id}-{hex_secret}" format.
                           Obtain from OVP account settings → API Keys → Show Secret.
        """
        if "-" not in shared_secret:
            raise ValueError(
                "shared_secret must be in '{id}-{hex_secret}' format, "
                f"e.g. '490-55c491d354cfefb9b4d26cf22fbdd0a1'. Got: {shared_secret!r}"
            )
        self._id, self._secret = shared_secret.split("-", 1)

    def token(self, timestamp: float | None = None) -> str:
        """Generate an rpctoken value valid for the current 120-second window."""
        ts = timestamp if timestamp is not None else time.time()
        counter = int(ts / self.WINDOW_SECONDS)
        msg = struct.pack(">Q", counter)
        h = hmac.new(self._secret.encode("utf-8"), msg, hashlib.sha1).hexdigest()
        return f"{self._id}-{h}"

    def headers(self, timestamp: float | None = None) -> dict[str, str]:
        """Return the authentication header dict to merge into a request."""
        return {"rpctoken": self.token(timestamp)}
