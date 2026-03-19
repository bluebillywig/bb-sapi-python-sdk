"""
Main SAPI client for Blue Billywig.

Usage::

    from bb_sapi import SapiClient

    client = SapiClient(
        base_url="https://mypublication.bbvms.com",
        shared_secret="490-55c491d354cfefb9b4d26cf22fbdd0a1",
    )

    # Analytics
    body = client.analytics.views("mediaclip", "2026-01-01", "2026-03-31")

    # Generic entity operations
    clip = client.get("mediaclip", "12345")
    results = client.list("mediaclip", limit=10, sort="createddate DESC")
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

from bb_sapi.auth import HotpAuth
from bb_sapi.entities.analytics import Analytics
from bb_sapi.entities.lineitem import LineItem
from bb_sapi.entities.mediaclip import MediaClip
from bb_sapi.exceptions import (
    SapiAnalyticsError,
    SapiAuthError,
    SapiClientError,
    SapiError,
    SapiHTTPError,
    SapiNotFoundError,
    SapiServerError,
)

# Analytics base path (separate subdomain/path from regular SAPI)
_ANALYTICS_PATH = "/sapi/analytics"
_JWT_TTL = 3300  # re-fetch JWT ~55 minutes before 1-hour expiry


class SapiClient:
    """
    Synchronous Blue Billywig SAPI client.

    Args:
        base_url:      Publication base URL, e.g. ``"https://mypub.bbvms.com"``.
        shared_secret: HOTP shared secret in ``"{id}-{hex_secret}"`` format.
        timeout:       HTTP request timeout in seconds (default 30).
        session:       Optional :class:`requests.Session` (e.g. for testing).
    """

    def __init__(
        self,
        base_url: str,
        shared_secret: str,
        *,
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = HotpAuth(shared_secret)
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # JWT cache
        self._jwt: Optional[str] = None
        self._jwt_fetched_at: float = 0.0

        # Sub-clients
        self.analytics = Analytics(self)
        self.mediaclip = MediaClip(self)
        self.lineitem = LineItem(self)

    # ------------------------------------------------------------------
    # Generic entity operations
    # ------------------------------------------------------------------

    def get(
        self,
        entity: str,
        entity_id: str | int,
        *,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Fetch a single entity by ID."""
        return self._sapi_request("GET", f"/sapi/{entity}/{entity_id}", params=params)

    def list(
        self,
        entity: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort: Optional[str] = None,
        filters: Optional[dict[str, str]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        List entities with optional filtering, sorting, and pagination.

        Args:
            entity:  Entity type, e.g. ``"mediaclip"``.
            limit:   Maximum number of results.
            offset:  Number of results to skip.
            sort:    Sort expression, e.g. ``"createddate DESC"``.
            filters: Additional filter key-value pairs.
            params:  Raw extra query parameters (merged last).
        """
        p: dict[str, str] = {}
        if limit is not None:
            p["limit"] = str(limit)
        if offset is not None:
            p["offset"] = str(offset)
        if sort is not None:
            p["sort"] = sort
        if filters:
            p.update(filters)
        if params:
            p.update(params)
        return self._sapi_request("GET", f"/sapi/{entity}", params=p)

    def search(
        self,
        query: str,
        *,
        entity_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[str] = None,
        filters: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Full-text search across entity types.

        Args:
            query:       Solr query string, e.g. ``"*:*"`` or ``"id:12345"``.
            entity_type: Restrict to a single entity class (e.g. ``"MediaClip"``).
            limit:       Maximum results.
            offset:      Results to skip.
            fields:      Comma-separated fields to return.
            filters:     Additional filter key-value pairs.
        """
        p: dict[str, str] = {"q": query}
        if entity_type:
            p["className[]"] = entity_type
        if limit is not None:
            p["limit"] = str(limit)
        if offset is not None:
            p["offset"] = str(offset)
        if fields:
            p["fields"] = fields
        if filters:
            p.update(filters)
        return self._sapi_request("GET", "/papi/search", params=p)

    def create(
        self,
        entity: str,
        data: dict[str, Any],
        *,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Create a new entity (POST to ``/sapi/{entity}/new``)."""
        return self._sapi_request("POST", f"/sapi/{entity}/new", json=data, params=params)

    def update(
        self,
        entity: str,
        entity_id: str | int,
        data: dict[str, Any],
        *,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Update an existing entity (PUT to ``/sapi/{entity}/{id}``)."""
        return self._sapi_request("PUT", f"/sapi/{entity}/{entity_id}", json=data, params=params)

    def delete(
        self,
        entity: str,
        entity_id: str | int,
        *,
        purge: bool = False,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Delete an entity.

        Args:
            purge: For mediaclips, permanently purge instead of soft-delete.
        """
        p: dict[str, str] = {}
        if purge:
            p["purge"] = "true"
        if params:
            p.update(params)
        return self._sapi_request("DELETE", f"/sapi/{entity}/{entity_id}", params=p or None)

    def action(
        self,
        entity: str,
        entity_id: str | int,
        action_name: str,
        *,
        method: str = "GET",
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Call an action on a specific entity (``/sapi/{entity}/{id}/{action}``)."""
        return self._sapi_request(
            method, f"/sapi/{entity}/{entity_id}/{action_name}", json=data, params=params
        )

    def entity_action(
        self,
        entity: str,
        action_name: str,
        *,
        method: str = "GET",
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Call an entity-level action without ID (``/sapi/{entity}/{action}``)."""
        return self._sapi_request(
            method, f"/sapi/{entity}/{action_name}", json=data, params=params
        )

    def versions(self, entity: str, entity_id: str | int) -> list[dict[str, Any]]:
        """
        Return the version history for any entity that supports it, newest first.

        Most SAPI entities support ``/sapi/{entity}/{id}/versions``, including
        ``lineitem``, ``mediaclip``, ``playout``, ``player``, etc.

        Each entry contains at least ``id``, ``date``, and ``isLatest``.
        """
        body = self.action(entity, entity_id, "versions")
        if isinstance(body, list):
            return body
        items = body.get("items")
        if items is None:
            raise SapiError(
                f"Unexpected versions response for {entity}/{entity_id}: "
                f"expected list or dict with 'items', got {type(body).__name__} "
                f"with keys: {list(body.keys()) if isinstance(body, dict) else '?'}"
            )
        return items

    def raw_request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Arbitrary SAPI request for any endpoint not covered above."""
        return self._sapi_request(method, path, json=data, params=params)

    # ------------------------------------------------------------------
    # JWT / Bearer auth (for /v1 endpoints such as ad-stats)
    # ------------------------------------------------------------------

    def get_jwt(self, *, force_refresh: bool = False) -> str:
        """
        Obtain a JWT for Bearer-auth on ``/v1`` endpoints (e.g. ad-stats API).

        The token is cached and auto-refreshed after ~55 minutes.
        """
        if not force_refresh and self._jwt and (time.time() - self._jwt_fetched_at) < _JWT_TTL:
            return self._jwt

        body = self._sapi_request("GET", "/sapi/auth/token")
        token = body.get("token") or body.get("jwt") or body.get("access_token")
        if not token:
            raise SapiAuthError(
                0,
                f"Unexpected JWT response — expected 'token'/'jwt'/'access_token' key, "
                f"got keys: {list(body.keys())}",
            )
        self._jwt = token
        self._jwt_fetched_at = time.time()
        return self._jwt

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sapi_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, str]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Send an authenticated request to the regular SAPI and return parsed JSON."""
        url = self._base_url + path
        headers = self._auth.headers()
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json,
            headers=headers,
            timeout=self._timeout,
        )
        return self._handle_response(resp)

    def _analytics_request(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        """
        Send an authenticated request to the analytics API.

        The analytics endpoint is ``{base_url}/sapi/analytics/{path}``.
        Errors are signalled by ``{"success": false, "error": "..."}`` bodies.
        """
        url = f"{self._base_url}{_ANALYTICS_PATH}/{path}"
        headers = self._auth.headers()
        resp = self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=self._timeout,
        )
        body = self._handle_response(resp)
        if not body.get("success", True):
            raise SapiAnalyticsError(body.get("error", repr(body)))
        return body

    @staticmethod
    def _handle_response(resp: requests.Response) -> dict[str, Any]:
        status = resp.status_code
        url = resp.url

        if status in (401, 403):
            raise SapiAuthError(status, "Unauthorized" if status == 401 else "Forbidden", url)
        if status == 404:
            raise SapiNotFoundError(status, "Not Found", url)
        if 400 <= status < 500:
            try:
                msg = resp.json().get("error") or resp.text
            except ValueError:
                msg = resp.text
            raise SapiClientError(status, msg, url)
        if status >= 500:
            try:
                msg = resp.json().get("error") or resp.text
            except ValueError:
                msg = resp.text
            raise SapiServerError(status, msg, url)

        try:
            return resp.json()
        except ValueError as exc:
            content_type = resp.headers.get("Content-Type", "unknown")
            raise SapiHTTPError(
                status,
                f"Non-JSON response (Content-Type: {content_type}): {resp.text[:500]}",
                url,
            ) from exc
