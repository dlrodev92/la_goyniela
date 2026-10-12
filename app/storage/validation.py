"""Image-upload validation helpers.

Two checks run for every uploaded image:

1. ``content_type`` must be one of PNG / JPEG / WebP (415 otherwise).
2. Raw size must be at most :data:`MAX_SIZE_BYTES` (413 otherwise).
3. The first bytes of the body must match the canonical magic bytes for the
   declared content type (400 otherwise). This catches the common attack
   where a client claims ``image/png`` but uploads an HTML page or a script.

These helpers raise :class:`fastapi.HTTPException` directly so the endpoint
code is ``await validate_image(...)`` and nothing else.
"""
from __future__ import annotations

from fastapi import HTTPException, status as _status

#: Allowed MIME types for upload. Anything else is 415.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})

#: Max body size — 5 MB. Anything bigger is 413.
MAX_SIZE_BYTES: int = 5 * 1024 * 1024


#: Maps a content type to the file extension we use when storing the file.
CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


def extension_for_content_type(content_type: str) -> str:
    """Return the file extension for a given MIME type.

    Falls back to ``"bin"`` for unknown types so the caller always has something
    to put in the storage path. Unknown types should already have been rejected
    by :func:`validate_image`.
    """
    return CONTENT_TYPE_TO_EXT.get(content_type, "bin")


def validate_image(data: bytes, content_type: str) -> None:
    """Validate image bytes + content type, or raise HTTPException.

    Order matters: size is checked before magic bytes so an oversized payload
    still gets the right 413 rather than a 400.

    Raises
    ------
    HTTPException
        - 415 if ``content_type`` is not in :data:`ALLOWED_CONTENT_TYPES`.
        - 413 if ``len(data)`` exceeds :data:`MAX_SIZE_BYTES`.
        - 400 if magic bytes don't match the declared content type.
    """
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported content type: {content_type!r}",
        )
    if len(data) > MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=_status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"file too large: {len(data)} bytes (max {MAX_SIZE_BYTES})",
        )

    if content_type == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(
                status_code=_status.HTTP_400_BAD_REQUEST,
                detail="PNG magic bytes missing",
            )
    elif content_type == "image/jpeg":
        if not data.startswith(b"\xff\xd8\xff"):
            raise HTTPException(
                status_code=_status.HTTP_400_BAD_REQUEST,
                detail="JPEG magic bytes missing",
            )
    elif content_type == "image/webp":
        # WebP is ``RIFF<4 bytes size>WEBP...`` — the literal "WEBP" should
        # appear in the first 12 bytes (after the 4-byte "RIFF" + 4-byte size).
        if not (data.startswith(b"RIFF") and b"WEBP" in data[:12]):
            raise HTTPException(
                status_code=_status.HTTP_400_BAD_REQUEST,
                detail="WebP magic bytes missing",
            )