"""Slice 1 — Jornada window fields + TeamIdentityMap.gender_known

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5b6
Create Date: 2026-10-06 10:50:00.000000

Adds three columns needed by the jornada manager (slice 1 of the
``gestor-jornadas`` plan, ``docs/producto/gestor-jornadas.md``):

- ``jornada.kickoff_last_match_at`` (DateTime(timezone=True), nullable):
  the kickoff of the last partido in the boleto — the practical "end" of
  the jornada. Used by :class:`app.services.jornada_manager.JornadaManager`
  to derive ``voting_closes_at`` (``kickoff_last - 1h``) and
  ``escrutinio_expected_at`` (``kickoff_last + 2.5h``). Nullable because
  the column is populated AFTER the 15 ``composicion.kickoff_at`` rows
  land; a draft jornada has no value.

- ``jornada.escrutinio_expected_at`` (DateTime(timezone=True), nullable):
  cached value of ``kickoff_last_match_at + 2.5h`` so the API can return
  it without recomputing. Same nullability rationale as above.

- ``team_identity_map.gender_known`` (Boolean, NOT NULL, default true):
  distinguishes rows where ``gender`` reflects certain knowledge (seeded
  catalog rows, rows created from a feminine suffix like "Logroño F")
  from auto-generated stubs where the matcher had to assume ``'men'``
  because no marker was present. The UI uses it to render a "?" fallback
  instead of showing the assumed gender as fact. Default ``true`` keeps
  every pre-existing row behaving exactly as before — the matcher
  update that flips stubs to ``gender_known=False`` lands in slice 5
  (which only reads this column once it exists).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add kickoff_last_match_at, escrutinio_expected_at, gender_known."""
    op.add_column(
        "jornada",
        sa.Column("kickoff_last_match_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "jornada",
        sa.Column("escrutinio_expected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "team_identity_map",
        sa.Column(
            "gender_known",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    """Reverse the three columns added in upgrade()."""
    op.drop_column("team_identity_map", "gender_known")
    op.drop_column("jornada", "escrutinio_expected_at")
    op.drop_column("jornada", "kickoff_last_match_at")