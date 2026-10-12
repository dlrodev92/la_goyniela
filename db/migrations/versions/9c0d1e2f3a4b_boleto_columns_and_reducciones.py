"""Boleto N-column support + reducciones table (R1-R6)

Continuation of the intuicion + PDF sprint. Adds:

- ``porra.boleto_columns JSONB NULLABLE`` — new column for N-column boletos.
  Each entry is ``{"matches": [{"partido_orden": int, "signs": [str, ...]}]}``
  (14 rows per column). The legacy ``boleto_matches`` is left nullable
  for rollback but should NOT be used by new code.
- ``porra.reduccion VARCHAR(4) NULLABLE`` — only set when ``modo='reducida'``
  (values "R1".."R6" from the official Quiniela table).

Data migration: any existing row with ``boleto_matches IS NOT NULL`` is
copied to ``boleto_columns`` as a single-column entry.

Constraint: ``ck_porra_reduccion_only_reducida`` enforces that the
column is non-null only when ``modo='reducida'``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c0d1e2f3a4b"
down_revision = "8b9c0d1e2f3a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Add the new columns.
    op.add_column(
        "porra",
        sa.Column("boleto_columns", sa.JSON, nullable=True),
    )
    op.add_column(
        "porra",
        sa.Column("reduccion", sa.String(length=4), nullable=True),
    )

    # 2) Backfill: any porra with the legacy single-column boleto
    #    gets its 14 rows wrapped in a one-element list under
    #    ``boleto_columns``. Idempotent (the SELECT skips already-set rows).
    op.execute(
        """
        UPDATE porra
        SET boleto_columns = jsonb_build_array(
            jsonb_build_object('matches', boleto_matches)
        )
        WHERE boleto_matches IS NOT NULL
          AND boleto_columns IS NULL
        """
    )

    # 3) CHECK: reduccion only when modo=reducida.
    op.create_check_constraint(
        "ck_porra_reduccion_only_reducida",
        "porra",
        "(modo <> 'reducida') OR (reduccion IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_porra_reduccion_only_reducida", "porra", type_="check")
    op.drop_column("porra", "reduccion")
    op.drop_column("porra", "boleto_columns")
