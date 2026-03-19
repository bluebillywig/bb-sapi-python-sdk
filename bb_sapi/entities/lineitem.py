"""
LineItem entity client for Blue Billywig SAPI.

Includes version history support for resolving which creative was active
during a specific time period.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from bb_sapi.client import SapiClient


class LineItem:
    """
    LineItem API access including creative version history.

    Usage::

        # Get current lineitem
        li = client.lineitem.get("starcasino_preroll")

        # Creative that ran during a report period
        creatives = client.lineitem.creatives_for_period(
            "starcasino_preroll", "2026-01-01", "2026-03-31"
        )
    """

    def __init__(self, client: SapiClient) -> None:
        self._client = client

    def get(self, name: str, *, version_id: Optional[str] = None) -> dict[str, Any]:
        """
        Fetch a lineitem by name.

        Args:
            name:       Lineitem name (slug), e.g. ``"starcasino_preroll"``.
            version_id: If provided, return the lineitem state at that version.
        """
        params = {"versionId": version_id} if version_id else None
        return self._client.get("lineitem", name, params=params)

    def versions(self, name: str) -> list[dict[str, Any]]:
        """
        Return the version history for a lineitem, newest first.

        Each entry contains at least ``id``, ``date``, and ``isLatest``.
        """
        body = self._client.action("lineitem", name, "versions")
        return body if isinstance(body, list) else body.get("items", [])

    def creatives_for_period(
        self,
        name: str,
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """
        Determine which creative(s) were active for a lineitem during a date range.

        Fetches all versions, filters to those whose active window overlaps the
        report period, and extracts the creative MediaClip ID from ``vast_url``.

        Returns a list of dicts::

            [
                {
                    "version_id": "...",
                    "date": "YYYY-MM-DD",
                    "creative_id": "12345",   # MediaClip ID, or None if not BB-hosted
                    "vast_url": "https://...",
                },
                ...
            ]

        Notes:
            Creative identity is only resolvable when the creative is BB-hosted
            (``vast_url`` pointing to the OVP). For externally served creatives
            the lineitem name is known but the creative cannot be identified.

            When a lineitem's creative changed mid-period, multiple entries are
            returned — one per distinct creative that was active.
        """
        all_versions = self.versions(name)
        if not all_versions:
            return []

        # Versions are newest-first; sort oldest-first to assign active windows
        sorted_versions = sorted(all_versions, key=lambda v: v.get("date", ""))

        result: list[dict[str, Any]] = []
        seen_creative_ids: set[str | None] = set()

        for i, ver in enumerate(sorted_versions):
            ver_date = ver.get("date", "")
            # Active window: from ver_date to the next version's date (exclusive)
            next_date = sorted_versions[i + 1]["date"] if i + 1 < len(sorted_versions) else None

            # Overlap check: version active window vs [from_date, to_date]
            if next_date and next_date <= from_date:
                continue  # version expired before our period
            if ver_date > to_date:
                continue  # version only active after our period

            # Fetch the lineitem state at this version to get the vast_url
            ver_data = self.get(name, version_id=ver["id"])
            vast_url: Optional[str] = ver_data.get("vast_url") or ver_data.get("vastUrl")
            creative_id = _extract_creative_id(vast_url) if vast_url else None

            if creative_id not in seen_creative_ids:
                seen_creative_ids.add(creative_id)
                result.append(
                    {
                        "version_id": ver["id"],
                        "date": ver_date,
                        "creative_id": creative_id,
                        "vast_url": vast_url,
                    }
                )

        return result


def _extract_creative_id(vast_url: str) -> Optional[str]:
    """
    Extract the MediaClip ID from a BB-hosted VAST URL.

    Expected format: ``https://{pub}.bbvms.com/mediaclip/{id}.xml?output=vast``

    Returns ``None`` for externally hosted creatives.
    """
    try:
        # Strip query string
        path = vast_url.split("?")[0]
        if "/mediaclip/" not in path:
            return None
        segment = path.split("/mediaclip/")[-1]
        return segment.split(".")[0]
    except Exception:
        return None
