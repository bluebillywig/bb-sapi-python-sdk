"""Tests for SapiClient."""
import pytest
import responses as resp_lib

from bb_sapi import SapiClient
from bb_sapi.exceptions import (
    SapiAuthError,
    SapiClientError,
    SapiNotFoundError,
    SapiServerError,
)

BASE_URL = "https://test.bbvms.com"
SECRET = "490-deadbeef"


def make_client() -> SapiClient:
    return SapiClient(BASE_URL, SECRET, timeout=5)


@resp_lib.activate
def test_get_success():
    resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip/123", json={"id": 123, "title": "Test"})
    client = make_client()
    result = client.get("mediaclip", "123")
    assert result["id"] == 123


@resp_lib.activate
def test_get_not_found():
    resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip/999", status=404, json={"error": "Not found"})
    client = make_client()
    with pytest.raises(SapiNotFoundError):
        client.get("mediaclip", "999")


@resp_lib.activate
def test_auth_error():
    resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip/1", status=403)
    client = make_client()
    with pytest.raises(SapiAuthError):
        client.get("mediaclip", "1")


@resp_lib.activate
def test_server_error():
    resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip/1", status=500, json={"error": "oops"})
    client = make_client()
    with pytest.raises(SapiServerError):
        client.get("mediaclip", "1")


@resp_lib.activate
def test_client_error():
    resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip/1", status=400, json={"error": "bad"})
    client = make_client()
    with pytest.raises(SapiClientError):
        client.get("mediaclip", "1")


@resp_lib.activate
def test_list_passes_params():
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/sapi/mediaclip",
        json={"items": [], "total": 0},
    )
    client = make_client()
    client.list("mediaclip", limit=5, sort="createddate DESC")
    req = resp_lib.calls[0].request
    assert "limit=5" in req.url
    assert "sort=createddate" in req.url


@resp_lib.activate
def test_rpctoken_header_sent():
    resp_lib.add(resp_lib.GET, f"{BASE_URL}/sapi/mediaclip/1", json={"id": 1})
    client = make_client()
    client.get("mediaclip", "1")
    req = resp_lib.calls[0].request
    assert "rpctoken" in req.headers


@resp_lib.activate
def test_analytics_request():
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/sapi/analytics/views/mediaclip",
        json={"success": True, "total": 42, "facets": {}},
    )
    client = make_client()
    result = client.analytics.views("mediaclip", "2026-01-01", "2026-03-31")
    assert result["total"] == 42


@resp_lib.activate
def test_analytics_error_body():
    from bb_sapi.exceptions import SapiAnalyticsError
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/sapi/analytics/views/mediaclip",
        json={"success": False, "error": "unknown entity"},
    )
    client = make_client()
    with pytest.raises(SapiAnalyticsError, match="unknown entity"):
        client.analytics.views("mediaclip", "2026-01-01", "2026-03-31")
