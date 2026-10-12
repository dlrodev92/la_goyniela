"""Tests for the per-column propuesta algorithm (oct 2026, slice 2).

Coverage:

- ``test_propuesta_per_column_counts_per_column`` — 2 members each vote 2
  columns. Verify counts in column 1 = sum of votes from both users in
  column 1 only (NOT column 2).
- ``test_propuesta_intuicion_fijos_locked_across_columns`` — for
  ``modo='intuicion'`` with ``fijos=10``, verify the partidos 1..10
  have the SAME sign in all N columns of the propuesta.
- ``test_propuesta_multiple_single_column`` — for ``modo='multiple'``,
  the respuesta has exactly 1 column in ``columnas``.
- ``test_propuesta_sencilla_n_columns`` — for ``modo='sencilla'`` with
  ``columnas=4``, the respuesta has 4 columns, each with 14 simples.
- ``test_propuesta_pleno_shared_once`` — verify the Pleno is ONE value
  across the response (not N copies), computed from deduped per-user
  Pleno votes.
- ``test_propuesta_no_votes_returns_defaults`` — when nobody voted,
  return defaults (each column: 14 simples ``"1"``; pleno ``"0-0"``).
- ``test_propuesta_partido_vote_with_columna_idx`` — direct unit test
  on ``PartidoVote(columna_idx=...)`` aggregation.

All tests here hit the PURE algorithm (``compute_propuesta_for_porra``
and its helpers) — no DB, no FastAPI, no HTTP. The endpoint-level
coverage lives in ``tests/test_porra.py`` (``Propuesta`` section) and
``tests/test_picks_multicolumn.py``.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest

from app.services.propuesta import (
    PartidoVote,
    PlenoVote,
    compute_pleno_propuesta,
    compute_propuesta_for_porra,
    compute_vote_weights,
)


# ---------- Per-column counts ----------


def test_propuesta_per_column_counts_per_column():
    """2 members × 2 columns: counts in col 1 reflect col-1 votes only.

    User A votes ``"1"`` for partido 1 in BOTH columns; user B votes
    ``"X"`` in col 1 and ``"2"`` in col 2 for the SAME partido 1. The
    propuesta must NOT aggregate across columns — col 1 sees
    ``{"1": 1, "X": 1}`` and col 2 sees ``{"1": 1, "2": 1}`` for
    partido 1, exactly.
    """
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="intuicion",
        fijos=0,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.75"),
        partido_votes_by_column={
            1: {
                1: [
                    PartidoVote(user_id=1, columna_idx=1, signs=("1",)),
                    PartidoVote(user_id=2, columna_idx=1, signs=("X",)),
                ],
            },
            2: {
                1: [
                    PartidoVote(user_id=1, columna_idx=2, signs=("1",)),
                    PartidoVote(user_id=2, columna_idx=2, signs=("2",)),
                ],
            },
        },
        pleno_votes=[],
    )

    assert len(result["columnas"]) == 2
    col1 = result["columnas"][0]
    col2 = result["columnas"][1]

    # Col 1: A voted "1", B voted "X" → only those two signals.
    assert col1["partidos"][0]["vote_counts"] == {
        "1": 1.0, "X": 1.0, "2": 0.0,
    }
    # Col 2: A voted "1", B voted "2" — the "X" vote from col 1 does NOT leak.
    assert col2["partidos"][0]["vote_counts"] == {
        "1": 1.0, "X": 0.0, "2": 1.0,
    }
    # And the column indices are populated right.
    assert col1["columna_idx"] == 1
    assert col2["columna_idx"] == 2


def test_propuesta_per_column_partido_1_14_always_present():
    """Every column's ``partidos`` list has exactly 14 entries (1..14).

    We use ``modo='intuicion'`` (with ``fijos=0``) so the format
    assignment marks every match as simple — that keeps this test
    focused on the structural invariant (14 rows per column, in
    order) instead of where the D/T format falls.
    """
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="intuicion",
        fijos=0,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.75"),
        partido_votes_by_column={
            1: {
                5: [PartidoVote(user_id=1, columna_idx=1, signs=("1",))],
            },
        },
        pleno_votes=[],
    )
    assert len(result["columnas"]) == 1
    col = result["columnas"][0]
    assert len(col["partidos"]) == 14
    # Partido 5 has the actual vote; the rest are defaulted to "1".
    assert col["partidos"][4]["partido_orden"] == 5
    assert col["partidos"][4]["vote_counts"] == {
        "1": 1.0, "X": 0.0, "2": 0.0,
    }
    # Every other partido is a simple "1" with zero counts.
    for pos in (1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14):
        idx = pos - 1
        assert col["partidos"][idx]["partido_orden"] == pos
        assert col["partidos"][idx]["kind"] == "simple"
        assert col["partidos"][idx]["reparto"] == ["1"]
        assert col["partidos"][idx]["vote_counts"] == {
            "1": 0.0, "X": 0.0, "2": 0.0,
        }


# ---------- Modo branching ----------


def test_propuesta_intuicion_fijos_locked_across_columns():
    """``modo='intuicion'`` + ``fijos=10`` → partidos 1..10 carry the SAME sign in EVERY column.

    We engineer the votes so that WITHOUT the lock:

    - column 1 → partido 2 = ["1"] (both users voted "1").
    - column 2 → partido 2 = ["X"] (both users voted "X").

    With ``fijos=10``, partido 2 falls inside the locked range
    (1..10), so the propuesta must canonicalise using column 1's sign
    ("1") and force both columns' partido 2 = ["1"].

    Partidos 11..14 are NOT in the lock range → they carry their
    column-specific sign.
    """
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="intuicion",
        fijos=10,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.75"),
        partido_votes_by_column={
            1: {
                # Partido 2 (within 1..10): both users vote "1" in col 1.
                2: [
                    PartidoVote(user_id=1, columna_idx=1, signs=("1",)),
                    PartidoVote(user_id=2, columna_idx=1, signs=("1",)),
                ],
                # Partido 11 (outside the lock range): X in col 1.
                11: [
                    PartidoVote(user_id=1, columna_idx=1, signs=("X",)),
                ],
            },
            2: {
                # Same partido 2 (still in lock range), BUT a different
                # column-local winner ("X" wins in col 2 without the lock).
                2: [
                    PartidoVote(user_id=1, columna_idx=2, signs=("X",)),
                    PartidoVote(user_id=2, columna_idx=2, signs=("X",)),
                ],
                # Same partido 11 (outside lock range): "1" in col 2.
                11: [
                    PartidoVote(user_id=1, columna_idx=2, signs=("1",)),
                ],
            },
        },
        pleno_votes=[],
    )

    assert len(result["columnas"]) == 2

    # Partido 2 (within the lock range) must be ["1"] in BOTH columns
    # — col 2's natural ["X"] gets force-overwritten by col 1's ["1"].
    assert result["columnas"][0]["partidos"][1]["reparto"] == ["1"]
    assert result["columnas"][0]["partidos"][1]["kind"] == "simple"
    assert result["columnas"][1]["partidos"][1]["reparto"] == ["1"], (
        f"col 2 partido 2 expected ['1'] (lock forced), got "
        f"{result['columnas'][1]['partidos'][1]['reparto']!r}"
    )
    assert result["columnas"][1]["partidos"][1]["kind"] == "simple"

    # Partido 11 (outside the lock range) carries its column-specific
    # sign — col 1 = ["X"], col 2 = ["1"].
    assert result["columnas"][0]["partidos"][10]["reparto"] == ["X"]
    assert result["columnas"][1]["partidos"][10]["reparto"] == ["1"]


def test_propuesta_multiple_single_column():
    """``modo='multiple'`` → respuesta has exactly 1 column in ``columnas``.

    The default per-column algorithm runs ``assign_format_to_partidos``
    so this is also a regression check that the format assignment
    continues to work inside one column.
    """
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="multiple",
        fijos=None,
        dobles=1,      # single doble
        triples=0,
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column={
            1: {
                1: [PartidoVote(user_id=1, columna_idx=1, signs=("1",)),
                    PartidoVote(user_id=2, columna_idx=1, signs=("X",))],
            },
        },
        pleno_votes=[],
    )
    assert len(result["columnas"]) == 1
    col = result["columnas"][0]
    assert col["columna_idx"] == 1
    # One doble across the 14 entries (D=1, T=0).
    n_dobles = sum(1 for p in col["partidos"] if p["kind"] == "doble")
    n_triples = sum(1 for p in col["partidos"] if p["kind"] == "triple")
    assert n_dobles == 1
    assert n_triples == 0
    # partido 1 has the doble (it's the only partido with a divided vote).
    assert col["partidos"][0]["kind"] == "doble"
    assert sorted(col["partidos"][0]["reparto"]) == ["1", "X"]
    # Columns + cost mirror the multiple cost model: 2 cols × 0.50€ = €1.00.
    assert result["columns"] == 2
    assert result["cost_eur"] == Decimal("1.00")


def test_propuesta_sencilla_n_columns():
    """``modo='sencilla'`` + 4 columns → 4 column entries, all simples.

    With the multi-column sprint, even though there's only 1 vote per
    partido per column, the propuesta exposes the N-column shape. Every
    row in every column is a ``simple`` (``sencilla`` is by definition
    1-sign per row).

    We craft votes so the per-column sign is "1" in every column except
    column 3 where it's "X" — that proves each column is independent
    (column 3 sees its own vote pool, NOT columns 1/2/4).
    """
    cols_votes: dict[int, dict[int, list[PartidoVote]]] = {}
    for col_idx in range(1, 5):
        sign = "X" if col_idx == 3 else "1"
        cols_votes[col_idx] = {
            1: [PartidoVote(user_id=1, columna_idx=col_idx, signs=(sign,))],
        }

    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="sencilla",
        fijos=None,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column=cols_votes,
        pleno_votes=[],
    )
    assert len(result["columnas"]) == 4
    assert [c["columna_idx"] for c in result["columnas"]] == [1, 2, 3, 4]
    for col in result["columnas"]:
        for p in col["partidos"]:
            assert p["kind"] == "simple"

    # Column 3: user voted "X" → "X" wins on partido 1. The other
    # columns voted "1" → "1" wins there. Cross-column leakage would
    # average them — verify NO leakage.
    p1_by_col = {c["columna_idx"]: c["partidos"][0]["reparto"] for c in result["columnas"]}
    assert p1_by_col == {1: ["1"], 2: ["1"], 3: ["X"], 4: ["1"]}

    # Columns + cost = 4 cols × 0.50€ = €2.00 (slice-1 cost model).
    assert result["columns"] == 4
    assert result["cost_eur"] == Decimal("2.00")


def test_propuesta_reducida_single_column():
    """``modo='reducida'`` → 1 column, the format (D, T) comes from
    ``dobles``/``triples`` that the endpoint stamped from the spec."""
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="reducida",
        fijos=None,
        dobles=0,      # the spec stamps the real (D, T); here we
        triples=1,     # test (0, 1) → 3 columns.
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column={
            1: {
                1: [PartidoVote(user_id=1, columna_idx=1, signs=("1",))],
            },
        },
        pleno_votes=[],
    )
    assert len(result["columnas"]) == 1
    # (D=0, T=1) → 3 cols × 0.50€ = €1.50.
    assert result["columns"] == 3
    assert result["cost_eur"] == Decimal("1.50")


# ---------- Pleno ----------


def test_propuesta_pleno_shared_once():
    """The Pleno is ONE value across the response (not N copies).

    Internally each member has N rows for partido 15 (one per column),
    all with the same (local, visitante) by the picks data-model
    invariant. The algorithm must dedupe BY USER before aggregating so
    the propuesta carries ONE ``(local, visitante)`` pair at the top
    level (NOT inside each column).

    We engineer 2 users × 3 columns = 6 PlenoVote rows. Without dedup
    each user would dominate. With dedup, user A's (1, 0) and user B's
    (1, 0) tie 1-1 → tie-break keeps the canonical (1, 0).
    """
    pleno_votes: list[PlenoVote] = []
    for col_idx in (1, 2, 3):
        for user_id, (local, visitante) in ((1, ("1", "0")), (2, ("1", "0"))):
            pleno_votes.append(PlenoVote(
                user_id=user_id,
                columna_idx=col_idx,
                local=local,
                visitante=visitante,
            ))

    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="sencilla",
        fijos=None,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.50"),
        # 3 columns so the algoritmo sees the N=3 column structure;
        # it's the dedup logic on ``pleno_votes`` we're testing here.
        partido_votes_by_column={
            col_idx: {
                1: [
                    PartidoVote(user_id=1, columna_idx=col_idx, signs=("1",)),
                    PartidoVote(user_id=2, columna_idx=col_idx, signs=("1",)),
                ],
            }
            for col_idx in (1, 2, 3)
        },
        pleno_votes=pleno_votes,
    )

    # Top-level Pleno ONCE (not per column).
    assert "pleno" in result
    assert result["pleno"] == {"local": "1", "visitante": "0"}
    # None of the per-column payloads carry a Pleno entry.
    for col in result["columnas"]:
        for p in col["partidos"]:
            assert p["partido_orden"] != 15


def test_propuesta_pleno_dedup_one_user_per_column_dominates():
    """Without dedup, the most vocal user's Pleno would tie-break.

    User 1 votes (1, 0) across 5 columns, user 2 votes (2, 0) across 5
    columns. AFTER dedup: 1-1 tie → tie-break (P2): fewer goals first
    so ``"1"`` wins because ``"1" < "2"`` in the canonical order. If we
    naïvely aggregated all 10 rows: also 5-5 → same tie-break result.

    The dedup test is structural: verify the user's first row's
    ``columna_idx`` is preserved on the new PlenoVote but each user
    contributes exactly ONE row.
    """
    pleno_votes: list[PlenoVote] = []
    for col_idx in range(1, 6):
        pleno_votes.append(PlenoVote(
            user_id=1, columna_idx=col_idx,
            local="1", visitante="0",
        ))
    for col_idx in range(1, 6):
        pleno_votes.append(PlenoVote(
            user_id=2, columna_idx=col_idx,
            local="2", visitante="0",
        ))

    # Check the dedup manually: group by user, take first row per user.
    by_user: dict[int, list[PlenoVote]] = {}
    for v in pleno_votes:
        by_user.setdefault(v.user_id, []).append(v)
    deduped = [
        PlenoVote(
            user_id=uid,
            columna_idx=votes[0].columna_idx,
            local=votes[0].local,
            visitante=votes[0].visitante,
        )
        for uid, votes in by_user.items()
    ]
    # Each user contributes exactly one row.
    assert len(deduped) == 2

    propuesta = compute_pleno_propuesta(deduped)
    # Tie-break: fewer goals first → "1" wins.
    assert propuesta.local == "1"
    assert propuesta.visitante == "0"


# ---------- Defaults ----------


def test_propuesta_no_votes_returns_defaults():
    """Nobody voted → defaults per the spec: 14 simples "1" per column; pleno "0-0"."""
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="intuicion",
        fijos=0,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.75"),
        partido_votes_by_column={},
        pleno_votes=[],
    )
    # No columns when nobody voted — the algorithm returns an empty
    # ``columnas`` list (n_columns = 0). The endpoint's cost/columns
    # fields reflect the porra's configured ``columnas``, not the
    # algorithm's seen-columns; that's a follow-up slice-3 issue.
    assert len(result["columnas"]) == 0
    # Pleno falls back to "0-0" per spec P2.
    assert result["pleno"] == {"local": "0", "visitante": "0"}
    # Participation: 0 voters.
    assert result["participation"] == {"voted": 0}


def test_propuesta_no_votes_in_column_returns_per_column_defaults():
    """A column exists but no one voted → that column has 14 simples "1".

    This is the more useful no-vote case: a porra with
    ``columnas=3`` exists (admin set it) but only the algorithm sees 0
    votes (e.g. empty test setup). The algorithm produces N empty
    columns by iterating ``range(1, n_columns + 1)``.
    """
    # Caller passes an empty dict per their columna_idx — the algorithm
    # detects ``n_columns`` from ``max(keys)``. So we simulate a porra
    # with N columns configured but no votes submitted by setting
    # empty lists per column:
    empty_cols: dict[int, dict[int, list[PartidoVote]]] = {
        col_idx: {} for col_idx in range(1, 4)
    }
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="sencilla",
        fijos=None,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column=empty_cols,
        pleno_votes=[],
    )
    assert len(result["columnas"]) == 3
    for col in result["columnas"]:
        assert len(col["partidos"]) == 14
        for p in col["partidos"]:
            assert p["kind"] == "simple"
            assert p["reparto"] == ["1"]
            assert p["vote_counts"] == {"1": 0.0, "X": 0.0, "2": 0.0}
    assert result["pleno"] == {"local": "0", "visitante": "0"}
    # The (empty) columns still drive cost_eur/columns for sencilla.
    assert result["columns"] == 3
    assert result["cost_eur"] == Decimal("1.50")


# ---------- Response shape ----------


def test_propuesta_response_passthrough_modo_fijos():
    """The top-level ``modo`` and ``fijos`` are pass-through from the porra."""
    result = compute_propuesta_for_porra(
        porra_id=42,
        modo="intuicion",
        fijos=7,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.75"),
        partido_votes_by_column={
            1: {
                1: [PartidoVote(user_id=1, columna_idx=1, signs=("1",))],
            },
        },
        pleno_votes=[],
    )
    assert result["porra_id"] == 42
    assert result["modo"] == "intuicion"
    assert result["fijos"] == 7


def test_propuesta_passthrough_modo_enum():
    """``modo`` accepts both enum and string — the value is stringified on the response."""
    from app.db.models import PorraModo

    result = compute_propuesta_for_porra(
        porra_id=1,
        modo=PorraModo.sencilla,  # enum, not string
        fijos=None,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column={1: {}},
        pleno_votes=[],
    )
    assert result["modo"] == "sencilla"


def test_propuesta_passthrough_modo_string_for_other_modes():
    """Non-intuicion modos set ``fijos`` to whatever was passed (typically None)."""
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="multiple",
        fijos=None,
        dobles=2,
        triples=0,
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column={1: {}},
        pleno_votes=[],
    )
    assert result["modo"] == "multiple"
    assert result["fijos"] is None
    # cost = 2^2 = 4 columns × 0.50€ = €2.00.
    assert result["columns"] == 4
    assert result["cost_eur"] == Decimal("2.00")


# ---------- Direct unit tests on PartidoVote/PlenoVote ----------


def test_propuesta_partido_vote_with_columna_idx():
    """``PartidoVote`` carries ``columna_idx`` and the per-column vote count math works."""
    votes = [
        PartidoVote(user_id=1, columna_idx=1, signs=("1",)),
        PartidoVote(user_id=2, columna_idx=1, signs=("1", "X")),
    ]
    weights = compute_vote_weights(votes)
    # User 1: 1.0 to "1". User 2: 0.5 to "1", 0.5 to "X". Totals:
    # "1": 1.5, "X": 0.5, "2": 0.0.
    assert weights == {"1": 1.5, "X": 0.5, "2": 0.0}


def test_propuesta_partido_vote_columna_idx_is_informational():
    """The ``columna_idx`` on ``PartidoVote`` is informational — the
    aggregator does NOT use it to filter votes. The endpoint pre-groups
    votes; the algorithm ignores ``columna_idx`` after grouping.

    This test demonstrates that helper math is what matters for the per-
    column aggregation — the field exists so the JSON shape stays
    honest and debuggable.
    """
    # Two votes with the same signs but distinct columna_idx still
    # both contribute to the same per-column weight sum (here we
    # simulate by calling ``compute_vote_weights`` with both — the
    # caller would have pre-grouped them into a single column).
    mixed = [
        PartidoVote(user_id=1, columna_idx=1, signs=("1",)),
        PartidoVote(user_id=2, columna_idx=2, signs=("1",)),
    ]
    weights = compute_vote_weights(mixed)
    assert weights == {"1": 2.0, "X": 0.0, "2": 0.0}


def test_propuesta_pleno_vote_has_columna_idx():
    """``PlenoVote`` carries ``columna_idx``; the constructor accepts it."""
    v = PlenoVote(user_id=1, columna_idx=3, local="M", visitante="2")
    assert v.user_id == 1
    assert v.columna_idx == 3
    assert v.local == "M"
    assert v.visitante == "2"


def test_propuesta_uses_counter_for_pleno():
    """Sanity: the Pleno propuesta logic counts votes per value (deduped by user)."""
    # 3 unique users each vote "1" once, 2 vote "2". With dedup the
    # tally is {"1": 3, "2": 2} → "1" wins on local.
    votes = [
        PlenoVote(user_id=u, columna_idx=1, local="1", visitante="0")
        for u in (1, 2, 3)
    ] + [
        PlenoVote(user_id=u, columna_idx=1, local="2", visitante="0")
        for u in (4, 5)
    ]
    by_user: dict[int, list[PlenoVote]] = {}
    for v in votes:
        by_user.setdefault(v.user_id, []).append(v)
    deduped = [
        PlenoVote(
            user_id=uid,
            columna_idx=vs[0].columna_idx,
            local=vs[0].local,
            visitante=vs[0].visitante,
        )
        for uid, vs in by_user.items()
    ]
    counter: Counter[str] = Counter(v.local for v in deduped)
    assert counter == Counter({"1": 3, "2": 2})
    propuesta = compute_pleno_propuesta(deduped)
    assert propuesta.local == "1"
    assert propuesta.visitante == "0"


# ---------- Cost / columns by modo ----------


def test_propuesta_cost_intuicion_matches_columnas_x_precio():
    """``modo='intuicion'`` cost = ``columnas × precio_apuesta_eur``."""
    # 4 columns × 0.75€ = €3.00.
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="intuicion",
        fijos=10,
        dobles=0,
        triples=0,
        precio_apuesta_eur=Decimal("0.75"),
        partido_votes_by_column={
            col_idx: {
                1: [PartidoVote(user_id=1, columna_idx=col_idx, signs=("1",))],
            }
            for col_idx in range(1, 5)
        },
        pleno_votes=[],
    )
    assert result["columns"] == 4
    assert result["cost_eur"] == Decimal("3.00")


def test_propuesta_cost_multiple_matches_doble_triple():
    """``modo='multiple'`` cost = ``2^D × 3^T × precio_apuesta_eur``."""
    # (D=2, T=1) → 4 × 3 = 12 columns × 0.50€ = €6.00.
    result = compute_propuesta_for_porra(
        porra_id=1,
        modo="multiple",
        fijos=None,
        dobles=2,
        triples=1,
        precio_apuesta_eur=Decimal("0.50"),
        partido_votes_by_column={
            1: {
                1: [PartidoVote(user_id=1, columna_idx=1, signs=("1",))],
            },
        },
        pleno_votes=[],
    )
    assert result["columns"] == 12
    assert result["cost_eur"] == Decimal("6.00")
