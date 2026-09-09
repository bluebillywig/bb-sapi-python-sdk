"""Tests for TUS upload flow."""
import io
import json
import os
import tempfile

import pytest
import responses as resp_lib

from bb_sapi import SapiClient, UploadResult
from bb_sapi.exceptions import SapiError
from bb_sapi.upload import TusUploader, _b64

BASE_URL = "https://test.bbvms.com"
SECRET = "490-deadbeef"

TUS_CREATE_RESPONSE = {
    "tusUploadId": "abc123",
    "uploadIdentifier": "uid-xyz",
    "s3": {
        "key": "pub/media/file.mp4",
        "partSize": 5 * 1024 * 1024,
        "presignedUrls": [
            {"partNumber": 1, "url": "https://s3.example.com/upload?part=1"},
        ],
    },
}


def make_client() -> SapiClient:
    return SapiClient(BASE_URL, SECRET, timeout=5)


def make_temp_file(content: bytes = b"x" * 10, suffix: str = ".mp4") -> str:
    """Create a temporary file and return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.write(fd, content)
    os.close(fd)
    return path


# ---------------------------------------------------------------------------
# _b64 helper
# ---------------------------------------------------------------------------

def test_b64_roundtrip():
    import base64
    val = "hello world"
    encoded = _b64(val)
    assert base64.b64decode(encoded).decode() == val


# ---------------------------------------------------------------------------
# TusUploader._tus_create
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_tus_create_sends_required_headers():
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus",
        json=TUS_CREATE_RESPONSE,
        status=200,
    )
    client = make_client()
    uploader = TusUploader(client)
    result = uploader._tus_create(1024, "filename aGVsbG8=")

    req = resp_lib.calls[0].request
    assert req.headers.get("Tus-Resumable") == "1.0.0"
    assert req.headers.get("Upload-Length") == "1024"
    assert req.headers.get("Upload-Metadata") == "filename aGVsbG8="
    assert "rpctoken" in req.headers
    assert result["tusUploadId"] == "abc123"


@resp_lib.activate
def test_tus_create_unwraps_data_envelope():
    """Response wrapped in {status, data} is also accepted."""
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus",
        json={"status": "success", "data": TUS_CREATE_RESPONSE},
        status=200,
    )
    client = make_client()
    uploader = TusUploader(client)
    result = uploader._tus_create(1024, "x")
    assert result["tusUploadId"] == "abc123"


@resp_lib.activate
def test_tus_create_unexpected_shape_raises():
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus",
        json={"something": "unexpected"},
        status=200,
    )
    client = make_client()
    uploader = TusUploader(client)
    with pytest.raises(SapiError, match="Unexpected TUS create response"):
        uploader._tus_create(1024, "x")


# ---------------------------------------------------------------------------
# TusUploader._tus_complete
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_tus_complete_sends_correct_request():
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
        status=200,
    )
    client = make_client()
    uploader = TusUploader(client)
    parts = [{"PartNumber": 1, "ETag": "etag123"}]
    uploader._tus_complete("abc123", parts)

    req = resp_lib.calls[0].request
    assert req.headers.get("Tus-Resumable") == "1.0.0"
    assert "rpctoken" in req.headers
    # The endpoint expects the parts under a "parts" key; a bare array is read
    # as no parts at all.
    assert json.loads(req.body) == {"parts": parts}


# ---------------------------------------------------------------------------
# Full upload_file flow
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_upload_file_happy_path():
    # TUS create
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    # S3 PUT chunk
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"etag-part1"'},
    )
    # TUS complete
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    path = make_temp_file(b"fake video data")
    try:
        client = make_client()
        result = client.upload_file(path)
        assert isinstance(result, UploadResult)
        assert result.tus_upload_id == "abc123"
        assert result.s3_key == "pub/media/file.mp4"
        assert result.mediaclip_id is None
        assert len(resp_lib.calls) == 3
    finally:
        os.unlink(path)


@resp_lib.activate
def test_upload_file_with_mediaclip_id_includes_in_metadata():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"etag1"'},
    )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    path = make_temp_file()
    try:
        client = make_client()
        result = client.upload_file(path, mediaclip_id="99999")

        tus_req = resp_lib.calls[0].request
        metadata = tus_req.headers.get("Upload-Metadata", "")
        assert "mediaclipId" in metadata
        assert _b64("99999") in metadata
        assert result.mediaclip_id == "99999"
    finally:
        os.unlink(path)


@resp_lib.activate
def test_upload_file_s3_failure_raises():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=404,
        body="NoSuchUpload",
    )
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/abc123", status=204)

    path = make_temp_file()
    try:
        client = make_client()
        with pytest.raises(SapiError, match="S3 upload failed for part 1"):
            client.upload_file(path)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# create_mediaclip flow
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_create_mediaclip_happy_path():
    # Create mediaclip entity
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/mediaclip/new",
        json={"id": 42, "title": "Test"},
    )
    # TUS create
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    # S3 PUT
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"etag1"'},
    )
    # TUS complete
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    path = make_temp_file(b"video content")
    try:
        client = make_client()
        result = client.create_mediaclip(path, title="My Video")
        assert result.mediaclip_id == "42"
        assert result.tus_upload_id == "abc123"
        assert len(resp_lib.calls) == 4

        # Verify mediaclipId was included in TUS metadata
        tus_req = resp_lib.calls[1].request
        assert _b64("42") in tus_req.headers.get("Upload-Metadata", "")
    finally:
        os.unlink(path)


@resp_lib.activate
def test_create_mediaclip_no_id_in_response_raises():
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/mediaclip/new",
        json={"title": "oops"},  # no id
    )

    path = make_temp_file()
    try:
        client = make_client()
        with pytest.raises(SapiError, match="Failed to obtain mediaclip ID"):
            client.create_mediaclip(path, title="Broken")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# on_progress callback
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_on_progress_called():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"e"'},
    )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    calls: list[tuple[int, int]] = []
    path = make_temp_file(b"data")
    try:
        client = make_client()
        client.upload_file(path, on_progress=lambda done, total: calls.append((done, total)))
        assert len(calls) == 1
        done, total = calls[0]
        assert done == total
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# mediatype derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "content_type,expected",
    [
        ("video/mp4", "video"),
        ("video/quicktime", "video"),
        ("audio/mpeg", "audio"),
        ("image/png", "image"),
        ("image/jpeg", "image"),
        ("application/pdf", "document"),
        ("text/vtt", "document"),
        ("application/octet-stream", "document"),
    ],
)
def test_media_type_mapping(content_type, expected):
    """Guards the mapping table only.

    This passes even if the call site stops using _media_type, so
    test_create_mediaclip_sends_correct_mediatype is the load-bearing test for
    the wiring — don't delete it as redundant with this one.
    """
    uploader = TusUploader(make_client())
    assert uploader._media_type(content_type) == expected


@resp_lib.activate
@pytest.mark.parametrize(
    "suffix,expected",
    [
        (".png", "image"),
        (".bmp", "image"),      # absent from the SDK's own table
        (".mp4", "video"),
        (".wmv", "video"),      # absent from the SDK's own table
        (".mxf", "video"),      # MIME type carries no family
        (".mp3", "audio"),
        (".flac", "audio"),     # absent from the SDK's own table
        (".ttf", "font"),       # originalfilename is public only for fonts
        (".eot", "font"),       # MIME type carries no family
        (".pdf", "document"),
        (".srt", "document"),   # the case the old code called "audio"
    ],
)
def test_create_mediaclip_sends_correct_mediatype(suffix, expected):
    """An image must not be created as an audio clip (shows a speaker icon in the OVP)."""
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/mediaclip/new", json={"id": 7})
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"etag1"'},
    )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    path = make_temp_file(b"data", suffix=suffix)
    try:
        client = make_client()
        client.create_mediaclip(path, title="Asset")
        body = json.loads(resp_lib.calls[0].request.body)
        assert body["mediatype"] == expected
    finally:
        os.unlink(path)


@resp_lib.activate
def test_create_mediaclip_extra_fields_can_override_mediatype():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/mediaclip/new", json={"id": 7})
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"e"'},
    )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    path = make_temp_file(b"data", suffix=".ttf")
    try:
        client = make_client()
        client.create_mediaclip(path, extra_fields={"mediatype": "font"})
        body = json.loads(resp_lib.calls[0].request.body)
        assert body["mediatype"] == "font"
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# S3 ETag handling
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_etag_quotes_are_preserved():
    """S3 returns a quoted ETag; CompleteMultipartUpload expects it verbatim."""
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/upload",
        status=200,
        headers={"ETag": '"d41d8cd98f00b204e9800998ecf8427e"'},
    )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"status": "success"},
    )

    path = make_temp_file(b"data")
    try:
        client = make_client()
        client.upload_file(path)
        body = json.loads(resp_lib.calls[2].request.body)
        assert body == {
            "parts": [
                {"PartNumber": 1, "ETag": '"d41d8cd98f00b204e9800998ecf8427e"'}
            ]
        }
    finally:
        os.unlink(path)


@resp_lib.activate
def test_missing_etag_raises_rather_than_completing_with_empty_part():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(resp_lib.PUT, "https://s3.example.com/upload", status=200)
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/abc123", status=204)

    path = make_temp_file(b"data")
    try:
        client = make_client()
        with pytest.raises(SapiError, match="no usable ETag for part 1"):
            client.upload_file(path)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Multi-part uploads
#
# Every test above uploads a single part, so the chunking, ordering and
# progress arithmetic that the parts wrapper exists to serve went unexercised.
# ---------------------------------------------------------------------------

MULTIPART_RESPONSE = {
    "tusUploadId": "multi1",
    "uploadIdentifier": "uid-multi",
    "s3": {
        "key": "pub/media/big.mp4",
        "partSize": 10,
        "presignedUrls": [
            {"partNumber": 1, "url": "https://s3.example.com/p1"},
            {"partNumber": 2, "url": "https://s3.example.com/p2"},
            {"partNumber": 3, "url": "https://s3.example.com/p3"},
        ],
    },
}


@resp_lib.activate
def test_multipart_upload_sends_each_chunk_and_orders_parts():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=MULTIPART_RESPONSE)
    for n in (1, 2, 3):
        resp_lib.add(
            resp_lib.PUT,
            f"https://s3.example.com/p{n}",
            status=200,
            headers={"ETag": f'"etag{n}"'},
        )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/multi1/complete",
        json={"success": True, "key": "pub/media/big.mp4"},
    )

    progress: list[tuple[int, int]] = []
    # 25 bytes at partSize 10 -> 10 + 10 + 5
    path = make_temp_file(b"A" * 10 + b"B" * 10 + b"C" * 5)
    try:
        client = make_client()
        client.upload_file(path, on_progress=lambda d, t: progress.append((d, t)))

        puts = [c for c in resp_lib.calls if c.request.method == "PUT"]
        assert [p.request.body for p in puts] == [b"A" * 10, b"B" * 10, b"C" * 5]

        complete = json.loads(resp_lib.calls[-1].request.body)
        assert complete == {
            "parts": [
                {"PartNumber": 1, "ETag": '"etag1"'},
                {"PartNumber": 2, "ETag": '"etag2"'},
                {"PartNumber": 3, "ETag": '"etag3"'},
            ]
        }
        assert progress == [(10, 25), (20, 25), (25, 25)]
    finally:
        os.unlink(path)


@resp_lib.activate
def test_part_count_mismatch_refuses_to_truncate():
    """Server part count and local offsets must agree, or the object won't match."""
    mismatched = {
        **MULTIPART_RESPONSE,
        "s3": {**MULTIPART_RESPONSE["s3"], "presignedUrls": [
            {"partNumber": 1, "url": "https://s3.example.com/p1"},
        ]},
    }
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=mismatched)
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/multi1", status=204)

    path = make_temp_file(b"x" * 25)  # needs 3 parts at partSize 10, got 1
    try:
        client = make_client()
        with pytest.raises(SapiError, match="returned 1 presigned URLs but"):
            client.upload_file(path)
        assert not [c for c in resp_lib.calls if c.request.method == "PUT"]
    finally:
        os.unlink(path)


