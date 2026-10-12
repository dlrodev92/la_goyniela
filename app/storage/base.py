"""Abstract storage backend.

All file uploads in the app (team logos, user avatars, ...) go through this
interface. Adding a new backend (Azure Blob, GCS, IPFS, ...) means writing one
new concrete class and registering it in :mod:`app.storage.factory` — the
endpoints and the rest of the code do not change.

The contract is intentionally narrow:

- ``save`` writes bytes and returns the public URL where the file can be fetched.
- ``delete`` removes an object (no-op if it doesn't exist).
- ``get_public_url`` is a pure function — no HTTP call.

Keys are relative paths like ``teams/club-men-fc-barcelona.png`` or
``users/42.webp``. The backend is responsible for prefixing with the bucket /
directory and for the public-facing URL format.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Asynchronous storage backend interface."""

    @abstractmethod
    async def save(
        self,
        key: str,
        data: bytes,
        content_type: str,
    ) -> str:
        """Store ``data`` at ``key``.

        Parameters
        ----------
        key
            Relative path (no leading slash). Example: ``teams/foo.png``.
        data
            Raw file contents.
        content_type
            MIME type — used to set ``Content-Type`` on the stored file so the
            client can ``GET`` it and have the browser render the right format.

        Returns
        -------
        str
            The public URL where the file can be fetched (no auth required).

        Notes
        -----
        Idempotency: if the key already exists, overwrite it. The caller is
        responsible for cleaning up the old key on a different-object upload
        (call :meth:`delete` on the previous key first).
        """

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the object at ``key``. No-op if it doesn't exist."""

    @abstractmethod
    def get_public_url(self, key: str) -> str:
        """Return the public URL for ``key`` (no HTTP call — pure function)."""