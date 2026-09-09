"""
TUS-based file upload for Blue Billywig SAPI.

Upload flow
-----------
1. POST /sapi/tus          — create upload, get presigned S3 URLs
2. PUT  <presigned_url>    — upload each chunk directly to S3
3. POST /sapi/tus/{id}/complete — finalize multipart upload

Resumption and cleanup
----------------------
HEAD   /sapi/tus/{id}           — bytes already stored, and the parts S3 holds
GET    /sapi/tus/{id}/sign/{n}  — a fresh presigned URL for one part
DELETE /sapi/tus/{id}           — abort the upload and release the S3 parts

The SAPI also supports a full mediaclip creation workflow:
1. Create a mediaclip entity (to obtain a clip ID)
2. Create TUS upload referencing that clip ID in metadata
3. Upload chunks to S3
4. Complete the upload
"""
from __future__ import annotations

import base64
import json as _json
import math
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal, Optional

import requests as _requests
from requests.adapters import HTTPAdapter, Retry

from bb_sapi.exceptions import SapiError

if TYPE_CHECKING:
    from bb_sapi.client import SapiClient


#: Every ``mediatype`` the SAPI recognises. ``interactive`` exists on the
#: backend but is never produced from a local file.
MediaType = Literal["video", "audio", "image", "font", "document", "interactive"]

#: The subset :meth:`TusUploader._media_type` can derive from a file.
DerivedMediaType = Literal["video", "audio", "image", "font", "document"]


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
    location: Optional[str] = None

    def __repr__(self) -> str:
        parts = [
            f"file={self.file_name!r}",
            f"size={self.file_size}",
            f"tus_upload_id={self.tus_upload_id!r}",
        ]
        if self.mediaclip_id:
            parts.append(f"mediaclip_id={self.mediaclip_id!r}")
        return f"UploadResult({', '.join(parts)})"


