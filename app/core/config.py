"""Application settings loaded from environment variables.

Single instance `settings` imported across the app. Pydantic-settings validates
types and required fields at startup, so missing env vars fail loudly.
"""
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    env: str = Field(default="dev", description="dev|staging|prod")
    database_url: str = Field(
        default="postgresql+psycopg://la_goyniela:la_goyniela@localhost:5433/la_goyniela",
        description="SQLAlchemy async URL. Default targets the docker-compose dev DB on port 5433.",
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _coerce_psycopg_driver(cls, v: str) -> str:
        """Translate ``postgresql://`` → ``postgresql+psycopg://``.

        The app uses the async psycopg3 driver everywhere. Hosting platforms
        (Railway, Heroku, Render, etc.) usually inject ``DATABASE_URL`` as
        a plain ``postgresql://...`` URL. SQLAlchemy would then try to load
        psycopg2 (the sync driver), which we don't ship. Rewrite the driver
        to psycopg3 on the way in.
        """
        if isinstance(v, str) and v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v
    log_level: str = Field(default="INFO")
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    api_football_key: str | None = Field(default=None, description="API-Football key (read at runtime by clients)")

    # Auth
    jwt_secret: str = Field(default="dev-only-secret-change-me-in-production-32chars-min")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expiration_minutes: int = Field(default=1440, ge=5, le=10080)  # 5 min to 7 days

    # OAuth providers — placeholder, user fills in their real Client IDs from
    # Google Cloud Console and X Developer Portal
    google_client_id: str = Field(default="dev-placeholder-replace-with-real-google-client-id")
    x_client_id: str = Field(default="dev-placeholder-replace-with-real-x-client-id")
    x_client_secret: str = Field(default="dev-placeholder-replace-with-real-x-client-secret")

    # Storage backend. 'local' = disk under ``storage_local_path`` served at
    # ``storage_public_base_url``. 's3' = any S3-compatible bucket (AWS,
    # MinIO, LocalStack) configured via the ``storage_s3_*`` vars below.
    storage_backend: str = Field(
        default="local",
        description="'local' (disk + StaticFiles mount) or 's3' (boto3)",
    )
    storage_public_base_url: str = Field(
        default="http://localhost:8000/static/uploads",
        description="URL prefix used to resolve storage keys for the local backend",
    )
    storage_s3_public_base_url: str | None = Field(
        default=None,
        description=(
            "Optional override for the S3 backend's public URL. Use this "
            "to serve R2 (or any S3-compatible) under a custom domain like "
            "`https://cdn.la-goyniela.example`. When unset, the URL is "
            "built from the endpoint + bucket (path-style) for R2/MinIO, "
            "or the AWS virtual-hosted style for AWS S3."
        ),
    )
    storage_local_path: str = Field(
        default="./static/uploads",
        description="Filesystem directory the local backend writes under",
    )
    storage_s3_bucket: str | None = Field(
        default=None, description="S3 bucket name (required when STORAGE_BACKEND=s3)",
    )
    storage_s3_region: str | None = Field(
        default=None, description="S3 region (e.g. 'eu-west-1'; required when STORAGE_BACKEND=s3)",
    )
    storage_s3_access_key: str | None = Field(
        default=None, description="S3 access key (required when STORAGE_BACKEND=s3)",
    )
    storage_s3_secret_key: str | None = Field(
        default=None, description="S3 secret key (required when STORAGE_BACKEND=s3)",
    )
    storage_s3_endpoint_url: str | None = Field(
        default=None,
        description=(
            "Optional S3-compatible endpoint (MinIO, LocalStack). "
            "Example: http://localhost:9000"
        ),
    )


settings = Settings()