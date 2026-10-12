"""MVP-04: quiniela modes (simple / doble / triple)

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-10-05 19:00:00.000000

Adds:
- ``jornada.quini_config JSONB`` — the per-jornada config that controls
  which matches are simple / doble / triple (MVP-04 quiniela modes).
  Defaults to NULL which the API treats as "directa simple" (the legacy
  1-sign-per-match behavior).

Evolves the ``pick`` table to support dobles/triples:
- Adds ``pick.signs JSONB`` (nullable) — a list of 1, 2, or 3 elements from
  ``{"1","X","2"}``. Length 1 = simple, 2 = doble, 3 = triple. The Pydantic
  validator + the picks endpoint enforce this against the jornada's
  ``quini_config.matches`` map.
- Backfills ``signs`` from the existing ``sign String(2)`` column for the
  partidos 1-14 rows that were using it: ``signs = jsonb_build_array(sign)``.
  Partido 15 rows have ``sign IS NULL`` so they are not touched here — their
  ``pleno_signs`` column stays unchanged.
- Drops the old ``pick.sign`` column.
- Replaces the old ``ck_pick_sign_xor_pleno`` constraint with
  ``ck_pick_signs_xor_pleno`` (same XOR semantics, but referencing ``signs``
  instead of ``sign``).
- Drops the now-obsolete ``ck_pick_sign_valid`` constraint (sign column
  is gone, so the IN('1','X','2') check is redundant — the new JSONB
  shape is validated in the application layer).

Net effect on existing rows: every pick row that had ``sign='1'`` (say)
now has ``signs=['1']``. The application treats this as "simple pick",
so behavior is identical for every pick that was ever submitted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a1"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add quini_config + evolve pick.sign → pick.signs."""

    # --- 1. jornada.quini_config: per-jornada quiniela modes (MVP-04).
    # Nullable, no server_default — the absence is meaningful and treated
    # by the application as "directa simple" (the legacy behavior).
    op.add_column(
        "jornada",
        sa.Column(
            "quini_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # --- 2. pick.signs: add the JSONB column for 1-3 signs per pick.
    op.add_column(
        "pick",
        sa.Column(
            "signs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # --- 3. Backfill signs from sign for existing rows (partidos 1-14).
    # jsonb_build_array wraps the existing single sign into a 1-element
    # JSONB array. The WHERE clause ensures we don't touch pleno rows
    # (partido 15) which have sign IS NULL anyway.
    op.execute(
        "UPDATE pick SET signs = jsonb_build_array(sign) WHERE sign IS NOT NULL"
    )

    # --- 4. Drop the old constraints that reference sign.
    op.drop_constraint("ck_pick_sign_xor_pleno", "pick", type_="check")
    op.drop_constraint("ck_pick_sign_valid", "pick", type_="check")

    # --- 5. Drop the sign column itself.
    op.drop_column("pick", "sign")

    # --- 6. Add the new XOR constraint referencing signs. The pleno_signs
    # column is unchanged; the new check has identical semantics to the
    # old one, just against the renamed column.
    op.create_check_constraint(
        "ck_pick_signs_xor_pleno",
        "pick",
        "(signs IS NOT NULL AND pleno_signs IS NULL) OR "
        "(signs IS NULL AND pleno_signs IS NOT NULL)",
    )


def downgrade() -> None:
    """Downgrade schema: reverse the pick evolution + drop quini_config."""

    # Reverse 6: drop the new XOR check.
    op.drop_constraint("ck_pick_signs_xor_pleno", "pick", type_="check")

    # Reverse 5: re-add the sign column and backfill from signs[0].
    op.add_column(
        "pick",
        sa.Column("sign", sa.String(length=2), nullable=True),
    )
    # jsonb_array_elements_text requires a FROM clause; pick each row's
    # first element of the signs array. Safe because every signo row
    # in the post-upgrade state has exactly 1 element (the data was
    # backfilled that way from the original sign column).
    op.execute(
        "UPDATE pick SET sign = (signs ->> 0) "
        "WHERE signs IS NOT NULL AND jsonb_array_length(signs) >= 1"
    )

    # Reverse 4: re-add the old constraints.
    op.create_check_constraint(
        "ck_pick_sign_xor_pleno",
        "pick",
        "(sign IS NOT NULL AND pleno_signs IS NULL) OR "
        "(sign IS NULL AND pleno_signs IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_pick_sign_valid",
        "pick",
        "sign IS NULL OR sign IN ('1','X','2')",
    )

    # Reverse 3: drop the signs column.
    op.drop_column("pick", "signs")

    # Reverse 1: drop the quini_config column.
    op.drop_column("jornada", "quini_config")