@dataclass
class UploadStatus:
    """Progress of an in-flight TUS upload, as reported by the server."""
    tus_upload_id: str
    offset: int
    length: Optional[int]
    part_size: int
    uploaded_parts: list[dict[str, Any]]

    @property
    def is_complete(self) -> bool:
        """True when S3 already holds every byte the upload was created for."""
        return self.length is not None and self.offset >= self.length


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

    #: Extensions whose MIME type the stdlib gets wrong, or does not know.
    #: Anything absent here falls through to :mod:`mimetypes`.
    _CONTENT_TYPES: ClassVar[dict[str, str]] = {
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

    #: Extensions whose MIME type carries no usable media family, mapped to the
    #: SAPI ``mediatype`` directly. ``application/mxf`` and
    #: ``application/vnd.ms-fontobject`` are correct on the wire but say nothing
    #: about the family, so the extension is the only signal available.
    _MEDIA_TYPE_BY_EXTENSION: ClassVar[dict[str, DerivedMediaType]] = {
        ".mxf": "video",
        ".vob": "video",
        ".eot": "font",
    }

    #: Transient S3 responses worth retrying. 403 is handled separately — it
    #: usually means the presigned URL expired, which is repaired by re-signing.
    _S3_RETRY_STATUSES = (500, 502, 503, 504)
    _S3_RETRIES = 3

    def __init__(self, client: SapiClient) -> None:
        self._client = client
        self._s3_session: Optional[_requests.Session] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_file(
        self,
        file_path: str | os.PathLike,
        *,
        title: Optional[str] = None,
        use_type: Optional[str] = None,
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
            title:        Display name to set on ``mediaclip_id`` once the
                          upload lands. Requires ``mediaclip_id``.
            use_type:     ``"commercial"`` (creative/ad) or ``"editorial"``
                          (content), set on ``mediaclip_id`` once the upload
                          lands. Requires ``mediaclip_id``.
            mediaclip_id: Attach the uploaded file to an existing MediaClip.
            on_progress:  Optional callback ``(bytes_uploaded, total_bytes)``.

        Returns:
            :class:`UploadResult` with IDs and metadata.

        Raises:
            SapiError: If ``title`` or ``use_type`` is given without
                ``mediaclip_id``, since there would be no entity to set them on.
        """
        if mediaclip_id is None and (title is not None or use_type is not None):
            given = ", ".join(
                name
                for name, value in (("title", title), ("use_type", use_type))
                if value is not None
            )
            raise SapiError(
                f"upload_file() was given {given} but no mediaclip_id. This call "
                f"uploads a file without creating an entity, so there is nothing "
                f"to set those on. Pass mediaclip_id to update an existing clip, "
                f"or use create_mediaclip() to make a new one."
            )

        path = Path(file_path)
        file_name = path.name
        file_size = self._file_size(path)
        content_type = self._content_type(path)

        metadata_parts = [
            f"filename {_b64(file_name)}",
            f"filetype {_b64(content_type)}",
        ]
        if mediaclip_id is not None:
            metadata_parts.append(f"mediaclipId {_b64(str(mediaclip_id))}")

        tus_data = self._tus_create(file_size, ",".join(metadata_parts))
        tus_upload_id = tus_data["tusUploadId"]

        completed = self._run_upload(
            path,
            tus_data,
            file_size,
            on_progress=on_progress,
            context=f"upload of {file_name!r}",
        )

        if mediaclip_id is not None:
            clip_updates: dict[str, Any] = {}
            if title is not None:
                clip_updates["title"] = title
            if use_type is not None:
                clip_updates["usetype"] = use_type
            if clip_updates:
                self._client.update("mediaclip", str(mediaclip_id), clip_updates)

        return UploadResult(
            tus_upload_id=tus_upload_id,
            upload_identifier=completed.get("uploadIdentifier")
            or tus_data.get("uploadIdentifier", ""),
            file_name=file_name,
            file_size=file_size,
            content_type=content_type,
            s3_key=completed.get("key") or tus_data["s3"]["key"],
            mediaclip_id=str(mediaclip_id) if mediaclip_id is not None else None,
            location=completed.get("location"),
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

        The entity's ``mediatype`` is derived from the file — see
        :meth:`_media_type` for the mapping and its limits. Pass
        ``extra_fields={"mediatype": ...}`` to set it yourself.

        The clip is always created as a draft and only moved to ``status`` once
        the file has landed, so a failed upload can never leave a published clip
        with no media. If any step after the entity is created fails, the S3
        multipart upload is aborted and the raised error names the clip ID so
        the empty entity can be cleaned up.

        Args:
            file_path:    Path to the local media file.
            title:        Display title (defaults to filename without extension).
            description:  Optional description.
            tags:         Optional list of tags.
            use_type:     ``"editorial"`` (content) or ``"commercial"`` (creative/ad).
            status:       Status to apply once the upload lands: ``"draft"``
                          (default) or ``"published"``.
            extra_fields: Any additional fields to include when creating the
                          entity. These override every derived and explicit
                          field above, including ``mediatype`` and ``title``.
            on_progress:  Optional callback ``(bytes_uploaded, total_bytes)``.

        Returns:
            :class:`UploadResult` including the ``mediaclip_id``.
        """
        path = Path(file_path)
        file_name = path.name
        file_name_no_ext = path.stem
        file_size = self._file_size(path)
        content_type = self._content_type(path)
        media_type = self._media_type(content_type, path.suffix)

        # Step 1 — create the mediaclip entity, always as a draft. Publishing
        # happens only once the file has actually landed.
        clip_data: dict[str, Any] = {
            "title": title or file_name_no_ext,
            "originalfilename": file_name,
            "mediatype": media_type,
            "usetype": use_type,
            "status": "draft",
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
        orphan_hint = (
            f"The empty mediaclip {clip_id} still exists — remove it with "
            f"client.delete('mediaclip', {clip_id!r})."
        )

        # Step 2 — create TUS upload with mediaclipId in metadata
        try:
            tus_data = self._tus_create(file_size, ",".join([
                f"filename {_b64(file_name)}",
                f"filetype {_b64(content_type)}",
                f"mediaclipId {_b64(clip_id)}",
            ]))
        except Exception as exc:
            raise SapiError(
                f"Could not start the upload for {file_name!r} after mediaclip "
                f"{clip_id} was created: {exc} {orphan_hint}"
            ) from exc

        tus_upload_id = tus_data["tusUploadId"]

        # Steps 3 and 4 — upload the chunks to S3, then finalise.
        completed = self._run_upload(
            path,
            tus_data,
            file_size,
            on_progress=on_progress,
            context=f"upload of {file_name!r} for mediaclip {clip_id}",
            orphan_hint=orphan_hint,
        )

        # Step 5 — the media is in place, so the clip can take its real status.
        if status != "draft":
            self._client.update("mediaclip", clip_id, {"status": status})

        return UploadResult(
            tus_upload_id=tus_upload_id,
            upload_identifier=completed.get("uploadIdentifier")
            or tus_data.get("uploadIdentifier", ""),
            file_name=file_name,
            file_size=file_size,
            content_type=content_type,
            s3_key=completed.get("key") or tus_data["s3"]["key"],
            mediaclip_id=clip_id,
            location=completed.get("location"),
        )

    def upload_status(self, tus_upload_id: str) -> UploadStatus:
        """
        Ask the server how much of an upload S3 already holds.

        Use this to resume an interrupted upload:
        :attr:`UploadStatus.uploaded_parts` lists the parts that do not need
        sending again.
        """
        url = f"{self._client._base_url}/sapi/tus/{tus_upload_id}"
        headers = {**self._client._auth.headers(), "Tus-Resumable": "1.0.0"}
        resp = self._client._session.head(
            url, headers=headers, timeout=self._client._timeout
        )
        if not resp.ok:
            raise SapiError(
                f"Could not read the status of TUS upload {tus_upload_id}: "
                f"HTTP {resp.status_code}."
            )

        # A HEAD response carries no body, so the server puts the S3 detail in
        # a header instead.
        try:
            tus_data = _json.loads(resp.headers.get("X-Tus-Data", "{}"))
        except ValueError:
            tus_data = {}
        s3_info = tus_data.get("s3", {}) if isinstance(tus_data, dict) else {}

        length_header = resp.headers.get("Upload-Length")
        return UploadStatus(
            tus_upload_id=tus_upload_id,
            offset=int(resp.headers.get("Upload-Offset", 0)),
            length=int(length_header) if length_header is not None else None,
            part_size=int(s3_info.get("partSize", 5 * 1024 * 1024)),
            uploaded_parts=s3_info.get("uploadedParts", []),
        )

    def sign_part(self, tus_upload_id: str, part_number: int) -> str:
        """Get a fresh presigned URL for one part, e.g. after the original expired."""
        body = self._client._sapi_request(
            "GET", f"/sapi/tus/{tus_upload_id}/sign/{part_number}"
        )
        url = body.get("url") if isinstance(body, dict) else None
        if not url:
            raise SapiError(
                f"Server returned no presigned URL for part {part_number} of "
                f"upload {tus_upload_id}: {body}."
            )
        return url

    def abort_upload(self, tus_upload_id: str) -> None:
        """
        Abort an upload and release the S3 parts already stored for it.

        Safe to call on an upload the server has already forgotten — a 404 is
        treated as "nothing left to clean up".
        """
        url = f"{self._client._base_url}/sapi/tus/{tus_upload_id}"
        headers = {**self._client._auth.headers(), "Tus-Resumable": "1.0.0"}
        resp = self._client._session.delete(
            url, headers=headers, timeout=self._client._timeout
        )
        if not resp.ok and resp.status_code != 404:
            raise SapiError(
                f"Could not abort TUS upload {tus_upload_id}: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _run_upload(
        self,
        path: Path,
        tus_data: dict[str, Any],
        file_size: int,
        *,
        on_progress: Optional[Callable[[int, int], None]],
        context: str,
        orphan_hint: str = "",
    ) -> dict[str, Any]:
        """
        Upload every chunk and finalise, aborting the S3 upload on any failure.

        Without the abort, a failed upload leaves its already-uploaded parts in
        the bucket until a lifecycle rule reaps them.
        """
        tus_upload_id = tus_data["tusUploadId"]
        try:
            parts = self._upload_chunks(
                path,
                tus_data["s3"],
                file_size,
                tus_upload_id,
                on_progress=on_progress,
            )
            return self._tus_complete(tus_upload_id, parts)
        except Exception as exc:
            try:
                self.abort_upload(tus_upload_id)
                cleanup = f"S3 multipart upload {tus_upload_id} was aborted."
            except Exception:
                # The original failure is the one worth reporting.
                cleanup = (
                    f"S3 multipart upload {tus_upload_id} could NOT be aborted "
                    f"— abort it with client.abort_upload({tus_upload_id!r})."
                )
            message = f"The {context} failed: {exc} {cleanup}"
            if orphan_hint:
                message = f"{message} {orphan_hint}"
            raise SapiError(message) from exc

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
        if isinstance(body, dict) and "tusUploadId" in body:
            return body
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        shape = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        raise SapiError(f"Unexpected TUS create response: {shape}")

    def _tus_complete(
        self, tus_upload_id: str, parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """POST /sapi/tus/{id}/complete — finalise the multipart upload."""
        if not parts:
            raise SapiError(
                f"Refusing to complete TUS upload {tus_upload_id} with no parts "
                f"— that would finalise an empty object."
            )

        url = f"{self._client._base_url}/sapi/tus/{tus_upload_id}/complete"
        headers = {
            **self._client._auth.headers(),
            "Tus-Resumable": "1.0.0",
        }
        # The endpoint reads $input['parts']; a bare array is read as no parts.
        resp = self._client._session.post(
            url,
            json={"parts": parts},
            headers=headers,
            timeout=self._client._timeout,
        )
        from bb_sapi.client import SapiClient
        body = SapiClient._handle_response(resp)

        # A 2xx that reports failure in its body would otherwise read as
        # success. SapiClient._analytics_request already guards this way.
        if isinstance(body, dict) and (
            body.get("error") or body.get("success") is False
        ):
            raise SapiError(
                f"TUS complete failed for upload {tus_upload_id} "
                f"(HTTP {resp.status_code}): {body.get('error', body)}"
            )
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------
    # S3 multipart upload
    # ------------------------------------------------------------------

    def _upload_chunks(
        self,
        path: Path,
        s3_info: dict[str, Any],
        file_size: int,
        tus_upload_id: Optional[str] = None,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[dict[str, Any]]:
        """
        Upload file chunks to S3 via presigned URLs.

        Returns a list of ``{PartNumber, ETag}`` dicts for the complete call.
        """
        presigned_urls: list[dict[str, Any]] = s3_info.get("presignedUrls") or []
        if not presigned_urls:
            raise SapiError(
                f"The server returned no presigned upload URLs for {path.name!r}; "
                f"nothing can be uploaded."
            )

        part_size: int = s3_info.get("partSize", 5 * 1024 * 1024)
        if part_size <= 0:
            raise SapiError(
                f"The server reported an unusable part size ({part_size}) for "
                f"{path.name!r}."
            )

        # The server decides how many parts there are; the byte offsets are
        # computed here. If the two disagree, the stored object would silently
        # not match the file.
        expected_parts = max(1, math.ceil(file_size / part_size))
        if len(presigned_urls) != expected_parts:
            raise SapiError(
                f"The server returned {len(presigned_urls)} presigned URLs but "
                f"{file_size} bytes at a part size of {part_size} needs "
                f"{expected_parts}. Refusing to upload {path.name!r} — the "
                f"stored object would not match the file."
            )

        parts: list[dict[str, Any]] = []
        bytes_uploaded = 0

        with open(path, "rb") as fh:
            for entry in presigned_urls:
                part_number: int = entry["partNumber"]
                url: str = entry["url"]
                start = (part_number - 1) * part_size
                end = min(start + part_size, file_size)
                if end <= start:
                    raise SapiError(
                        f"Part {part_number} maps to byte range [{start}, {end}) "
                        f"of {path.name!r} ({file_size} bytes) — the file changed "
                        f"size during the upload, or the server's part size "
                        f"disagrees with this SDK's."
                    )

                fh.seek(start)
                chunk = fh.read(end - start)
                if len(chunk) != end - start:
                    raise SapiError(
                        f"Expected {end - start} bytes for part {part_number} of "
                        f"{path.name!r} but read {len(chunk)} — the file changed "
                        f"size during the upload."
                    )

                etag = self._put_part(url, chunk, part_number, tus_upload_id)
                parts.append({"PartNumber": part_number, "ETag": etag})

                bytes_uploaded += len(chunk)
                if on_progress:
                    on_progress(bytes_uploaded, file_size)

        if bytes_uploaded != file_size:
            raise SapiError(
                f"Uploaded {bytes_uploaded} of {file_size} bytes for "
                f"{path.name!r}; refusing to finalise a truncated upload."
            )

        return parts

    def _put_part(
        self,
        url: str,
        chunk: bytes,
        part_number: int,
        tus_upload_id: Optional[str],
    ) -> str:
        """
        PUT one part to S3 and return its ETag.

        A 403 usually means the presigned URL outlived its TTL, which is
        repaired by asking the SAPI to sign the part again.
        """
        session = self._get_s3_session()
        headers = {"Content-Type": "application/octet-stream"}
        resp = session.put(
            url, data=chunk, headers=headers, timeout=self._client._timeout
        )

        if resp.status_code == 403 and tus_upload_id:
            resp = session.put(
                self.sign_part(tus_upload_id, part_number),
                data=chunk,
                headers=headers,
                timeout=self._client._timeout,
            )

        if not resp.ok:
            raise SapiError(
                f"S3 upload failed for part {part_number}: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )

        # S3 returns the ETag quoted ('"abc123"'). Keep it exactly as sent —
        # the value belongs to the server, not to us.
        etag = resp.headers.get("ETag", "")
        if not etag.strip().strip('"').strip():
            raise SapiError(
                f"S3 returned no usable ETag for part {part_number} "
                f"(raw header: {etag!r}); cannot complete the multipart upload."
            )
        return etag

    def _get_s3_session(self) -> _requests.Session:
        """
        A pooled session for S3 part uploads, retrying transient server errors.

        Deliberately separate from the SAPI session: that one may carry
        caller-supplied headers, and none of them belong in a request to S3.
        """
        if self._s3_session is None:
            session = _requests.Session()
            retry = Retry(
                total=self._S3_RETRIES,
                status_forcelist=list(self._S3_RETRY_STATUSES),
                allowed_methods=frozenset(["PUT"]),
                backoff_factor=0.5,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._s3_session = session
        return self._s3_session

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _file_size(self, path: Path) -> int:
        """Size of the file, rejecting the empty case before anything is sent."""
        size = path.stat().st_size
        if size == 0:
            raise SapiError(f"{path} is empty (0 bytes); there is nothing to upload.")
        return size

    def _content_type(self, path: Path) -> str:
        """
        MIME type for a file, used for the TUS ``filetype`` metadata.

        The table above wins where it has an entry; everything else falls back
        to :mod:`mimetypes`, which knows far more extensions than the table.
        """
        explicit = self._CONTENT_TYPES.get(path.suffix.lower())
        if explicit:
            return explicit
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"

    def _media_type(self, content_type: str, suffix: str = "") -> DerivedMediaType:
        """
        Map a MIME content type onto a SAPI ``mediatype``.

        The ``video``, ``audio``, ``image`` and ``font`` MIME families map to
        the mediatype of the same name, a few extensions whose MIME type hides
        the family are looked up directly, and everything else becomes a
        ``document``.

        This is a local best guess made when the entity is created, not a
        reimplementation of the backend: the backend also has an
        ``interactive`` mediatype, and it re-derives the type from the uploaded
        file during ingest, so the value sent here need not be the final one.
        Pass ``extra_fields={"mediatype": ...}`` to decide it yourself.
        """
        by_extension: Optional[DerivedMediaType] = self._MEDIA_TYPE_BY_EXTENSION.get(
            suffix.lower()
        )
        if by_extension:
            return by_extension
        for family in ("video", "audio", "image", "font"):
            if content_type.startswith(f"{family}/"):
                return family
        return "document"


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _b64(value: str) -> str:
    """Base64-encode a string value for TUS Upload-Metadata headers."""
    return base64.b64encode(value.encode()).decode()
