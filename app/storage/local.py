"""Local-disk storage backend.

Writes files under ``settings.storage_local_path`` (default ``./static/uploads``)
and exposes them at :class:`app.storage.base.StorageBackend` interface.

This is the default backend in dev / test — no AWS account required. In
``app.main`` we additionally mount the directory at ``/static/uploads`` so the
``get_public_url`` output resolves over plain HTTP when running locally.

Public URL format
-----------------
``{storage_public_base_url}/{key}`` — e.g.
``http://localhost:8000/static/uploads/teams/foo.png``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import settings
from app.storage.base import StorageBackend


class LocalStorage(StorageBackend):
    """Storage backend that writes to a local directory.

    Parameters
    ----------
    base_path
        Absolute or relative directory that file keys are written under.
        Created with ``parents=True, exist_ok=True`` on construction.
    public_base_url
        URL prefix that, when combined with the key, resolves to a public
        location on the web. In dev this is usually
        ``http://localhost:8000/static/uploads``.
    """

    def __init__(self, base_path: str | Path, public_base_url: str) -> None:
        self._base_path = Path(base_path)
        self._public_base_url = public_base_url.rstrip("/")
        self._base_path.mkdir(parents=True, exist_ok=True)

    async def save(self, key: str, data: bytes, content_type: str) -> str:
        """Write ``data`` to ``base_path / key``. Returns the public URL."""
        target = self._base_path / key
        await asyncio.to_thread(self._write, target, data)
        return self.get_public_url(key)

    @staticmethod
    def _write(target: Path, data: bytes) -> None:
        """Sync write helper (offloaded via ``asyncio.to_thread``).

        Creates parent directories on demand so callers don't have to pre-create
        ``teams/`` or ``users/`` before the first upload.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def delete(self, key: str) -> None:
        """Remove the file at ``base_path / key``. No-op if absent."""
        target = self._base_path / key
        await asyncio.to_thread(self._remove, target)

    @staticmethod
    def _remove(target: Path) -> None:
        try:
            target.unlink()
        except FileNotFoundError:
            # No-op on missing files — cleanup paths are best-effort.
            return

    def get_public_url(self, key: str) -> str:
        """Pure function. No HTTP call."""
        return f"{self._public_base_url}/{key}"


def _default_local_storage() -> LocalStorage:
    """Build a :class:`LocalStorage` from current :class:`Settings`.

    Used by :func:`app.storage.factory.get_storage` when ``storage_backend ==
    'local'``. Kept as a thin wrapper so tests can inject a custom path by
    monkeypatching ``settings`` (or by calling ``LocalStorage(...)`` directly).
    """
    return LocalStorage(
        base_path=settings.storage_local_path,
        public_base_url=settings.storage_public_base_url,
    )