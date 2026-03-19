"""
Analytics API client for Blue Billywig SAPI.

Endpoint base: /sapi/analytics/
Documentation: docs/AD_ANALYTICS.md in the sapi-mcp-server repo.

Key facts about the analytics data model
-----------------------------------------
- percentageViewed_int = highestTo / clipDuration × 100
  (highest playback position reached, NOT the exit point)
- exactPercentageViewed facet → distribution of this field across sessions
  → sum of counts where value >= X gives cumulative reach (viewcount≥X%)
  → this differs from Advanced Reports, which uses [X-20, X] dropout buckets
- lineitemInits tracks BB-managed lineitems per view-session (always recorded,
  regardless of creative host). Useful for per-video ad impressions when the
  ad-stats API's entityFilters do NOT support MediaClip filtering.
- vastQuartiles tracks VAST ad quartile events (25/50/75/100%) per session.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from bb_sapi.client import SapiClient


class Analytics:
    """
    Fluent analytics API.

    Usage::

        client.analytics.views(
            "mediaclip",
            from_date="2026-01-01",
            to_date="2026-03-31",
            facets=["eid", "uid"],
            facetconfig={"eid": {"limit": 10}, "uid": {"limit": 0, "metric": "unique"}},
        )
    """

    def __init__(self, client: SapiClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    def views(
        self,
        entity_type: str,
        from_date: str,
        to_date: str,
        *,
        entity_id: Optional[str] = None,
        facets: Optional[list[str]] = None,
        facetconfig: Optional[dict[str, Any]] = None,
        granularity: Optional[str] = None,
        rangefacet: Optional[list[str]] = None,
        extra_params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Aggregate view statistics with optional faceted breakdown.

        Args:
            entity_type: "mediaclip", "publication", etc.
            from_date:   Start date "YYYY-MM-DD".
            to_date:     End date "YYYY-MM-DD".
            entity_id:   Scope to a single entity (e.g. a specific video ID).
            facets:      Dimensions to facet on. Common values:
                         "eid", "title", "uid", "avgViewTime", "completed",
                         "exactPercentageViewed", "lineitemInits", "vastQuartiles",
                         "country", "region", "city", "domain", "deviceType",
                         "osName", "browserName".
            facetconfig: Per-facet options::

                            {
                                "uid":   {"limit": 0, "metric": "unique"},
                                "eid":   {"limit": 50},
                                "lineitemInits": {"limit": 1,
                                                  "query": json.dumps("my_lineitem")},
                            }

            granularity: "auto"|"minute"|"hour"|"day"|"week"|"month"|"year".
            rangefacet:  Facets to include time-series data for.
            extra_params: Raw additional query-string parameters.

        Returns:
            Parsed response body dict with keys: ``total``, ``facets``, ``otype``.

        Notes:
            ``facets.unique_uid`` is returned when ``uid`` is requested with
            ``metric: "unique"``.  ``facets.vastQuartiles`` contains a list of
            ``{value, count}`` dicts for 25/50/75/100.
        """
        path = f"views/{entity_type}"
        if entity_id:
            path += f"/{entity_id}"

        params: dict[str, str] = {
            "fromDate": from_date,
            "toDate": to_date,
        }
        if facets:
            params["facet"] = json.dumps(facets)
        if facetconfig:
            params["facetconfig"] = json.dumps(facetconfig)
        if granularity:
            params["granularity"] = granularity
        if rangefacet:
            params["rangefacet"] = json.dumps(rangefacet)
        if extra_params:
            params.update(extra_params)

        return self._client._analytics_request(path, params)

    # ------------------------------------------------------------------
    # range
    # ------------------------------------------------------------------

    def range(
        self,
        entity_type: str,
        from_date: str,
        to_date: str,
        *,
        entity_id: Optional[str] = None,
        granularity: str = "day",
        facets: Optional[list[str]] = None,
        facetconfig: Optional[dict[str, Any]] = None,
        extra_params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Time-series view counts over a date range.

        Granularity constraints (enforced by the API):
        - minute: < 7 days
        - hour:   < 30 days
        - day:    > 1 day
        - week:   > 7 days
        - month:  > 30 days
        - year:   > 365 days

        Returns:
            Dict with ``total`` and ``items`` (list of ``{datetime, total}``).
        """
        path = f"range/{entity_type}"
        if entity_id:
            path += f"/{entity_id}"

        params: dict[str, str] = {
            "fromDate": from_date,
            "toDate": to_date,
            "granularity": granularity,
        }
        if facets:
            params["facet"] = json.dumps(facets)
        if facetconfig:
            params["facetconfig"] = json.dumps(facetconfig)
        if extra_params:
            params.update(extra_params)

        return self._client._analytics_request(path, params)

    # ------------------------------------------------------------------
    # inits
    # ------------------------------------------------------------------

    def inits(
        self,
        entity_type: str,
        from_date: str,
        to_date: str,
        *,
        entity_id: Optional[str] = None,
        granularity: Optional[str] = None,
        extra_params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Player load (init) counts.

        Useful for computing play rate: views / inits.

        Returns:
            Dict with ``total`` (total inits) and optional time-series ``items``.
        """
        path = f"inits/{entity_type}"
        if entity_id:
            path += f"/{entity_id}"

        params: dict[str, str] = {
            "fromDate": from_date,
            "toDate": to_date,
        }
        if granularity:
            params["granularity"] = granularity
        if extra_params:
            params.update(extra_params)

        return self._client._analytics_request(path, params)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def unique_viewers(
        self, entity_type: str, from_date: str, to_date: str, entity_id: Optional[str] = None
    ) -> int:
        """Return total unique viewer count (unique uid) for a period."""
        body = self.views(
            entity_type, from_date, to_date,
            entity_id=entity_id,
            facets=["uid"],
            facetconfig={"uid": {"limit": 0, "metric": "unique"}},
        )
        return body.get("facets", {}).get("unique_uid", 0)

    def top_videos(
        self, from_date: str, to_date: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        Return top N mediaclips by view count, sorted descending.

        Returns list of ``{id, views}`` dicts.
        """
        body = self.views(
            "mediaclip", from_date, to_date,
            facets=["eid"],
            facetconfig={"eid": {"limit": limit}},
        )
        return [{"id": item["value"], "views": item["count"]}
                for item in body.get("facets", {}).get("eid", [])]

    def viewcount_reach(
        self,
        entity_id: str,
        from_date: str,
        to_date: str,
        *,
        thresholds: tuple[int, ...] = (20, 40, 60, 80, 95),
    ) -> dict[int, int]:
        """
        Compute cumulative viewcount reach (≥X%) for a specific video.

        Returns dict mapping threshold → number of sessions that reached ≥threshold%.

        This uses ``exactPercentageViewed`` (= highestTo / duration × 100),
        which tracks the highest position reached in the session (not exit point).
        A viewer who skips to 80% counts as having reached 80%, even if they
        did not watch linearly.

        Advanced Reports uses a different interpretation (dropout buckets per
        20% segment); this method returns cumulative reach.
        """
        body = self.views(
            "mediaclip", from_date, to_date,
            entity_id=entity_id,
            facets=["exactPercentageViewed"],
            facetconfig={"exactPercentageViewed": {"limit": 101}},
        )
        pct_dist = {
            item["value"]: item["count"]
            for item in body.get("facets", {}).get("exactPercentageViewed", [])
        }
        return {
            t: sum(count for pct, count in pct_dist.items() if pct >= t)
            for t in thresholds
        }

    def ad_stats_per_video(
        self, entity_id: str, from_date: str, to_date: str
    ) -> dict[str, Any]:
        """
        Return per-video ad metrics via the analytics API.

        Because the bb-backend ad-stats API silently ignores MediaClip
        entityFilters (architectural gap — no Redis key schema for MediaClip),
        per-video impressions and unique reach must come from analytics instead.

        Returns:
            {
                "impressions": int,         # sum of lineitemInits counts
                "lineitems": {name: count}, # per-lineitem session counts
                "vastQuartiles": {          # VAST IAB quartile completions
                    "25": int, "50": int, "75": int, "100": int
                },
            }

        Note: lineitemInits is always tracked regardless of creative host.
        vastQuartiles requires VAST events to fire; may be lower than impressions
        if init events are occasionally missed.
        """
        body = self.views(
            "mediaclip", from_date, to_date,
            entity_id=entity_id,
            facets=["lineitemInits", "vastQuartiles"],
            facetconfig={
                "lineitemInits": {"limit": 20},
                "vastQuartiles": {"limit": 4},
            },
        )
        facets = body.get("facets", {})

        lineitems = {
            item["value"]: item["count"]
            for item in facets.get("lineitemInits", [])
        }

        vq_raw = facets.get("vastQuartiles", [])
        if isinstance(vq_raw, list):
            vq = {str(item["value"]): item["count"] for item in vq_raw}
        elif isinstance(vq_raw, dict):
            vq = {str(k): v for k, v in vq_raw.items()}
        else:
            vq = {}

        return {
            "impressions": sum(lineitems.values()),
            "lineitems": lineitems,
            "vastQuartiles": {q: vq.get(q, 0) for q in ("25", "50", "75", "100")},
        }

    def unique_ad_reach(
        self, entity_id: str, lineitem: str, from_date: str, to_date: str
    ) -> int:
        """
        Unique viewers of a specific video who were shown a specific lineitem.

        Filters analytics by entity_id + lineitem name, counts distinct uid.
        Only works for BB-managed lineitems (always tracked regardless of
        whether the creative is BB-hosted or external — but creative identity
        is only resolvable for BB-hosted VAST/VPAID/SIMID creatives).

        When a video has multiple lineitems, call this per lineitem and take
        the max (not sum — sessions overlap across lineitems).
        """
        body = self.views(
            "mediaclip", from_date, to_date,
            entity_id=entity_id,
            facets=["lineitemInits", "uid"],
            facetconfig={
                "lineitemInits": {"limit": 1, "query": json.dumps(lineitem)},
                "uid": {"limit": 0, "metric": "unique"},
            },
        )
        return body.get("facets", {}).get("unique_uid", 0)
