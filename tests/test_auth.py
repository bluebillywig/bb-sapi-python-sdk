"""Tests for HotpAuth."""
import struct
import hashlib
import hmac

import pytest

from bb_sapi.auth import HotpAuth


def _expected_token(shared_secret: str, timestamp: float) -> str:
    id_, secret = shared_secret.split("-", 1)
    counter = int(timestamp / 120)
    msg = struct.pack(">Q", counter)
    h = hmac.new(secret.encode(), msg, hashlib.sha1).hexdigest()
    return f"{id_}-{h}"


def test_token_format():
    auth = HotpAuth("490-deadbeef")
    token = auth.token(timestamp=0.0)
    assert token.startswith("490-")
    assert len(token) > 10


def test_token_deterministic():
    auth = HotpAuth("490-deadbeef")
    assert auth.token(timestamp=100.0) == auth.token(timestamp=100.0)


def test_token_matches_reference():
    secret = "490-55c491d354cfefb9b4d26cf22fbdd0a1"
    auth = HotpAuth(secret)
    ts = 1_700_000_000.0
    assert auth.token(timestamp=ts) == _expected_token(secret, ts)


def test_headers():
    auth = HotpAuth("490-deadbeef")
    headers = auth.headers(timestamp=0.0)
    assert "rpctoken" in headers
    assert headers["rpctoken"] == auth.token(timestamp=0.0)


def test_invalid_secret_raises():
    with pytest.raises(ValueError, match="shared_secret must be"):
        HotpAuth("noseparator")


def test_window_boundary():
    auth = HotpAuth("1-secret")
    # Two timestamps in different windows must differ
    t1 = auth.token(timestamp=119.9)
    t2 = auth.token(timestamp=120.0)
    assert t1 != t2
