"""S3-compatible storage backend.

Uses ``boto3.client("s3", ...)`` for the actual API calls and offloads them to
a thread pool via :func:`asyncio.to_thread` so the FastAPI event loop is never
blocked by a sync boto3 round-trip.

Works with **AWS S3, Cloudflare R2, MinIO, and LocalStack** — they all expose
the same S3 API surface. Point ``STORAGE_S3_ENDPOINT_URL`` at the alternative
host and the rest of the code is identical. ACL is set to ``public-read`` so
the public URL returned by ``get_public_url`` works without auth (matches the
local-disk behavior).

Public URL resolution (in order of preference):

1. ``STORAGE_PUBLIC_BASE_URL`` (if set) — used as-is. **Required for R2**
   with a custom domain like ``https://cdn.example.com``.
2. If ``STORAGE_S3_ENDPOINT_URL`` is set (MinIO / LocalStack / R2 dev URL) —
   ``{endpoint}/{bucket}/{key}``.
3. Default — AWS S3 virtual-hosted style
   (``https://{bucket}.s3.{region}.amazonaws.com/{key}``).

Required env vars
-----------------
- ``STORAGE_S3_BUCKET``
- ``STORAGE_S3_REGION`` (use ``"auto"`` for R2)
- ``STORAGE_S3_ACCESS_KEY``
- ``STORAGE_S3_SECRET_KEY``

Optional env vars
-----------------
- ``STORAGE_S3_ENDPOINT_URL`` (for R2: ``https://<accountid>.r2.cloudflarestorage.com``;
  for MinIO: ``http://localhost:9000``)
- ``STORAGE_PUBLIC_BASE_URL`` (recommended for R2 with a custom domain;
  e.g. ``https://cdn.la-goyniela.example``)
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.storage.base import StorageBackend


class S3Storage(StorageBackend):
    """Storage backend for any S3-compatible bucket (AWS, Cloudflare R2,
    MinIO, LocalStack)."""

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        endpoint_url: str | None = None,
        public_base_url: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3Storage requires a non-empty bucket name")
        if not region:
            raise ValueError("S3Storage requires a non-empty region")
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url or None
        # If the user gave a public base URL (e.g. R2 with a custom domain),
        # we use it for all public URLs and ignore the AWS-style / endpoint
        # style URL generation below.
        self._public_base_url = (public_base_url or "").rstrip("/") or None

        # Imported lazily so the dependency is not loaded when running on the
        # local-disk backend (which doesn't need boto3 at all).
        import boto3  # noqa: WPS433 (lazy)

        self._client: Any = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=self._endpoint_url,
        )

    async def save(self, key: str, data: bytes, content_type: str) -> str:
        """Upload ``data`` to ``bucket/key``.

        We intentionally do NOT pass ``ACL='public-read'``: that's an
        AWS-S3-specific header. Cloudflare R2 ignores unknown ACL values
        and rejects the upload with ``AccessDenied`` instead. R2 uses a
        bucket-level "Allow Access" toggle (configured in the Cloudflare
        dashboard) for public access — no per-object ACL needed.

        AWS S3 buckets configured for public read will still work because
        their bucket policy covers the missing ACL header.
        """
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return self.get_public_url(key)

    async def delete(self, key: str) -> None:
        """Delete ``bucket/key``. No-op on missing objects."""
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=key,
        )

    def get_public_url(self, key: str) -> str:
        """Pure function. Returns the public URL where ``key`` is served.

        Order of preference:

        1. ``STORAGE_PUBLIC_BASE_URL`` (override; the canonical way to use
           R2 with a custom domain like ``https://cdn.example.com``).
        2. If ``STORAGE_S3_ENDPOINT_URL`` is set (MinIO / R2 without custom
           domain), build path-style: ``{endpoint}/{bucket}/{key}``.
        3. Default AWS S3 virtual-hosted style:
           ``https://{bucket}.s3.{region}.amazonaws.com/{key}``.
        """
        if self._public_base_url:
            return f"{self._public_base_url}/{key}"
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self._bucket}/{key}"
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{key}"