@resp_lib.activate
def test_empty_presigned_urls_refuses_rather_than_uploading_nothing():
    empty = {**MULTIPART_RESPONSE, "s3": {**MULTIPART_RESPONSE["s3"], "presignedUrls": []}}
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=empty)
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/multi1", status=204)

    path = make_temp_file(b"real content")
    try:
        client = make_client()
        with pytest.raises(SapiError, match="no presigned upload URLs"):
            client.upload_file(path)
    finally:
        os.unlink(path)


@resp_lib.activate
def test_expired_presigned_url_is_resigned_and_retried():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(resp_lib.PUT, "https://s3.example.com/upload", status=403, body="Expired")
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/sapi/tus/abc123/sign/1",
        json={"url": "https://s3.example.com/fresh"},
    )
    resp_lib.add(
        resp_lib.PUT,
        "https://s3.example.com/fresh",
        status=200,
        headers={"ETag": '"resigned"'},
    )
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/sapi/tus/abc123/complete", json={"success": True}
    )

    path = make_temp_file(b"data")
    try:
        client = make_client()
        client.upload_file(path)
        body = json.loads(resp_lib.calls[-1].request.body)
        assert body == {"parts": [{"PartNumber": 1, "ETag": '"resigned"'}]}
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Refusing to send something that cannot work
# ---------------------------------------------------------------------------

