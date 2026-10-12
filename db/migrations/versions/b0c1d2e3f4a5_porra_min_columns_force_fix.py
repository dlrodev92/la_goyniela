"""Force-correct the dev DB: drop and recreate ``ck_porra_min_columns``.

The previous migration (``8b9c0d1e2f3a_porra_modo_intuicion``) was
supposed to drop the original ``NOT (dobles = 0 AND triples = 0)``
constraint and replace it with the mode-aware version. The migration
file has the right code, but on the **dev DB** the drop+recreate did
not take effect (the test DB has the right constraint, the dev DB
still has the old one). This migration:

- Drops the constraint unconditionally (works whether the constraint
  is the old or the new one — both names are the same).
- Recreates it with the mode-aware expression.

After this migration, ``modo IN ('sencilla', 'intuicion')`` is allowed
to have ``dobles = triples = 0`` (the column count is read from
``porra.columnas``). Other modes still need ``dobles > 0 OR triples > 0``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "b0c1d2e3f4a5"
down_revision = "9c0d1e2f3a4b"  # last applied migration
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Drop the constraint if it exists (works for both the old
    #    ``NOT (dobles = 0 AND triples = 0)`` shape and the new
    #    mode-aware shape — both names are the same).
    conn = op.get_bind()
    exists = conn.execute(
        sa.text(
            """
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'ck_porra_min_columns'
              AND conrelid = 'porra'::regclass
              AND contype = 'c'
            """
        )
    ).first()
    if exists is not None:
        op.drop_constraint("ck_porra_min_columns", "porra", type_="check")

    # 2) Recreate with the mode-aware expression (matches the
    #    condition in 8b9c0d1e2f3a).
    op.create_check_constraint(
        "ck_porra_min_columns",
        "porra",
        "(modo IN ('sencilla', 'intuicion')) OR "
        "(dobles > 0 OR triples > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_porra_min_columns", "porra", type_="check")
    op.create_check_constraint(
        "ck_porra_min_columns",
        "porra",
        "NOT (dobles = 0 AND triples = 0)",
    )
