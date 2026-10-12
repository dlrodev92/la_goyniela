"""Porra.modo + fijos + columnas — 4th mode: intuicion

Slice 1 of the intuicion + PDF sprint. Adds:

- ``porra.modo``: enum string (sencilla | multiple | reducida | intuicion),
  default 'multiple' so all existing porras stay in the multiple bucket
  (the legacy implementation).
- ``porra.fijos``: int nullable, only meaningful when modo='intuicion'.
- ``porra.columnas``: int nullable, only meaningful when modo='intuicion'
  (and eventually 'sencilla'; multiple/reducida keep using the computed
  2^D × 3^T or reduction count).
- Relaxes ``ck_porra_min_columns`` (migration ``f4e5d6c7b8a9``) from
  "always NOT (D=0 AND T=0)" to "only for multiple/reducida modes".
  The ``intuicion`` and ``sencilla`` modes legitimately carry
  ``dobles=triples=0`` (the column count is ``porra.columnas``, not
  2^D × 3^T). The original migration file stays untouched; we drop
  the constraint here and recreate it mode-aware.

Idempotent: backfills 'multiple' for existing rows so the NOT NULL
constraint on ``modo`` is satisfied.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "8b9c0d1e2f3a"
down_revision = "f4e5d6c7b8a9"  # last migration (ck_porra_min_columns)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add the modo enum (stored as VARCHAR, native_enum=False).
    op.add_column(
        "porra",
        sa.Column("modo", sa.String(length=16), nullable=False,
                  server_default="multiple"),
    )
    # 2) Add fijos + columnas as nullable ints.
    op.add_column(
        "porra",
        sa.Column("fijos", sa.SmallInteger, nullable=True),
    )
    op.add_column(
        "porra",
        sa.Column("columnas", sa.SmallInteger, nullable=True),
    )
    # 3) Relax ck_porra_min_columns: it used to reject (D=0, T=0) for
    #    every porra. With ``modo='intuicion'`` (and soon ``sencilla``)
    #    that pair is the legitimate shape (the column count comes from
    #    ``porra.columnas``, not 2^D × 3^T). Drop and recreate the
    #    constraint so it only fires for multiple/reducida.
    op.drop_constraint("ck_porra_min_columns", "porra", type_="check")
    op.create_check_constraint(
        "ck_porra_min_columns",
        "porra",
        # Only enforce when modo is multiple or reducida. The other
        # modes carry their own shape (columnas + fijos for intuicion;
        # soon just columnas for sencilla).
        "(modo IN ('sencilla', 'intuicion')) OR "
        "(dobles > 0 OR triples > 0)",
    )
    # 4) Check constraint: when modo='intuicion', fijos and columnas must
    #    be set. When other modes, both can be NULL.
    op.create_check_constraint(
        "ck_porra_intuicion_fields",
        "porra",
        "(modo <> 'intuicion') OR (fijos IS NOT NULL AND columnas IS NOT NULL)",
    )


def downgrade() -> None:
    # Roll back the constraint relaxation first.
    op.drop_constraint("ck_porra_intuicion_fields", "porra", type_="check")
    op.drop_constraint("ck_porra_min_columns", "porra", type_="check")
    op.create_check_constraint(
        "ck_porra_min_columns",
        "porra",
        "NOT (dobles = 0 AND triples = 0)",
    )
    op.drop_column("porra", "columnas")
    op.drop_column("porra", "fijos")
    op.drop_column("porra", "modo")