"""
TUS-based file upload for Blue Billywig SAPI.

Upload flow
-----------
1. POST /sapi/tus          — create upload, get presigned S3 URLs
2. PUT  <presigned_url>    — upload each chunk directly to S3
3. POST /sapi/tus/{id}/complete — finalize multipart upload

The SAPI also supports a full mediaclip creation workflow:
1. Create a mediaclip entity (to obtain a clip ID)
2. Create TUS upload referencing that clip ID in metadata
3. Upload chunks to S3
4. Complete the upload
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import requests as _requests

from bb_sapi.exceptions import SapiError

if TYPE_CHECKING:
    from bb_sapi.client import SapiClient


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class UploadResult:
    """Result of a completed TUS upload."""
    tus_upload_id: str
    upload_identifier: str
    file_name: str
    file_size: int
    content_type: str
    s3_key: str
    mediaclip_id: Optional[str] = None

    def __repr__(self) -> str:
        parts = [
            f"file={self.file_name!r}",
            f"size={self.file_size}",
            f"tus_upload_id={self.tus_upload_id!r}",
        ]
        if self.mediaclip_id:
            parts.append(f"mediaclip_id={self.mediaclip_id!r}")
        return f"UploadResult({', '.join(parts)})"


# ---------------------------------------------------------------------------
# TUS client
# ---------------------------------------------------------------------------

class TusUploader:
    """
    Handles TUS-protocol file uploads to the Blue Billywig SAPI.

    You do not normally instantiate this directly — use
    :meth:`SapiClient.upload_file` or :meth:`SapiClient.create_mediaclip`
    instead.
    """

    _CONTENT_TYPES = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".mp3": "audio/mpeg",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".srt": "application/x-subrip",
        ".vtt": "text/vtt",
        ".pdf": "application/pdf",
    }

    def __init__(self, client: SapiClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_file(
        self,
        file_path: str | os.PathLike,
        *,
        title: Optional[str] = None,
        use_type: str = "commercial",
        mediaclip_id: Optional[str | int] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> UploadResult:
        """
        Upload a file (image, video, audio, subtitle, document) via TUS.

        This uploads the file to the SAPI without first creating a mediaclip
        entity. Useful for uploading creatives, thumbnails, and subtitle files.
        For full mediaclip creation use :meth:`create_mediaclip` instead.

        Args:
            file_path:    Path to the local file.
            title:        Display name (defaults to the file name without extension).
            use_type:     ``"commercial"`` (creative/ad) or ``"editorial"`` (content).
            mediaclip_id: Attach the uploaded file to an existing MediaClip.
            on_progress:  Optional callback ``(bytes_uploaded, total_bytes)``.

        Returns:
            :class:`UploadResult` with IDs and metadata.
        """
        path = Path(file_path)
        file_name = path.name
        file_size = path.stat().st_size
        content_type = self._content_type(path)

        metadata_parts = [
            f"filename {_b64(file_name)}",
            f"filetype {_b64(content_type)}",
        ]
        if mediaclip_id is not None:
            metadata_parts.append(f"mediaclipId {_b64(str(mediaclip_id))}")

        tus_data = self._tus_create(file_size, ",".join(metadata_parts))
        parts = self._upload_chunks(path, tus_data["s3"], on_progress=on_progress)
        self._tus_complete(tus_data["tusUploadId"], parts)

        return UploadResult(
            tus_upload_id=tus_data["tusUploadId"],
            upload_identifier=tus_data.get("uploadIdentifier", ""),
            file_name=file_name,
            file_size=file_size,
            content_type=content_type,
            s3_key=tus_data["s3"]["key"],
            mediaclip_id=str(mediaclip_id) if mediaclip_id is not None else None,
        )

    def create_mediaclip(
        self,
        file_path: str | os.PathLike,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        use_type: str = "editorial",
        status: str = "draft",
        extra_fields: Optional[dict[str, Any]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> UploadResult:
        """
        Full mediaclip creation workflow: create entity → TUS upload → S3 → complete.

        Mirrors the OVP6 frontend flow:

        1. Create a ``mediaclip`` entity (to obtain a clip ID)
        2. Create a TUS upload referencing that clip ID in metadata
        3. Upload file chunks to S3 via presigned URLs
        4. Complete the TUS upload

        Args:
            file_path:    Path to the local video/audio file.
            title:        Display title (defaults to filename without extension).
            description:  Optional description.
            tags:         Optional list of tags.
            use_type:     ``"editorial"`` (content) or ``"commercial"`` (creative/ad).
            status:       Initial status: ``"draft"`` (default) or ``"published"``.
            extra_fields: Any additional fields to include when creating the entity.
            on_progress:  Optional callback ``(bytes_uploaded, total_bytes)``.

        Returns:
            :class:`UploadResult` including the ``mediaclip_id``.
        """
        path = Path(file_path)
        file_name = path.name
        file_name_no_ext = path.stem
        file_size = path.stat().st_size
        content_type = self._content_type(path)
        media_type = "video" if content_type.startswith("video") else "audio"

        # Step 1 — create the mediaclip entity
        clip_data: dict[str, Any] = {
            "title": title or file_name_no_ext,
            "originalfilename": file_name,
            "mediatype": media_type,
            "usetype": use_type,
            "status": status,
        }
        if description:
            clip_data["description"] = description
        if tags:
            clip_data["tags"] = tags
        if extra_fields:
            clip_data.update(extra_fields)

        clip = self._client.create("mediaclip", clip_data)
        clip_id = clip.get("id") or clip.get("mediaclipId")
        if not clip_id:
            raise SapiError(
                f"Failed to obtain mediaclip ID from create response. "
                f"Response keys: {list(clip.keys())}"
            )
        clip_id = str(clip_id)

        # Step 2 — create TUS upload with mediaclipId in metadata
        metadata_parts = [
            f"filename {_b64(file_name)}",
            f"filetype {_b64(content_type)}",
            f"mediaclipId {_b64(clip_id)}",
        ]
        tus_data = self._tus_create(file_size, ",".join(metadata_parts))

        # Step 3 — upload chunks to S3
        parts = self._upload_chunks(path, tus_data["s3"], on_progress=on_progress)

        # Step 4 — complete
        self._tus_complete(tus_data["tusUploadId"], parts)

        return UploadResult(
            tus_upload_id=tus_data["tusUploadId"],
            upload_identifier=tus_data.get("uploadIdentifier", ""),
            file_name=file_name,
            file_size=file_size,
            content_type=content_type,
            s3_key=tus_data["s3"]["key"],
            mediaclip_id=clip_id,
        )

    # ------------------------------------------------------------------
    # Internal TUS calls
    # ------------------------------------------------------------------

    def _tus_create(self, file_size: int, upload_metadata: str) -> dict[str, Any]:
        """POST /sapi/tus — initiate upload, get presigned S3 URLs."""
        url = f"{self._client._base_url}/sapi/tus"
        headers = {
            **self._client._auth.headers(),
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(file_size),
            "Upload-Metadata": upload_metadata,
        }
        resp = self._client._session.post(url, headers=headers, timeout=self._client._timeout)
        from bb_sapi.client import SapiClient  # local import to avoid circular
        body = SapiClient._handle_response(resp)
        # Normalise: the response may be the data directly or wrapped in {status, data}
        if "tusUploadId" in body:
            return body
        if "data" in body:
            return body["data"]
        raise SapiError(f"Unexpected TUS create response: {list(body.keys())}")

    def _tus_complete(
        self, tus_upload_id: str, parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """POST /sapi/tus/{id}/complete — finalise the multipart upload."""
        url = f"{self._client._base_url}/sapi/tus/{tus_upload_id}/complete"
        headers = {
            **self._client._auth.headers(),
            "Tus-Resumable": "1.0.0",
        }
        resp = self._client._session.post(
            url,
            json=parts,
            headers=headers,
            timeout=self._client._timeout,
        )
        from bb_sapi.client import SapiClient
        return SapiClient._handle_response(resp)

    # ------------------------------------------------------------------
    # S3 multipart upload
    # ------------------------------------------------------------------

    def _upload_chunks(
        self,
        path: Path,
        s3_info: dict[str, Any],
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict[str, Any]]:
        """
        Upload file chunks to S3 via presigned URLs.

        Returns a list of ``{PartNumber, ETag}`` dicts for the complete call.
        """
        presigned_urls: list[dict[str, Any]] = s3_info["presignedUrls"]
        part_size: int = s3_info.get("partSize", 5 * 1024 * 1024)
        file_size = path.stat().st_size
        parts: list[dict[str, Any]] = []
        bytes_uploaded = 0

        with open(path, "rb") as fh:
            for entry in presigned_urls:
                part_number: int = entry["partNumber"]
                url: str = entry["url"]
                start = (part_number - 1) * part_size
                end = min(start + part_size, file_size)
                fh.seek(start)
                chunk = fh.read(end - start)

                resp = _requests.put(
                    url,
                    data=chunk,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=self._client._timeout,
                )
                if not resp.ok:
                    raise SapiError(
                        f"S3 upload failed for part {part_number}: "
                        f"HTTP {resp.status_code} — {resp.text[:200]}"
                    )

                etag = resp.headers.get("ETag", "").strip('"')
                parts.append({"PartNumber": part_number, "ETag": etag})

                bytes_uploaded += len(chunk)
                if on_progress:
                    on_progress(bytes_uploaded, file_size)

        return parts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _content_type(self, path: Path) -> str:
        return self._CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _b64(value: str) -> str:
    """Base64-encode a string value for TUS Upload-Metadata headers."""
    return base64.b64encode(value.encode()).decode()
