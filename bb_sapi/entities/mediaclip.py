"""
MediaClip entity client for Blue Billywig SAPI.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from bb_sapi.client import SapiClient


class MediaClip:
    """
    MediaClip API access.

    Usage::

        clip = client.mediaclip.get(12345)
        clips = client.mediaclip.list(limit=20, sort="createddate DESC", status="published")
        client.mediaclip.update(12345, {"title": "New title"})
    """

    def __init__(self, client: SapiClient) -> None:
        self._client = client

    def get(self, clip_id: str | int, *, params: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """Fetch a single MediaClip by ID."""
        return self._client.get("mediaclip", clip_id, params=params)

    def list(
        self,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort: Optional[str] = None,
        status: Optional[str] = None,
        filters: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        List MediaClips with optional filtering and pagination.

        Args:
            status: Filter by status, e.g. ``"published"``, ``"draft"``.
        """
        f: dict[str, str] = {}
        if status:
            f["status"] = status
        if filters:
            f.update(filters)
        return self._client.list(
            "mediaclip",
            limit=limit,
            offset=offset,
            sort=sort,
            filters=f or None,
        )

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new MediaClip."""
        return self._client.create("mediaclip", data)

    def update(self, clip_id: str | int, data: dict[str, Any]) -> dict[str, Any]:
        """Update an existing MediaClip."""
        return self._client.update("mediaclip", clip_id, data)

    def delete(self, clip_id: str | int, *, purge: bool = False) -> dict[str, Any]:
        """
        Delete a MediaClip.

        Args:
            purge: Permanently delete instead of soft-delete.
        """
        return self._client.delete("mediaclip", clip_id, purge=purge)

    def publish(self, clip_id: str | int) -> dict[str, Any]:
        """Publish a MediaClip."""
        return self._client.action("mediaclip", clip_id, "publish", method="PUT")

    def unpublish(self, clip_id: str | int) -> dict[str, Any]:
        """Unpublish a MediaClip."""
        return self._client.action("mediaclip", clip_id, "unpublish", method="PUT")

    def search(
        self,
        query: str,
        *,
        limit: Optional[int] = None,
        fields: Optional[str] = None,
        filters: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Search MediaClips using Solr query syntax."""
        return self._client.search(
            query,
            entity_type="MediaClip",
            limit=limit,
            fields=fields,
            filters=filters,
        )

    def content_clips(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        sort: str = "views DESC",
    ) -> list[dict[str, Any]]:
        """
        Return content MediaClips, excluding ad creatives (``usetype: commercial``).

        Args:
            limit:  Maximum results per page.
            offset: Pagination offset.
            sort:   Sort expression passed as a raw query param to ``/papi/search``.

        Returns:
            List of MediaClip dicts.
        """
        body = self._client.search(
            "NOT usetype:commercial",
            entity_type="MediaClip",
            limit=limit,
            offset=offset,
            filters={"sort": sort},  # papi/search accepts sort as a query param
        )
        return body.get("items", [])
