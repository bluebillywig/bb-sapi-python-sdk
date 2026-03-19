"""Tests for TUS upload flow."""
import io
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


def make_temp_file(content: bytes = b"x" * 10) -> str:
    """Create a temporary file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
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
    import json
    assert json.loads(req.body) == parts


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
        status=403,
        body="AccessDenied",
    )

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
