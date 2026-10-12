"""Image uploads: user.avatar_url already exists; this migration is a no-op.

Revision ID: a1b2c3d4e5f6
Revises: 740f65cae8a5
Create Date: 2026-10-05 12:00:00.000000

The :class:`app.db.models.User` model already has ``avatar_url`` (Text, nullable)
from the auth migration. This migration exists purely to anchor the schema
history so the storage / upload endpoints can be deployed without re-touching
the auth table.

``team_identity_map.logo_url`` (String(255), nullable) is also already present
from the catalog migration — no change needed there.

If a future revision needs to tighten ``user.avatar_url`` to VARCHAR(500), add
it here as ``op.alter_column('user', 'avatar_url', ...)``.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "740f65cae8a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    ``user.avatar_url`` was added in migration ``33906bd8fdea`` (auth_user_*).
    ``team_identity_map.logo_url`` was added in ``605f324ada7e`` (catalog).
    No DDL is required for the image-uploads feature.
    """
    pass


def downgrade() -> None:
    """Downgrade schema. No-op — see upgrade()."""
    pass