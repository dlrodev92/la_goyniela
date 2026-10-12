"""Pure functions for the MVP-05 porra model.

Two layers:

- **Cost + format math** (``column_cost``, ``total_cost``,
  ``feasible_formats``) — pure arithmetic, no DB, no I/O.

- **Media algorithm** (``compute_propuesta``) — given the picks for one
  porra, returns the computed media (which partidos become dobles / triples
  + which signs go in each + the pleno). Also pure (takes plain Python
  data) so it can be unit-tested without a DB.

Spec: ``la_goyniela_client/docs/project/features/porra-model.md``.

Media algorithm (per the spec):

1. **Weight of a vote.** Each member contributes **1 vote per partido**,
   split equally across the signs they marked (1 sign = 1.0; 2 signs =
   0.5 + 0.5; 3 signs = 1/3 + 1/3 + 1/3). A triple does NOT weigh more
   than a simple — they all contribute 1.0 in total. *(Confirmed P1.)*

2. **Per-partido aggregation.** For each partido, sum the weights for
   each sign → ``s1, sx, s2``. Sort desc: ``a ≥ b ≥ c``.

3. **Gain of covering more signs.**
   - Doble gain  = ``b``.
   - Triple gain = ``b + c``.

4. **Assignment.** Greedy: top T partidos by ``(b+c)`` → triples; top D
   of the REMAINING by ``b`` → dobles; rest → simple. Ties broken by
   ``partido_orden`` ascending.

5. **Sign output.**
   - Simple: ``[a]``
   - Doble:  ``[a, b]``
   - Triple: ``[a, b, c]``
   Tie-break on equal weights: ``"1" > "X" > "2"`` (deterministic).

6. **Pleno al 15.** For each of (local, visitante), the value with the
   most votes wins. Tie-break: fewer goals first (``0 < 1 < 2 < M``).
   *(Confirmed P2.)*

7. **Defaults for zero-vote partidos.** If a partido has NO votes, fall
   back to ``signs=["1"]`` (simple, home win) and ``kind="simple"``. The
   admin will fix the final boleto from there. The algoritmo returns the
   propuesta anyway (the spec says yes, even with missing voters — P3).

For pleno with no votes, default to ``{"local": "0", "visitante": "0"}``.

All functions are PURE so they can be unit-tested without a DB. The
endpoint (``app/api/v1/porras.py``) feeds the picks as plain Python data.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Iterable, NamedTuple

from app.db.models import PorraModo
from app.schemas.porra import (
    PlenoIn,
    PLENO_VALUES,
    SIGNS,
)


# ---------- Cost + format math ----------


def column_cost(dobles: int, triples: int) -> int:
    """Number of columns the boleto plays.

    Formula: ``2^D × 3^T``. Per Quiniela real, this is the column count
    of one boleto multiple with ``D`` doubles and ``T`` triples.

    The minimum is 2 columns: ``(D=1, T=0)`` or ``(D=0, T=1)``.
    ``(D=0, T=0)`` is rejected at the schema + DB layer (migration
    ``f4e5d6c7b8a9``) — Quiniela does not sell 1-column boletos.

    Always 1 or higher for the math; the API layer enforces ≥ 2.
    The CHECK ``ck_porra_dt_within_partidos`` already enforces
    ``D + T ≤ 14`` so we don't re-check here.
    """
    return (2 ** dobles) * (3 ** triples)


def total_cost(dobles: int, triples: int, precio_apuesta_eur: Decimal) -> Decimal:
    """Total cost of one boleto: ``column_cost(D, T) × precio_apuesta_eur``."""
    return Decimal(column_cost(dobles, triples)) * Decimal(precio_apuesta_eur)


def feasible_formats(
    *,
    budget_eur: Decimal,
    precio_apuesta_eur: Decimal,
    max_dobles: int = 14,
    max_triples: int = 14,
) -> list[dict]:
    """Enumerate (D, T) pairs that fit the budget at the given price.

    Returns a list of dicts ``{dobles, triples, columns, coste_eur}``,
    ordered by:
      1. ``columns`` desc (best coverage first)
      2. ``triples`` desc (more triples is better than more dobles
         for the same column count)
      3. ``dobles`` desc
      4. ``dobles + triples`` desc (more matches upgraded)
      5. ``dobles`` asc (less risk: upgrade "free" matches first)

    Starts at the minimum valid format (2 columns = 1 doble). The
    ``(0, 0)`` format (1 simple column) is NOT a valid Quiniela
    product; it's rejected at the schema + DB layer
    (migration ``f4e5d6c7b8a9``).

    Constraints:
    - ``0 <= D <= max_dobles``, ``0 <= T <= max_triples``
    - ``(D, T) != (0, 0)`` (minimum 2 columns)
    - ``D + T <= 14``
    - ``column_cost(D, T) * precio_apuesta_eur <= budget_eur``

    Raises ``ValueError`` if ``precio_apuesta_eur <= 0`` (the cost
    model would degenerate — every format fits).
    """
    if precio_apuesta_eur <= 0:
        raise ValueError(
            f"precio_apuesta_eur must be > 0, got {precio_apuesta_eur}",
        )
    if budget_eur <= 0:
        raise ValueError(f"budget_eur must be > 0, got {budget_eur}")
    out: list[dict] = []
    for d in range(0, max_dobles + 1):
        for t in range(0, max_triples + 1):
            # Minimum 2 columns: skip (0, 0). Also skip anything that
            # uses 1 column total (impossible by definition since the
            # smallest column_cost is 2^1 = 2 at d=1).
            if d == 0 and t == 0:
                continue
            if d + t > 14:
                continue
            cols = column_cost(d, t)
            cost = total_cost(d, t, precio_apuesta_eur)
            if cost <= budget_eur:
                out.append({
                    "dobles": d,
                    "triples": t,
                    "columns": cols,
                    "coste_eur": cost,
                })
    # Sort: columns desc, triples desc, dobles desc, then (d+t) desc, d asc.
    out.sort(
        key=lambda f: (
            -f["columns"],
            -f["triples"],
            -f["dobles"],
            -(f["dobles"] + f["triples"]),
            f["dobles"],
        ),
    )
    return out


def max_columns_for_budget(
    *,
    budget_eur: Decimal,
    precio_apuesta_eur: Decimal,
) -> tuple[int, int]:
    """Return the (D, T) pair with the most columns that fits the budget.

    Useful as a default suggestion when the admin doesn't specify D/T.
    Picks the first entry of ``feasible_formats`` (which is sorted by
    columns desc) and returns just (D, T).
    """
    formats = feasible_formats(
        budget_eur=budget_eur, precio_apuesta_eur=precio_apuesta_eur,
    )
    if not formats:
        return (0, 0)
    first = formats[0]
    return (first["dobles"], first["triples"])


# ---------- Media algorithm ----------


class PartidoVote(NamedTuple):
    """One member's vote for one partido 1-14 in one column.

    ``signs`` is a list of 1, 2, or 3 unique elements from ``SIGNS``.
    The function computes the weight as ``1.0 / len(signs)`` and splits
    it equally across the signs.

    ``columna_idx`` (oct 2026, multi-column sprint) is the 1-indexed
    position in the multi-column pick submission. Votes from DIFFERENT
    columns of the same member must NOT be aggregated together — each
    column is its own propuesta. The per-column aggregator receives
    votes already grouped by ``columna_idx`` so this field is purely
    informational; the algorithm does not branch on it.
    """
    user_id: int
    columna_idx: int
    signs: tuple[str, ...]


class PlenoVote(NamedTuple):
    """One member's voto for partido 15 (pleno) in one column.

    ``local`` and ``visitante`` ∈ ``PLENO_VALUES``. The Pleno is
    **shared per member** across all columns of the same submission —
    the per-column aggregator deduplicates per-user Pleno votes before
    calling :func:`compute_pleno_propuesta` so the response carries ONE
    Pleno, not N copies.

    ``columna_idx`` is the column the row originated from (1..N). All
    N rows for the same user have the same ``local``/``visitante``
    values; the dedup picks any one of them.
    """
    user_id: int
    columna_idx: int
    local: str
    visitante: str


def compute_vote_weights(
    partido_votes: Iterable[PartidoVote],
) -> dict[str, float]:
    """Sum the per-sign weights for one partido.

    Each vote contributes ``1.0 / len(vote.signs)`` to each sign in its
    list. Returns ``{"1": float, "X": float, "2": float}`` with all three
    keys present (zero if no vote picked that sign).
    """
    weights: dict[str, float] = {s: 0.0 for s in SIGNS}
    for v in partido_votes:
        if not v.signs:
            continue
        w = 1.0 / len(v.signs)
        for s in v.signs:
            if s in weights:
                weights[s] += w
    return weights


def _sort_signs_desc(weights: dict[str, float]) -> list[tuple[str, float]]:
    """Sort signs by weight desc; ties by canonical order ``1 > X > 2``.

    Deterministic — the spec requires it (so the propuesta is reproducible
    from the same votes).
    """
    canonical = {"1": 0, "X": 1, "2": 2}
    return sorted(
        weights.items(),
        key=lambda kv: (-kv[1], canonical.get(kv[0], 99)),
    )


def assign_format_to_partidos(
    weights_by_partido: dict[int, dict[str, float]],
    *,
    dobles_budget: int,
    triples_budget: int,
) -> dict[int, tuple[str, list[str]]]:
    """Decide which partidos get doble / triple and the sign list for each.

    Returns ``{partido_orden: (kind, signs)}`` for all 14 partidos.
    Greedy:

    - Compute ``b + c`` (triple gain) for each partido.
    - Top ``triples_budget`` by ``(b + c)`` → triples (ties by ``partido_orden`` asc).
    - For the remaining, compute ``b`` (doble gain).
    - Top ``dobles_budget`` by ``b`` → dobles.
    - Rest → simple.

    Tie-break: ``partido_orden`` ascending. The spec says "los empates se
    rompen por posición del partido".

    The signs list for each partido is the top-N by weight (N = len(signs)
    in the kind: 1 for simple, 2 for doble, 3 for triple). Within the top
    N, ties on the cutoff are broken by canonical order ``1 > X > 2``.

    ``weights_by_partido`` maps ``partido_orden`` (1..14) → weights dict
    (all three signs, even if zero). Partidos with NO votes get an empty
    dict (``{s: 0.0 for s in SIGNS}``), which maps to simple + ``["1"]``
    (a default — admin fixes in the final boleto).
    """
    # 1. Compute gain triples per partido: b + c (top-2 and top-3 sums).
    triple_gains: list[tuple[int, float]] = []
    for pos, w in weights_by_partido.items():
        ranked = _sort_signs_desc(w)
        if len(ranked) < 2:
            triple_gain = ranked[0][1] if ranked else 0.0
        else:
            triple_gain = ranked[1][1] + (ranked[2][1] if len(ranked) > 2 else 0.0)
        triple_gains.append((pos, triple_gain))

    # Sort by triple_gain desc, tie-break by partido_orden asc.
    triple_gains.sort(key=lambda kv: (-kv[1], kv[0]))
    triple_partidos = {pos for pos, _ in triple_gains[:triples_budget]}

    # 2. Compute doble gains for the REMAINING partidos: b (top-2 weight).
    doble_gains: list[tuple[int, float]] = []
    for pos, w in weights_by_partido.items():
        if pos in triple_partidos:
            continue
        ranked = _sort_signs_desc(w)
        doble_gain = ranked[1][1] if len(ranked) > 1 else 0.0
        doble_gains.append((pos, doble_gain))

    doble_gains.sort(key=lambda kv: (-kv[1], kv[0]))
    doble_partidos = {pos for pos, _ in doble_gains[:dobles_budget]}

    # 3. Compose the per-partido result.
    out: dict[int, tuple[str, list[str]]] = {}
    for pos in sorted(weights_by_partido.keys()):
        ranked = _sort_signs_desc(weights_by_partido[pos])
        if pos in triple_partidos:
            kind = "triple"
            signs = [r[0] for r in ranked[:3]]
        elif pos in doble_partidos:
            kind = "doble"
            signs = [r[0] for r in ranked[:2]]
        else:
            kind = "simple"
            signs = [r[0] for r in ranked[:1]]
        # Edge case — fully empty weights: default to simple + "1" so the
        # propuesta never has an empty signs list (admin can fix it).
        if not signs:
            signs = ["1"]
        out[pos] = (kind, signs)

    return out


def compute_pleno_propuesta(
    pleno_votes: Iterable[PlenoVote],
) -> PlenoIn:
    """Pick the local + visitante value with the most votes.

    Tie-break: fewer goals first (``0 < 1 < 2 < M``). If no votes at all,
    defaults to ``{"local": "0", "visitante": "0"}`` (the spec's default).
    """
    local_counter: Counter[str] = Counter()
    visit_counter: Counter[str] = Counter()
    for v in pleno_votes:
        local_counter[v.local] += 1
        visit_counter[v.visitante] += 1

    # Tie-break order: 0, 1, 2, M (fewer goals first per spec).
    tiebreak = {"0": 0, "1": 1, "2": 2, "M": 3}

    def _winner(counter: Counter[str]) -> str:
        if not counter:
            return "0"
        max_count = max(counter.values())
        candidates = [v for v, c in counter.items() if c == max_count]
        candidates.sort(key=lambda v: tiebreak.get(v, 99))
        return candidates[0]

    return PlenoIn(
        local=_winner(local_counter),
        visitante=_winner(visit_counter),
    )


# ---------- Convenience aggregator ----------


def compute_propuesta_for_porra(
    *,
    porra_id: int,
    modo: "PorraModo | str",
    fijos: int | None,
    dobles: int,
    triples: int,
    precio_apuesta_eur: Decimal,
    partido_votes_by_column: dict[int, dict[int, list[PartidoVote]]],
    pleno_votes: list[PlenoVote],
) -> dict:
    """Compute the full propuesta for one porra — one entry PER COLUMN.

    **Multi-column shape (oct 2026, slice 2):** the votes are grouped
    by ``columna_idx`` so each column becomes its own independiente
    propuesta. The Pleno is SHARED across columns — there's one
    ``(local, visitante)`` propuesta for the whole porra (deduplicated
    per user; the values are identical across the user's columns by the
    picks model invariant).

    The pure-function contract: input is plain Python data, output is a
    plain ``dict``. The endpoint (``app/api/v1/porras.py``) is the only
    caller and it does the DB→pure conversion. No I/O happens here.

    Parameters
    ----------
    porra_id
        Echoed into the response.
    modo
        ``PorraModo`` value or string (``"multiple"``, ``"sencilla"``,
        ``"reducida"``, ``"intuicion"``). Drives the format-assignment
        branch — ``multiple`` / ``reducida`` apply the ``(D, T)`` format
        per column; ``sencilla`` / ``intuicion`` mark every partido in
        every column as ``simple``. Passed through to the response as
        ``modo``.
    fijos
        Only meaningful when ``modo == 'intuicion'``. The first
        ``fijos`` partidos are LOCKED to the same sign across ALL
        columns of the propuesta — the algorithm picks the canonical
        sign from column 1 and forces it on the remaining columns.
        ``None`` for the other modes.
    dobles, triples
        The porra's format budget. Used directly only for
        ``multiple`` / ``reducida``.
    precio_apuesta_eur
        Per-column price. Used to compute ``cost_eur`` for the
        ``sencilla`` / ``intuicion`` modes (``columnas *
        precio_apuesta_eur``); the other modes use the (D, T) cost
        formula.
    partido_votes_by_column
        ``{columna_idx: {partido_orden: [PartidoVote]}}``. The endpoint
        already groups votes by both columns AND partido before calling
        here. Partidos not present in a column get an empty vote list
        (no votes → defaults to ``simple`` + ``["1"]`` per the existing
        spec).
    pleno_votes
        Flat list of ``PlenoVote`` rows across all columns of all
        members. The function dedups per user — the user's N rows have
        the same ``(local, visitante)`` by invariant, so the first one
        wins and the user contributes one vote to the Pleno aggregation.

    Returns
    -------
    dict with keys:

    - ``porra_id`` — echo
    - ``modo`` — string, pass-through
    - ``fijos`` — int or ``None``, pass-through
    - ``columnas`` — list of ``{columna_idx, partidos: [...]}. Each
      ``partidos`` list has 14 entries (one per partido 1..14, in
      order). Partido 15 is NOT in the per-column output (it is shared
      outside the columns array).
    - ``pleno`` — ``{"local": str, "visitante": str}`` — ONE shared
      propuesta computed from the deduplicated per-user Pleno votes.
    - ``participation`` — ``{"voted": int}`` — count of unique users
      who voted (partido or Pleno).
    - ``columns`` — number of columns the BOLETO plays (the same value
      used elsewhere: ``2^D × 3^T`` for ``multiple`` / ``reducida``,
      ``porra.columnas`` for ``sencilla`` / ``intuicion``).
    - ``cost_eur`` — total cost of the boleto.

    Per-partido payload ``partidos[i]``:
    - ``partido_orden`` — 1..14
    - ``vote_counts`` — ``{"1": float, "X": float, "2": float}``
    - ``reparto`` — list of 1, 2, or 3 signs (top-N by weight in this
      column's votes)
    - ``kind`` — ``"simple"`` | ``"doble"`` | ``"triple"``

    Defaults for empty columns: every partido is ``simple`` with
    ``reparto = ["1"]`` and ``vote_counts`` all zero. The Pleno default
    (no votes) is ``"0" / "0"`` (the spec's P2 default).
    """
    # --- 1. Resolve column count.
    if partido_votes_by_column:
        n_columns = max(partido_votes_by_column.keys())
    else:
        n_columns = 0

    # --- 2. Aggregate the SHARED Pleno (deduplicate per user).
    #
    # Each member submits N rows for partido 15 — one per columna_idx —
    # all with the same (local, visitante) by the picks data-model
    # invariant (tested in test_picks_intuicion_pleno_shared_across_columns).
    # Counting them as N votes would inflate the tally Nx for every
    # user (and the most active voter would dominate the propuesta).
    # Dedupe by user; take whichever of the user's rows we see first.
    pleno_per_user: dict[int, list[PlenoVote]] = {}
    for v in pleno_votes:
        pleno_per_user.setdefault(v.user_id, []).append(v)
    deduped_pleno_votes: list[PlenoVote] = [
        PlenoVote(
            user_id=uid,
            columna_idx=votes[0].columna_idx,
            local=votes[0].local,
            visitante=votes[0].visitante,
        )
        for uid, votes in pleno_per_user.items()
    ]
    pleno_propuesta = compute_pleno_propuesta(deduped_pleno_votes)

    # --- 3a. Pre-compute the canonical signs for the intuicion fijos.
    #
    # The first ``fijos`` partidos must carry the SAME sign in every
    # column of the propuesta. The canonical sign comes from COLUMN 1
    # (the admin's reference column when they set ``fijos``). We
    # compute it once, before the per-column loop, so each column's
    # iteration can force the same sign.
    #
    # If column 1 has no votes for a fijo, we fall back to ``"1"``
    # (the per-partido empty default the rest of the algorithm also
    # uses — matches the "admin fixes in the final boleto" spec rule).
    canonical_signs_for_fijos: dict[int, str] = {}
    if (
        modo == PorraModo.intuicion
        or (isinstance(modo, str) and modo == "intuicion")
    ) and (fijos or 0) > 0:
        col1_votes = partido_votes_by_column.get(1, {})
        col1_weights: dict[int, dict[str, float]] = {}
        for pos in range(1, 15):
            col1_weights[pos] = compute_vote_weights(col1_votes.get(pos, []))
        for pos in range(1, (fijos or 0) + 1):
            ranked = _sort_signs_desc(col1_weights[pos])
            canonical_signs_for_fijos[pos] = (
                ranked[0][0] if ranked else "1"
            )

    # --- 3b. Per-column propuesta.
    #
    # The format assignment branches on modo:
    # - multiple / reducida: each column independently runs the
    #   (dobles, triples) format assignment. For these modos N=1, so
    #   "per-column" is structurally identical to the legacy flat
    #   algorithm. Output is wrapped in a 1-entry ``columnas`` list.
    # - sencilla / intuicion: every partido in every column is a
    #   ``simple`` (sencilla is by definition; intuicion likewise
    #   because the variation lives ACROSS columns, not within rows).
    columnas_out: list[dict] = []
    for col_idx in range(1, n_columns + 1):
        col_votes_by_partido = partido_votes_by_column.get(col_idx, {})

        weights_by_partido: dict[int, dict[str, float]] = {}
        for pos in range(1, 15):
            weights_by_partido[pos] = compute_vote_weights(
                col_votes_by_partido.get(pos, []),
            )

        if modo in (PorraModo.multiple, PorraModo.reducida) or (
            isinstance(modo, str) and modo in ("multiple", "reducida")
        ):
            kind_signs = assign_format_to_partidos(
                weights_by_partido,
                dobles_budget=dobles,
                triples_budget=triples,
            )
        else:
            # sencilla / intuicion — every partido is a simple.
            kind_signs = _all_simple_kind_signs(weights_by_partido)

        # --- 3c. Enforce intuicion fijos within this column.
        #
        # We don't read from a per-column ``kind_signs`` to drive the
        # canonical — the canonical was pre-computed from column 1.
        # The "lock" is unconditional: every column carries the same
        # sign for every locked partido, regardless of how the
        # per-column algorithm would have picked otherwise.
        for pos, canonical_sign in canonical_signs_for_fijos.items():
            if pos in kind_signs:
                kind_signs[pos] = ("simple", [canonical_sign])

        partidos_out: list[dict] = []
        for pos in range(1, 15):
            kind, signs = kind_signs[pos]
            partidos_out.append({
                "partido_orden": pos,
                "vote_counts": {
                    s: float(weights_by_partido[pos][s]) for s in SIGNS
                },
                "reparto": signs,
                "kind": kind,
            })

        columnas_out.append({
            "columna_idx": col_idx,
            "partidos": partidos_out,
        })

    # --- 4. Participation tally (unique users across columns + Pleno).
    #
    # Even if a user voted in only some columns (e.g. forgot to submit
    # for column 3), they still count as "voted" for participation
    # purposes.
    users_who_voted: set[int] = set()
    for col_votes in partido_votes_by_column.values():
        for votes in col_votes.values():
            for v in votes:
                users_who_voted.add(v.user_id)
    for v in pleno_votes:
        users_who_voted.add(v.user_id)

    # --- 5. Cost + columns count by modo.
    #
    # The "columns" / "cost_eur" fields on the respuesta are the same
    # values used by PorraOut / BoletoOut / formats preview — the
    # propuesta echoes them so clients can render the cost preview
    # without a second trip.
    if modo in (PorraModo.multiple, PorraModo.reducida) or (
        isinstance(modo, str) and modo in ("multiple", "reducida")
    ):
        columns_played = column_cost(dobles, triples)
        cost_eur = total_cost(dobles, triples, precio_apuesta_eur)
    else:
        # sencilla / intuicion — N independent columns at the per-
        # column price (slice 1 cost model).
        columns_played = n_columns
        cost_eur = Decimal(n_columns) * Decimal(precio_apuesta_eur)

    return {
        "porra_id": porra_id,
        "modo": modo.value if isinstance(modo, PorraModo) else str(modo),
        "fijos": fijos,
        "columnas": columnas_out,
        "pleno": {
            "local": pleno_propuesta.local,
            "visitante": pleno_propuesta.visitante,
        },
        "participation": {"voted": len(users_who_voted)},
        "columns": columns_played,
        "cost_eur": cost_eur,
    }


def _all_simple_kind_signs(
    weights_by_partido: dict[int, dict[str, float]],
) -> dict[int, tuple[str, list[str]]]:
    """Mark every partido as ``simple`` with the single top sign by weight.

    Used for ``sencilla`` (every match is a simple by definition) and
    ``intuicion`` (where variation lives ACROSS columns, not within a
    row). Tie-break: empty weights default to ``["1"]`` (matches the
    existing proposal-level fallback for partidos with no votes).
    """
    out: dict[int, tuple[str, list[str]]] = {}
    for pos in sorted(weights_by_partido.keys()):
        ranked = _sort_signs_desc(weights_by_partido[pos])
        signs = [r[0] for r in ranked[:1]] if ranked else ["1"]
        if not signs:
            signs = ["1"]
        out[pos] = ("simple", signs)
    return out


# Re-export for callers.
__all__ = [
    "PartidoVote",
    "PlenoVote",
    "assign_format_to_partidos",
    "column_cost",
    "compute_pleno_propuesta",
    "compute_propuesta_for_porra",
    "compute_vote_weights",
    "feasible_formats",
    "max_columns_for_budget",
    "total_cost",
]