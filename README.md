# bb-sapi-python-sdk

Python SDK for the [Blue Billywig](https://www.bluebillywig.com/) Streaming API (SAPI).

## Features

- HOTP authentication (HMAC-SHA1, 120-second window) — same scheme as the PHP SDK and MCP server
- Synchronous HTTP client built on `requests`
- Full analytics API: views, range, inits, faceted breakdowns
- Convenience helpers: top videos, unique viewers, viewcount reach (≥X%), per-video ad stats
- LineItem version history for resolving which creative ran during a report period
- Generic entity CRUD: `get`, `list`, `search`, `create`, `update`, `delete`, `action`
- Version history for any entity via `client.versions(entity, id)`
- TUS file upload: `upload_file()` and `create_mediaclip()` with S3 multipart support
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

# Version history (works for any entity: mediaclip, lineitem, playout, player, ...)
versions = client.versions("mediaclip", "12345")
# [{"id": "...", "date": "2026-01-15", "isLatest": False}, ...]
```

## File uploads (TUS)

The SAPI uses the [TUS protocol](https://tus.io/) backed by S3 multipart upload.

### Upload a file without creating a mediaclip entity

Use this for creatives, thumbnails, subtitle files, and images — anything
where the entity already exists or is managed separately.

```python
result = client.upload_file(
    "/tmp/ad_creative.mp4",
    mediaclip_id="12345",        # optional: attach to an existing mediaclip
    use_type="commercial",       # needs mediaclip_id — see below
)
print(result.tus_upload_id)      # TUS upload ID
print(result.s3_key)             # S3 object key
```

`title` and `use_type` are applied to `mediaclip_id` once the upload lands, so
they require it. Passing either without a `mediaclip_id` raises `SapiError`
rather than silently doing nothing — there is no entity to set them on.

### Create a mediaclip with a file

Full OVP6 workflow: creates the mediaclip entity first, then uploads the file.

The entity's `mediatype` is derived from the file extension: `video`, `audio`,
`image` and `font` files map to the mediatype of the same name, and anything
else (subtitles, PDFs, unrecognised binary) becomes a `document`. Pass
`extra_fields={"mediatype": ...}` to decide it yourself.

This is a local best guess, not a copy of the backend's own logic. The backend
re-derives the mediatype from the uploaded file during ingest, so the value
sent here need not be the final one.

The clip is always created as a `draft` and only moved to `status` once the
file has landed, so a failed upload cannot leave a published clip with no
media. If a step after the entity is created fails, the S3 upload is aborted
and the error names the clip ID so you can remove the empty entity.

```python
result = client.create_mediaclip(
    "/tmp/match_recap.mp4",
    title="Match Recap",
    description="Highlights from the match",
    tags=["football", "highlights"],
    status="draft",              # "draft" or "published"
    on_progress=lambda done, total: print(f"{done / total * 100:.0f}%"),
)
print(result.mediaclip_id)       # e.g. "12345"
```

### UploadResult

Both methods return an `UploadResult`:

```python
result.tus_upload_id    # SAPI TUS upload ID
result.upload_identifier
result.mediaclip_id     # set only by create_mediaclip()
result.file_name
result.file_size        # bytes
result.content_type
result.s3_key
```

### How it works

```
POST /sapi/tus                    ← create upload, get presigned S3 URLs
  Upload-Metadata: filename <b64>, filetype <b64> [, mediaclipId <b64>]

PUT  <presigned_url>  (×N parts)  ← upload chunks directly to S3
  ← collect ETag from each response

POST /sapi/tus/{id}/complete      ← finalise multipart upload
  {"parts": [{PartNumber, ETag}, ...]}
  ETags are sent exactly as S3 returned them, quotes included.
```

Before finalising, the SDK checks that the parts it uploaded actually cover the
file, so a disagreement between the server's part count and the local chunking
fails loudly instead of storing a truncated object.

### Resuming and cleaning up

```python
status = client.upload_status(tus_upload_id)   # HEAD  /sapi/tus/{id}
status.offset, status.length, status.uploaded_parts
status.is_complete

url = client.sign_part(tus_upload_id, 3)       # GET   /sapi/tus/{id}/sign/3
client.abort_upload(tus_upload_id)             # DELETE /sapi/tus/{id}
```

Part uploads that fail with a transient 5xx are retried, and a part whose
presigned URL has expired (HTTP 403) is re-signed and sent again. A failed
upload aborts itself, so `abort_upload` is only needed to clean up after a
crash — or when an error message tells you to.

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
git clone https://github.com/bluebillywig/bb-sapi-python-sdk.git
cd bb-sapi-python-sdk
pip install -e ".[dev]"
pytest
```

## Publishing to PyPI

### One-time setup

1. Create a [PyPI account](https://pypi.org/account/register/) and enable 2FA.

2. Create an API token at **PyPI → Account settings → API tokens** (scope: entire account for first upload, then restrict to this project).

3. Install build tools:

   ```bash
   pip install build twine
   ```

4. Store credentials in `~/.pypirc` (or use `TWINE_USERNAME`/`TWINE_PASSWORD` env vars):

   ```ini
   [pypi]
   username = __token__
   password = pypi-AgEIcHlwaS5vcmc...
   ```

### Release workflow

1. Bump the version in `pyproject.toml` and `bb_sapi/__init__.py`:

   ```toml
   # pyproject.toml
   version = "0.2.0"
   ```

   ```python
   # bb_sapi/__init__.py
   __version__ = "0.2.0"
   ```

2. Commit and tag:

   ```bash
   git add pyproject.toml bb_sapi/__init__.py
   git commit -m "Release v0.2.0"
   git tag v0.2.0
   git push && git push --tags
   ```

3. Build source distribution and wheel:

   ```bash
   python -m build
   # produces dist/bb_sapi_python_sdk-0.2.0.tar.gz
   #          dist/bb_sapi_python_sdk-0.2.0-py3-none-any.whl
   ```

4. Upload to PyPI:

   ```bash
   twine upload dist/*
   ```

5. Verify: `pip install bb-sapi-python-sdk==0.2.0`

### Test on TestPyPI first (optional)

```bash
twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ bb-sapi-python-sdk
```

## License

MIT
