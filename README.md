# bb-sapi-python-sdk

Python SDK for the [Blue Billywig](https://www.bluebillywig.com/) Streaming API (SAPI).

## Features

- HOTP authentication (HMAC-SHA1, 120-second window) — same scheme as the PHP SDK and MCP server
- Synchronous HTTP client built on `requests`
- Full analytics API: views, range, inits, faceted breakdowns
- Convenience helpers: top videos, unique viewers, viewcount reach (≥X%), per-video ad stats
- LineItem version history for resolving which creative ran during a report period
- Generic entity CRUD: `get`, `list`, `search`, `create`, `update`, `delete`, `action`
- Clean exception hierarchy

## Installation

```bash
pip install bb-sapi-python-sdk
```

Or from source:

```bash
git clone https://github.com/bluebillywig/bb-sapi-python-sdk.git
cd bb-sapi-python-sdk
pip install -e ".[dev]"
```

## Quick start

```python
from bb_sapi import SapiClient

client = SapiClient(
    base_url="https://mypublication.bbvms.com",
    shared_secret="490-55c491d354cfefb9b4d26cf22fbdd0a1",
)

# Top 10 videos this month
top = client.analytics.top_videos("2026-01-01", "2026-03-31", limit=10)
for v in top:
    print(v["id"], v["views"])

# Unique viewers for a specific video
unique = client.analytics.unique_viewers("mediaclip", "2026-01-01", "2026-03-31",
                                         entity_id="12345")
print(f"Unique viewers: {unique}")

# Viewcount reach thresholds
reach = client.analytics.viewcount_reach("12345", "2026-01-01", "2026-03-31")
# {20: 1200, 40: 900, 60: 700, 80: 400, 95: 150}

# Per-video ad impressions + VAST quartiles
ad = client.analytics.ad_stats_per_video("12345", "2026-01-01", "2026-03-31")
print(ad["impressions"])          # sum of lineitemInits counts
print(ad["vastQuartiles"]["50"])  # sessions where VAST 50% quartile fired

# Unique ad reach for a specific lineitem
reach = client.analytics.unique_ad_reach("12345", "starcasino_preroll",
                                          "2026-01-01", "2026-03-31")
print(f"Unique viewers who saw the ad: {reach}")
```

## Authentication

The SAPI uses HOTP (HMAC-SHA1) with a 120-second time window.

Obtain your shared secret from your Blue Billywig account:
**Account Settings → API Keys → Show Secret**

Format: `{id}-{hex_secret}` — e.g. `490-55c491d354cfefb9b4d26cf22fbdd0a1`

```python
client = SapiClient(
    base_url="https://mypublication.bbvms.com",
    shared_secret="490-55c491d354cfefb9b4d26cf22fbdd0a1",
)
```

**Never commit your shared secret.** Use environment variables:

```bash
export SAPI_BASE_URL=https://mypublication.bbvms.com
export SAPI_SHARED_SECRET=490-55c491d354cfefb9b4d26cf22fbdd0a1
```

## Analytics

### Views with faceted breakdown

```python
body = client.analytics.views(
    "mediaclip",
    from_date="2026-01-01",
    to_date="2026-03-31",
    facets=["eid", "uid", "country"],
    facetconfig={
        "eid": {"limit": 50},
        "uid": {"limit": 0, "metric": "unique"},
    },
)
# body["total"]          → total view sessions
# body["facets"]["eid"]  → [{value, count}, ...]
# body["facets"]["unique_uid"] → unique viewer count
```

### Time-series (range)

```python
items = client.analytics.range(
    "mediaclip",
    from_date="2026-01-01",
    to_date="2026-03-31",
    granularity="day",
)
# items["items"] → [{datetime, total}, ...]
```

### Analytics facets reference

| Facet | What it returns |
|---|---|
| `eid` | Views per video ID |
| `title` | Views per video title |
| `uid` (+ `metric: "unique"`) | Unique viewer count |
| `avgViewTime` | Average view time in seconds |
| `completed` | Completed views (boolean breakdown) |
| `exactPercentageViewed` | Distribution of highest playback position (0–100%) |
| `lineitemInits` | Sessions per BB-managed lineitem (ad impressions per video) |
| `vastQuartiles` | VAST IAB quartile completions per video (25/50/75/100%) |
| `country`, `region`, `city` | Geographic breakdown |
| `domain`, `referrer` | Traffic source breakdown |
| `deviceType`, `mobileBrand` | Device breakdown |
| `osName`, `browserName` | Platform breakdown |

### Ad analytics notes

**Per-video impressions** — use `lineitemInits` from the analytics API.
The bb-backend ad-stats API (`/v1/ad-stats`) silently ignores `MediaClip` entity
filters: unknown types are dropped by the DTO, the filter builder returns empty
combinations, and the service falls back to publication totals. This is an
architectural limitation (Redis keys are scoped to AdUnit/AdSchedule/LineItem only).

**`lineitemInits`** is always tracked regardless of where the creative is hosted.

**Creative identity** (which video played as the ad) is only resolvable when the
creative is BB-hosted (VAST/VPAID/SIMID served from the OVP).

**`exactPercentageViewed`** = `highestTo / clipDuration × 100` — the highest
playback position reached in the session, not the exit point. A viewer who skips
to 80% counts as having reached 80%.

## Entity operations

```python
# Fetch a single entity
clip = client.get("mediaclip", "12345")

# List with filters
clips = client.list("mediaclip", limit=20, sort="createddate DESC",
                     filters={"status": "published"})

# Search (Solr query syntax)
results = client.search("title:football", entity_type="MediaClip", limit=10)

# Create
new_clip = client.create("mediaclip", {"title": "New Video"})

# Update
client.update("mediaclip", "12345", {"title": "Updated Title"})

# Delete (soft)
client.delete("mediaclip", "12345")

# Delete (permanent)
client.delete("mediaclip", "12345", purge=True)

# Entity action
client.action("mediaclip", "12345", "publish", method="PUT")
```

## LineItem version history

```python
from bb_sapi.entities.lineitem import LineItem

li = LineItem(client)

# Which creative(s) ran during the report period?
creatives = li.creatives_for_period(
    "starcasino_preroll",
    from_date="2026-01-01",
    to_date="2026-03-31",
)
# [{"version_id": "...", "date": "2026-01-15",
#   "creative_id": "7019583", "vast_url": "https://..."}]
```

When the creative changed mid-period, multiple entries are returned — one per
distinct creative that was active.

## Examples

See [`examples/analytics_export.py`](examples/analytics_export.py) for a complete
Excel analytics export with per-video ad metrics, VAST quartile data, viewcount
reach, and pre-roll creative breakdown.

```bash
SAPI_BASE_URL=https://mypub.bbvms.com \
SAPI_SHARED_SECRET=490-... \
SAPI_FROM_DATE=2026-01-01 \
SAPI_TO_DATE=2026-03-31 \
python examples/analytics_export.py
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
