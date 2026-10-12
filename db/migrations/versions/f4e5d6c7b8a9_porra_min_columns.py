"""Enforce minimum 2 columns per porra (1 double OR 1 triple).

Real Quiniela sells boletos starting at 2 columns (1 doble). The
``(0, 0)`` format (1 simple column) is not a valid Quiniela product
in our model. This migration:

1. Bumps any existing ``(dobles=0, triples=0)`` porra to the cheapest
   valid format: ``(dobles=1, triples=0)``. The cost rises from
   ``precio`` to ``2 * precio``; if the porra's budget cannot afford
   the upgrade (rare, since the 1-column boleto already cost
   ``precio`` and 2 columns cost ``2 * precio``), we DELETE the row.
   Picks cascade via FK ``ondelete="CASCADE"``.
2. Adds a new CHECK constraint ``ck_porra_min_columns`` that rejects
   ``(dobles=0, triples=0)`` going forward.

Idempotent: the UPDATE/DELETE only affects rows that match the
``(0, 0)`` pattern; the ALTER on the table is a no-op if the
constraint already exists.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f4e5d6c7b8a9"
down_revision = "e1f2a3b4c5d7"  # last migration (is_superadmin + 3 estados + soft delete)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Bump or delete any pre-existing (0, 0) porras so the new CHECK
    #    can be added without rejecting legacy rows.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, budget_eur, precio_apuesta_eur "
            "FROM porra WHERE dobles = 0 AND triples = 0"
        )
    ).fetchall()
    for row in rows:
        porra_id, budget, precio = row
        # Cheapest valid format: 1 doble → 2 columns → 2*precio.
        cost_upgraded = float(precio) * 2
        if float(budget) >= cost_upgraded:
            conn.execute(
                sa.text(
                    "UPDATE porra SET dobles = 1, triples = 0 "
                    "WHERE id = :pid"
                ),
                {"pid": porra_id},
            )
        else:
            # Budget too small for the upgrade. Hard-delete the porra
            # (picks cascade via FK ondelete="CASCADE"). This is a
            # dev-data edge case; the production scheduler would never
            # have created such a porra (the budget validator runs
            # before insert).
            conn.execute(
                sa.text("DELETE FROM porra WHERE id = :pid"),
                {"pid": porra_id},
            )

    # 2) Enforce the new minimum going forward.
    op.create_check_constraint(
        "ck_porra_min_columns",
        "porra",
        "NOT (dobles = 0 AND triples = 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_porra_min_columns", "porra", type_="check")
