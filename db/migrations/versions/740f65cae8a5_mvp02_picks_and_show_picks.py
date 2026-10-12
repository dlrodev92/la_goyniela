"""MVP-02: pick table + group.show_picks

Revision ID: 740f65cae8a5
Revises: e4315e0408cf
Create Date: 2026-10-04 13:00:00.000000

Adds:
- ``group.show_picks BOOLEAN NOT NULL DEFAULT FALSE`` (admin toggles whether
  members can see each other's picks for a jornada).
- ``pick`` table — one row per (group, user, jornada, partido_orden) for
  partido 1..15. Partido 1..14 use ``sign`` ("1" | "X" | "2"). Partido 15
  (Pleno al 15) uses ``pleno_signs`` JSONB with keys M0/M1/M2.

Cascade rules:
- ``group_id`` and ``jornada_id`` → CASCADE (deleting the peña or the jornada
  drops the picks).
- ``user_id`` → RESTRICT (we should never silently delete a peña member who
  has picks; remove their memberships / picks first).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "740f65cae8a5"
down_revision: Union[str, Sequence[str], None] = "e4315e0408cf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- group.show_picks: admin toggle for whether members can see each
    # other's picks. Defaults to FALSE so existing groups keep current
    # behavior (privacy-first).
    op.add_column(
        "group",
        sa.Column(
            "show_picks",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    # --- pick table: per-member per-jornada per-partido picks.
    # See app/db/models.py: ``class Pick`` for the rationale on each
    # constraint. The CHECK constraints below mirror the model exactly so
    # direct INSERTs that bypass the ORM still enforce the same invariants.
    op.create_table(
        "pick",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("jornada_id", sa.Integer(), nullable=False),
        sa.Column("partido_orden", sa.SmallInteger(), nullable=False),
        sa.Column("sign", sa.String(length=2), nullable=True),
        sa.Column(
            "pleno_signs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "partido_orden >= 1 AND partido_orden <= 15",
            name="ck_pick_partido_orden_range",
        ),
        sa.CheckConstraint(
            "(sign IS NOT NULL AND pleno_signs IS NULL) OR "
            "(sign IS NULL AND pleno_signs IS NOT NULL)",
            name="ck_pick_sign_xor_pleno",
        ),
        sa.CheckConstraint(
            "sign IS NULL OR sign IN ('1','X','2')",
            name="ck_pick_sign_valid",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["group.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["jornada_id"], ["jornada.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "user_id",
            "jornada_id",
            "partido_orden",
            name="uq_pick_member_partido",
        ),
    )
    op.create_index(
        op.f("ix_pick_group_id"), "pick", ["group_id"], unique=False,
    )
    op.create_index(
        op.f("ix_pick_user_id"), "pick", ["user_id"], unique=False,
    )
    op.create_index(
        op.f("ix_pick_jornada_id"), "pick", ["jornada_id"], unique=False,
    )
    op.create_index(
        "ix_pick_group_jornada", "pick", ["group_id", "jornada_id"], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_pick_group_jornada", table_name="pick")
    op.drop_index(op.f("ix_pick_jornada_id"), table_name="pick")
    op.drop_index(op.f("ix_pick_user_id"), table_name="pick")
    op.drop_index(op.f("ix_pick_group_id"), table_name="pick")
    op.drop_table("pick")
    op.drop_column("group", "show_picks")