def test_empty_file_is_rejected_before_any_request():
    path = make_temp_file(b"")
    try:
        client = make_client()
        with pytest.raises(SapiError, match="empty"):
            client.upload_file(path)
    finally:
        os.unlink(path)


@resp_lib.activate
def test_unusable_etag_is_rejected():
    """A proxy returning an empty quoted ETag must not reach CompleteMultipartUpload."""
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT, "https://s3.example.com/upload", status=200, headers={"ETag": '""'}
    )
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/abc123", status=204)

    path = make_temp_file(b"data")
    try:
        client = make_client()
        with pytest.raises(SapiError, match="no usable ETag"):
            client.upload_file(path)
    finally:
        os.unlink(path)


def test_tus_complete_refuses_empty_parts():
    client = make_client()
    uploader = TusUploader(client)
    with pytest.raises(SapiError, match="with no parts"):
        uploader._tus_complete("abc123", [])


@resp_lib.activate
def test_complete_reporting_failure_in_a_200_body_is_not_treated_as_success():
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={"error": "MalformedXML"},
        status=200,
    )
    client = make_client()
    uploader = TusUploader(client)
    with pytest.raises(SapiError, match="MalformedXML"):
        uploader._tus_complete("abc123", [{"PartNumber": 1, "ETag": '"e"'}])


