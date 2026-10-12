"""MVP-05: per-(group, jornada) ``porra`` replaces ``jornada.quini_config``

Revision ID: c1d2e3f4a5b6
Revises: b2c3d4e5f6a1
Create Date: 2026-10-05 21:00:00.000000

This migration is the schema side of the MVP-04 → MVP-05 evolution:

- Drops ``jornada.quini_config`` (the per-jornada quiniela mode config).
  Each peña now owns its own format (D doubles, T triples) and budget via
  the new :class:`Porra` table.
- Replaces ``pick.pleno_signs`` JSONB dict (M0/M1/M2) with the simpler
  ``pleno_local`` + ``pleno_visitante`` VARCHAR(2) pair. Each holds one
  value from ``{"0","1","2","M"}`` — the Quiniela real layout.
- Adds ``pick.porra_id`` FK to the new ``porra`` table. The legacy
  ``(group_id, user_id, jornada_id, partido_orden)`` UNIQUE stays
  enforced (the porra is 1:1 with ``(group, jornada)`` so the invariant
  is preserved).
- Replaces the old XOR constraint ``ck_pick_signs_xor_pleno`` with a
  new shape that splits by ``partido_orden``:

      partido_orden != 15 → signs IS NOT NULL AND pleno_local IS NULL AND pleno_visitante IS NULL
      partido_orden  = 15 → signs IS NULL AND pleno_local IS NOT NULL AND pleno_visitante IS NOT NULL

- Adds ``ck_pick_pleno_local_valid`` and ``ck_pick_pleno_visitante_valid``
  constraints (each ∈ {"0","1","2","M"}).
- Creates the ``porra`` table with the full set of CHECK constraints
  described in ``app/db/models.py``.

DATA MIGRATION:
This migration is dev-only and destructive. No data migration is performed:
``pick.pleno_signs`` is dropped without backfill; the ``pleno_local`` /
``pleno_visitante`` columns start as NULL everywhere; any pre-existing
pick rows would fail the new XOR constraint and need to be wiped.

In dev, this is fine: ``tests/conftest.py`` and the migration tests
rebuild state from scratch. In production this would be a separate
``data migration`` job (out of scope here).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: drop MVP-04 quini_config, evolve pick, add porra."""

    # --- 1. jornada.quini_config: drop the per-jornada quiniela-mode JSONB.
    op.drop_column("jornada", "quini_config")

    # --- 2. pick.pleno_signs: drop the M0/M1/M2 JSONB dict.
    op.drop_constraint("ck_pick_signs_xor_pleno", "pick", type_="check")
    op.drop_column("pick", "pleno_signs")

    # --- 3. pick: add the new pleno columns + porra FK.
    # All nullable: the new CHECK constraint validates the cross-column shape.
    op.add_column(
        "pick",
        sa.Column("pleno_local", sa.String(length=2), nullable=True),
    )
    op.add_column(
        "pick",
        sa.Column("pleno_visitante", sa.String(length=2), nullable=True),
    )
    op.add_column(
        "pick",
        sa.Column("porra_id", sa.Integer(), nullable=True),
    )

    # --- 4. pick: create the new porra table FIRST so we can wire the FK
    # (the FK is added AFTER the porra table exists, with CASCADE).
    op.create_table(
        "porra",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("jornada_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("budget_eur", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column(
            "dobles",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "triples",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("precio_apuesta_eur", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'open'"),
            nullable=False,
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
        sa.Column("boleto_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("boleto_set_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "boleto_matches",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("pleno_local", sa.String(length=2), nullable=True),
        sa.Column("pleno_visitante", sa.String(length=2), nullable=True),
        sa.CheckConstraint("dobles >= 0 AND triples >= 0", name="ck_porra_dt_nonneg"),
        sa.CheckConstraint(
            "dobles + triples <= 14",
            name="ck_porra_dt_within_partidos",
        ),
        sa.CheckConstraint("budget_eur > 0", name="ck_porra_budget_positive"),
        sa.CheckConstraint(
            "precio_apuesta_eur > 0",
            name="ck_porra_precio_positive",
        ),
        sa.CheckConstraint(
            "boleto_matches IS NULL OR jsonb_array_length(boleto_matches) = 14",
            name="ck_porra_boleto_matches_count",
        ),
        sa.CheckConstraint(
            "pleno_local IS NULL OR pleno_local IN ('0','1','2','M')",
            name="ck_porra_pleno_local_valid",
        ),
        sa.CheckConstraint(
            "pleno_visitante IS NULL OR pleno_visitante IN ('0','1','2','M')",
            name="ck_porra_pleno_visitante_valid",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["group.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["jornada_id"], ["jornada.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["user.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["boleto_set_by_user_id"], ["user.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "jornada_id", name="uq_porra_group_jornada"),
    )
    op.create_index(
        op.f("ix_porra_group_id"), "porra", ["group_id"], unique=False,
    )
    op.create_index(
        op.f("ix_porra_jornada_id"), "porra", ["jornada_id"], unique=False,
    )
    op.create_index(
        op.f("ix_porra_created_by_user_id"),
        "porra",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_porra_group_jornada", "porra", ["group_id", "jornada_id"], unique=False,
    )

    # --- 5. pick.porra_id: now wire the FK + index (CASCADE).
    op.create_foreign_key(
        "fk_pick_porra_id_porra",
        "pick",
        "porra",
        ["porra_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_pick_porra_id"), "pick", ["porra_id"], unique=False,
    )
    op.create_index(
        "ix_pick_porra_user", "pick", ["porra_id", "user_id"], unique=False,
    )

    # --- 6. pick: the new XOR check splits by partido_orden.
    op.create_check_constraint(
        "ck_pick_signs_xor_pleno",
        "pick",
        "(partido_orden != 15 AND signs IS NOT NULL "
        "AND pleno_local IS NULL AND pleno_visitante IS NULL) "
        "OR (partido_orden = 15 AND signs IS NULL "
        "AND pleno_local IS NOT NULL AND pleno_visitante IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_pick_pleno_local_valid",
        "pick",
        "pleno_local IS NULL OR pleno_local IN ('0','1','2','M')",
    )
    op.create_check_constraint(
        "ck_pick_pleno_visitante_valid",
        "pick",
        "pleno_visitante IS NULL OR pleno_visitante IN ('0','1','2','M')",
    )

    # --- 7. pick.porra_id: make NOT NULL now that the FK is in place.
    # (Added nullable so the FK could be created without breaking any
    # pre-existing rows; dev data is wiped before this point.)
    op.alter_column("pick", "porra_id", nullable=False)


def downgrade() -> None:
    """Downgrade schema: reverse the MVP-05 evolution (destructive)."""

    # Reverse 7 + 6 + 5: drop porra_id FK + new constraints.
    op.alter_column("pick", "porra_id", nullable=True)
    op.drop_constraint("ck_pick_pleno_visitante_valid", "pick", type_="check")
    op.drop_constraint("ck_pick_pleno_local_valid", "pick", type_="check")
    op.drop_index("ix_pick_porra_user", table_name="pick")
    op.drop_index(op.f("ix_pick_porra_id"), table_name="pick")
    op.drop_constraint("fk_pick_porra_id_porra", "pick", type_="foreignkey")
    op.drop_column("pick", "porra_id")
    op.drop_constraint("ck_pick_signs_xor_pleno", "pick", type_="check")

    # Reverse 4: drop the porra table.
    op.drop_index("ix_porra_group_jornada", table_name="porra")
    op.drop_index(op.f("ix_porra_created_by_user_id"), table_name="porra")
    op.drop_index(op.f("ix_porra_jornada_id"), table_name="porra")
    op.drop_index(op.f("ix_porra_group_id"), table_name="porra")
    op.drop_table("porra")

    # Reverse 3: drop the new pick columns.
    op.drop_column("pick", "pleno_visitante")
    op.drop_column("pick", "pleno_local")

    # Reverse 2: re-add pleno_signs + the old XOR constraint.
    op.add_column(
        "pick",
        sa.Column(
            "pleno_signs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_pick_signs_xor_pleno",
        "pick",
        "(signs IS NOT NULL AND pleno_signs IS NULL) OR "
        "(signs IS NULL AND pleno_signs IS NOT NULL)",
    )

    # Reverse 1: re-add jornada.quini_config.
    op.add_column(
        "jornada",
        sa.Column(
            "quini_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )