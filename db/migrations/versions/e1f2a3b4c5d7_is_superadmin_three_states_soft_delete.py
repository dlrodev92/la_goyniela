"""is_superadmin + 3 estados + soft delete infra

Slice 1 of the superadmin-panel sprint:

- ``user.is_superadmin`` column (NOT NULL, default false) + promote david
  lopez (the project owner) so the upcoming admin endpoints in slice 2
  have a known superadmin to gate on. Email is hard-coded here on
  purpose — slice 2 introduces a proper "promote" endpoint that lets
  existing superadmins grant the role to other users; until that ships
  the owner needs a bootstrap path.
- reduce ``JornadaStatus`` from 4 to 3 states (draft, active, past).
  Idempotent ``UPDATE`` renames any pre-existing ``closed`` or
  ``completed`` rows to ``past``; verified 0 such rows exist in the
  dev DB today (draft=5, no others), so the UPDATE is a no-op but
  stays for safety on environments with prod data.
- soft delete infra: ``deleted_at`` (tz-aware, nullable) on
  ``team_identity_map`` and ``jornada``, both with a partial-ish B-tree
  index (``ix_<table>_deleted_at``) for the public read endpoints to
  filter by. Slice 2 introduces the actual DELETE endpoints; this
  migration only adds the storage + index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_superadmin + deleted_at columns; collapse 4 jornada states to 3."""
    op.add_column(
        "user",
        sa.Column(
            "is_superadmin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE \"user\" SET is_superadmin = true "
        "WHERE email = 'dlro92trader@gmail.com'"
    )
    op.execute(
        "UPDATE jornada SET status = 'past' "
        "WHERE status IN ('closed', 'completed')"
    )
    op.add_column(
        "team_identity_map",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_team_identity_deleted_at",
        "team_identity_map",
        ["deleted_at"],
    )
    op.add_column(
        "jornada",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_jornada_deleted_at",
        "jornada",
        ["deleted_at"],
    )


def downgrade() -> None:
    """Reverse the is_superadmin column, the deleted_at columns, and the
    JornadaStatus collapse (best-effort: rows created as 'past' in this
    migration's lifetime go back to 'closed')."""
    op.drop_index("ix_jornada_deleted_at", "jornada")
    op.drop_column("jornada", "deleted_at")
    op.drop_index("ix_team_identity_deleted_at", "team_identity_map")
    op.drop_column("team_identity_map", "deleted_at")
    op.execute(
        "UPDATE jornada SET status = 'closed' WHERE status = 'past'"
    )
    op.drop_column("user", "is_superadmin")
