"""Jornada default columns + Pick multi-column

Adds:

- ``Jornada.default_columnas`` (SmallInteger, nullable, range 2..12).
  The admin sets this when configuring the jornada; it's the default
  ``columnas`` value for porras in ``sencilla`` or ``intuicion`` mode
  created for this jornada. Nullable so existing jornadas (and the
  ``multiple`` / ``reducida`` modes that don't use ``columnas``) are
  unaffected.

- ``Pick.columna_idx`` (SmallInteger, NOT NULL, default 1). The unique
  key moves from ``(porra_id, user_id, partido_orden)`` to
  ``(porra_id, user_id, partido_orden, columna_idx)``. Backfills existing
  rows with ``columna_idx=1`` so the migration is idempotent and the
  data is preserved.

- Drops the legacy ``uq_pick_member_partido`` constraint (on
  ``(group_id, user_id, jornada_id, partido_orden)``). It was a holdover
  from MVP-02 (pre-porra scoping) and would block the new multi-column
  shape, since each ``(user, porra, partido)`` tuple may now have N
  rows (one per column). The porra-scoped unique key
  (``uq_pick_porra_user_partido_col``) is the source of truth for
  per-member uniqueness going forward; ``group_id`` and ``jornada_id``
  remain on the row for index efficiency + FK-cascade alignment, but
  uniqueness is enforced at the porra scope.

Idempotent: ``columna_idx`` has a server default of ``1`` so the
NOT NULL constraint is satisfied for existing rows; the server default
is dropped afterwards so freshly-inserted rows must explicitly set
``columna_idx``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "c3d4e5f6a7b8"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Jornada.default_columnas — nullable SmallInteger in 2..12.
    op.add_column(
        "jornada",
        sa.Column("default_columnas", sa.SmallInteger, nullable=True),
    )
    op.create_check_constraint(
        "ck_jornada_default_columnas_range",
        "jornada",
        "default_columnas IS NULL OR (default_columnas >= 2 AND default_columnas <= 12)",
    )

    # 2) Pick.columna_idx with default 1 (idempotent backfill).
    op.add_column(
        "pick",
        sa.Column("columna_idx", sa.SmallInteger, nullable=False, server_default="1"),
    )
    op.alter_column("pick", "columna_idx", server_default=None)

    # 3) Replace the porra-scoped unique key. The old key was
    #    ``(porra_id, user_id, partido_orden)`` → either
    #    ``uq_pick_porra_member_partido`` (defined in the model file but
    #    not always present in the live DB — it's a phantom from an
    #    autogenerate cycle that was never migrated) or no constraint
    #    at all (the dev DB at the time of writing only has
    #    ``uq_pick_member_partido``). We use the same IF-EXISTS pattern
    #    as ``b0c1d2e3f4a5`` so the migration is idempotent on both
    #    branches.
    conn = op.get_bind()
    for name in ("uq_pick_porra_member_partido",):
        exists = conn.execute(
            sa.text(
                """
                SELECT 1 FROM pg_constraint
                WHERE conname = :name
                  AND conrelid = 'pick'::regclass
                  AND contype = 'u'
                """
            ),
            {"name": name},
        ).first()
        if exists is not None:
            op.drop_constraint(name, "pick", type_="unique")
    op.create_unique_constraint(
        "uq_pick_porra_user_partido_col",
        "pick",
        ["porra_id", "user_id", "partido_orden", "columna_idx"],
    )

    # 4) Drop the vestigial MVP-02 constraint. Pre-porra scoping, picks
    #    had at most one row per (group, user, jornada, partido). With
    #    the multi-column phase that invariant changes (the porra's
    #    ``columnas`` may be > 1) and this legacy constraint would
    #    block INSERTs that the new porra-scoped key allows. The
    #    porra-scoped constraint above is the source of truth for
    #    per-member uniqueness going forward; ``group_id`` and
    #    ``jornada_id`` remain on the row for index efficiency +
    #    FK-cascade alignment, but uniqueness is enforced at the porra
    #    scope.
    conn = op.get_bind()
    legacy_exists = conn.execute(
        sa.text(
            """
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_pick_member_partido'
              AND conrelid = 'pick'::regclass
              AND contype = 'u'
            """
        )
    ).first()
    if legacy_exists is not None:
        op.drop_constraint("uq_pick_member_partido", "pick", type_="unique")


def downgrade() -> None:
    # Reverse (4) → (3) → (2) → (1).
    op.create_unique_constraint(
        "uq_pick_member_partido",
        "pick",
        ["group_id", "user_id", "jornada_id", "partido_orden"],
    )
    op.drop_constraint("uq_pick_porra_user_partido_col", "pick", type_="unique")
    op.create_unique_constraint(
        "uq_pick_porra_member_partido",
        "pick",
        ["porra_id", "user_id", "partido_orden"],
    )
    op.drop_column("pick", "columna_idx")
    op.drop_constraint("ck_jornada_default_columnas_range", "jornada", type_="check")
    op.drop_column("jornada", "default_columnas")