# ---------------------------------------------------------------------------
# Cleanup on failure
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_failed_upload_aborts_the_s3_multipart_upload():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(resp_lib.PUT, "https://s3.example.com/upload", status=404, body="gone")
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/abc123", status=204)

    path = make_temp_file(b"data")
    try:
        client = make_client()
        with pytest.raises(SapiError, match="was aborted"):
            client.upload_file(path)
        deletes = [c for c in resp_lib.calls if c.request.method == "DELETE"]
        assert len(deletes) == 1
        assert deletes[0].request.url.endswith("/sapi/tus/abc123")
    finally:
        os.unlink(path)


@resp_lib.activate
def test_failed_create_mediaclip_upload_names_the_orphan_clip():
    """The caller cannot clean up an orphan whose ID the error never mentions."""
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/mediaclip/new", json={"id": 4242})
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(resp_lib.PUT, "https://s3.example.com/upload", status=404, body="gone")
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/abc123", status=204)

    path = make_temp_file(b"data")
    try:
        client = make_client()
        with pytest.raises(SapiError) as excinfo:
            client.create_mediaclip(path)
        message = str(excinfo.value)
        assert "4242" in message                      # the orphan entity
        assert "abc123" in message                    # the S3 upload
        assert "delete('mediaclip', '4242')" in message
    finally:
        os.unlink(path)


@resp_lib.activate
def test_publish_only_happens_after_the_upload_lands():
    """A failed upload must never leave a published clip with no media."""
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/mediaclip/new", json={"id": 7})
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT, "https://s3.example.com/upload", status=200, headers={"ETag": '"e"'}
    )
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/sapi/tus/abc123/complete", json={"success": True}
    )
    resp_lib.add(resp_lib.PUT, f"{BASE_URL}/sapi/mediaclip/7", json={"id": 7})

    path = make_temp_file(b"data")
    try:
        client = make_client()
        client.create_mediaclip(path, status="published")

        created = json.loads(resp_lib.calls[0].request.body)
        assert created["status"] == "draft", "clip must be created as a draft"

        publish = resp_lib.calls[-1].request
        assert publish.method == "PUT"
        assert publish.url.endswith("/sapi/mediaclip/7")
        assert json.loads(publish.body) == {"status": "published"}
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# title / use_type on upload_file
# ---------------------------------------------------------------------------

