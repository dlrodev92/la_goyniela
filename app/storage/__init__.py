"""Storage backend abstraction.

Two implementations live in this package:

- :class:`LocalStorage` — writes under a local directory and serves the files
  via FastAPI's ``StaticFiles`` mount.
- :class:`S3Storage` — wraps ``boto3`` calls (sync) inside ``asyncio.to_thread``
  so the FastAPI request loop is not blocked.

Both implement :class:`StorageBackend`, the abstract interface every endpoint
that stores user-facing files goes through. The factory :func:`get_storage`
picks the right one based on ``settings.storage_backend`` so changing the env
var ``STORAGE_BACKEND`` is the only knob needed to swap implementations.
"""
from __future__ import annotations

from app.storage.base import StorageBackend
from app.storage.factory import get_storage
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage

__all__ = ["StorageBackend", "LocalStorage", "S3Storage", "get_storage"]