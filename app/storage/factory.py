"""Storage backend factory.

Returns a singleton :class:`StorageBackend` based on the
``STORAGE_BACKEND`` env var (``'local'`` or ``'s3'``). All endpoint code
depends on this — never instantiate :class:`LocalStorage` /
:class:`S3Storage` directly outside the factory.

The singleton is rebuilt when ``STORAGE_BACKEND`` changes, which makes it
trivially testable via :func:`pytest.MonkeyPatch.setenv` (see
``tests/test_image_uploads.py``).
"""
from __future__ import annotations

from app.core.config import settings
from app.storage.base import StorageBackend
from app.storage.local import LocalStorage
from app.storage.s3 import S3Storage


_BACKENDS = ("local", "s3")


def get_storage() -> StorageBackend:
    """Return the storage backend for the current ``settings.storage_backend``.

    Raises
    ------
    RuntimeError
        If ``settings.storage_backend`` is not one of ``'local'`` / ``'s3'``,
        or if S3 is selected but the required credentials are missing.
    """
    backend = (settings.storage_backend or "local").lower()

    if backend == "local":
        return LocalStorage(
            base_path=settings.storage_local_path,
            public_base_url=settings.storage_public_base_url,
        )

    if backend == "s3":
        missing = [
            k
            for k, v in (
                ("STORAGE_S3_BUCKET", settings.storage_s3_bucket),
                ("STORAGE_S3_REGION", settings.storage_s3_region),
                ("STORAGE_S3_ACCESS_KEY", settings.storage_s3_access_key),
                ("STORAGE_S3_SECRET_KEY", settings.storage_s3_secret_key),
            )
            if not v
        ]
        if missing:
            raise RuntimeError(
                f"STORAGE_BACKEND=s3 requires env vars: {', '.join(missing)}"
            )
        return S3Storage(
            bucket=settings.storage_s3_bucket or "",
            region=settings.storage_s3_region or "",
            access_key=settings.storage_s3_access_key or "",
            secret_key=settings.storage_s3_secret_key or "",
            endpoint_url=settings.storage_s3_endpoint_url,
            # S3-specific public URL override. The general
            # ``storage_public_base_url`` is for the local backend and would
            # leak localhost URLs into the DB when R2 is the real backend.
            # Set this to your R2 custom domain (e.g.
            # ``https://cdn.la-goyniela.example``) to override the endpoint
            # or AWS-style URL.
            public_base_url=settings.storage_s3_public_base_url,
        )

    raise RuntimeError(
        f"Unknown STORAGE_BACKEND={settings.storage_backend!r}; "
        f"expected one of {_BACKENDS}"
    )