def test_upload_file_rejects_title_without_a_mediaclip_to_put_it_on():
    path = make_temp_file()
    try:
        client = make_client()
        with pytest.raises(SapiError, match="no mediaclip_id"):
            client.upload_file(path, title="Hero image")
    finally:
        os.unlink(path)


@resp_lib.activate
def test_upload_file_applies_title_and_use_type_to_the_mediaclip():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT, "https://s3.example.com/upload", status=200, headers={"ETag": '"e"'}
    )
    resp_lib.add(
        resp_lib.POST, f"{BASE_URL}/sapi/tus/abc123/complete", json={"success": True}
    )
    resp_lib.add(resp_lib.PUT, f"{BASE_URL}/sapi/mediaclip/99", json={"id": 99})

    path = make_temp_file()
    try:
        client = make_client()
        client.upload_file(path, mediaclip_id="99", title="Hero", use_type="editorial")
        update = resp_lib.calls[-1].request
        assert update.method == "PUT"
        assert json.loads(update.body) == {"title": "Hero", "usetype": "editorial"}
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Upload lifecycle: status, sign, abort
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_upload_status_reads_offset_and_parts_from_headers():
    resp_lib.add(
        resp_lib.HEAD,
        f"{BASE_URL}/sapi/tus/abc123",
        status=200,
        headers={
            "Upload-Offset": "10",
            "Upload-Length": "25",
            "X-Tus-Data": json.dumps(
                {"s3": {"partSize": 10, "uploadedParts": [
                    {"PartNumber": 1, "Size": 10, "ETag": '"e1"'}]}}
            ),
        },
    )
    status = make_client().upload_status("abc123")
    assert (status.offset, status.length, status.part_size) == (10, 25, 10)
    assert status.uploaded_parts == [{"PartNumber": 1, "Size": 10, "ETag": '"e1"'}]
    assert status.is_complete is False


@resp_lib.activate
def test_upload_status_reports_completion():
    resp_lib.add(
        resp_lib.HEAD,
        f"{BASE_URL}/sapi/tus/abc123",
        status=200,
        headers={"Upload-Offset": "25", "Upload-Length": "25"},
    )
    assert make_client().upload_status("abc123").is_complete is True


@resp_lib.activate
def test_sign_part_returns_a_fresh_url():
    resp_lib.add(
        resp_lib.GET,
        f"{BASE_URL}/sapi/tus/abc123/sign/3",
        json={"url": "https://s3.example.com/fresh"},
    )
    assert make_client().sign_part("abc123", 3) == "https://s3.example.com/fresh"


@resp_lib.activate
def test_abort_upload_tolerates_an_already_forgotten_upload():
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/gone", status=404)
    make_client().abort_upload("gone")  # must not raise


@resp_lib.activate
def test_abort_upload_raises_on_a_real_failure():
    resp_lib.add(resp_lib.DELETE, f"{BASE_URL}/sapi/tus/abc123", status=500, body="boom")
    with pytest.raises(SapiError, match="Could not abort"):
        make_client().abort_upload("abc123")


# ---------------------------------------------------------------------------
# The completed upload's own view of where the file landed
# ---------------------------------------------------------------------------

@resp_lib.activate
def test_result_prefers_the_key_the_server_confirmed():
    resp_lib.add(resp_lib.POST, f"{BASE_URL}/sapi/tus", json=TUS_CREATE_RESPONSE)
    resp_lib.add(
        resp_lib.PUT, "https://s3.example.com/upload", status=200, headers={"ETag": '"e"'}
    )
    resp_lib.add(
        resp_lib.POST,
        f"{BASE_URL}/sapi/tus/abc123/complete",
        json={
            "success": True,
            "key": "pub/media/actual-key.mp4",
            "location": "https://bucket.s3.amazonaws.com/pub/media/actual-key.mp4",
        },
    )

    path = make_temp_file()
    try:
        result = make_client().upload_file(path)
        assert result.s3_key == "pub/media/actual-key.mp4"
        assert result.location.endswith("actual-key.mp4")
    finally:
        os.unlink(path)
