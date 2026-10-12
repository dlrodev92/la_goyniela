"""Pydantic DTOs for the MVP-05 porra model.

DTOs:

- ``PorraCreateIn``        — admin POST body: jornada + budget + format
- ``PorraPatchIn``         — admin PATCH body (partial): budget or format
- ``PorraOut``             — read shape: porra summary (no picks/boleto detail)
- ``PorraDetailOut``       — read shape with formats preview + cost + status
- ``PorraFormatOut``       — one feasible (D, T) pair from /porra-formats
- ``IntuicionOptionOut``   — one feasible (columnas, fijos) pair from /intuicion-options
- ``IntuicionOptionsOut``  — read wrapper: budget + the list of options
- ``PicksSubmitIn``        — member POST body: N columns + 1 pleno (sibling)
- ``PickPartidoIn``        — LEGACY single-partido shape (kept for back-compat,
                            no longer used by ``PicksSubmitIn``)
- ``PlenoIn``              — partido 15 (pleno) as a SIBLING field in a submission
- ``PicksOut``             — read shape: members + show_picks + picks (when revealed)
- ``MemberPicksOut``       — one member + their picks
- ``PickOut``              — one partido + signs/pleno (read shape, includes team IDs)
- ``PropuestaOut``         — admin POST /propuesta response (computed media, not persisted)
- ``PropuestaPartidoOut``  — one partido in the propuesta response
- ``BoletoIn``             — admin PUT /boleto body: matches + pleno
- ``BoletoMatchIn``        — one partido in a boleto
- ``BoletoOut``            — read shape: the persisted final boleto + columns/cost

The pick shape (MVP-05) is FREE-FORM: for partidos 1-14 the ``signs`` list
can be 1, 2, or 3 unique elements from ``{"1","X","2"}`` regardless of
the porra's format (D, T). The format only governs how the FINAL boleto
the admin saves plays (i.e., how many columns of the multiplication).

For partido 15 (pleno al 15) the shape is ``{local, visitante}`` where
each is in ``{"0","1","2","M"}`` (M = "more than 3 goals"). This replaces
the old MVP-04 ``pleno_signs`` M0/M1/M2 JSONB dict.

Cost model:

- ``multiple`` mode: ``column_cost(D, T) = 2^D × 3^T``, total =
  ``column_cost × precio_apuesta_eur``. Same cost model as Quiniela real.
- ``intuicion`` mode (oct 2026 slice 1): ``column_cost = columnas`` (set
  explicitly by the admin), total = ``columnas × precio_apuesta_eur``.
  Each column costs the per-column price.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import PorraModo
from app.schemas.jornada import TeamSummary
from app.services.reducciones import REDUCCIONES


# ---------- Sign / value enums (single source of truth) ----------

# Signs for partidos 1-14. Free-form: 1, 2, or 3 of these in a list.
Sign = Literal["1", "X", "2"]
SIGNS: tuple[str, ...] = ("1", "X", "2")

# Values for partido 15 pleno. Each value ∈ {"0","1","2","M"}.
PlenoValue = Literal["0", "1", "2", "M"]
PLENO_VALUES: tuple[str, ...] = ("0", "1", "2", "M")


# ---------- Porra create / patch / read ----------


class PorraCreateIn(BaseModel):
    """Admin POST body — create a new porra for (group, jornada).

    The mode (``modo``) picks how the FINAL boleto is generated. Each mode
    carries its own required fields:

    - ``multiple``  (default) — ``dobles`` + ``triples`` ≤ 14 and
      ``not (0, 0)``. The minimum Quiniela boleto is 2 columns (1 doble).
      ``(dobles=0, triples=0)`` is rejected at validation time AND at the
      DB CHECK level (migration ``f4e5d6c7b8a9_porra_min_columns``).
    - ``sencilla``  — ``columnas`` (≥ 2). The boleto plays N independent
      columns of simples (every match in every column has 1 sign).
      ``columnas`` may be omitted on the body if the target jornada has
      ``default_columnas`` configured — the endpoint picks up the default.
    - ``reducida``  — ``reduccion`` (one of R1..R6) is required. The
      ``dobles``/``triples`` are derived from the reduction's spec and
      MUST match the values from ``app.services.reducciones`` at
      validation time; clients usually send ``dobles=triples=0`` and
      the endpoint stamps the correct values from the spec.
    - ``intuicion`` — ``fijos`` is required. ``columnas`` may be omitted
      on the body if the target jornada has ``default_columnas``
      configured — the endpoint picks up the default. Field bounds:
      ``0 ≤ fijos ≤ 13``, ``2 ≤ columnas ≤ 12`` (both inclusive).
      Cost = ``columnas × precio_apuesta_eur`` ≤ ``budget_eur``.

    ``precio_apuesta_eur`` defaults to 0.50 in the endpoint (it tries to
    read the live Losilla price first); Pydantic only validates positivity.

    Oct-2026 multi-column sprint: ``columnas`` is now optional at the
    schema layer for ``sencilla`` / ``intuicion`` so the jornada-level
    ``default_columnas`` can fill in when the admin doesn't override
    it. The endpoint applies the default + raises 422 if BOTH are
    missing.
    """
    model_config = ConfigDict(extra="forbid")

    jornada_id: int = Field(..., ge=1)
    budget_eur: Decimal = Field(..., gt=0)
    modo: PorraModo = Field(
        default=PorraModo.multiple,
        description=(
            "How the FINAL boleto is generated. multiple (default, legacy), "
            "sencilla, reducida, or intuicion."
        ),
    )
    dobles: int = Field(0, ge=0, le=14)
    triples: int = Field(0, ge=0, le=14)
    fijos: int | None = Field(default=None, ge=0, le=13)
    columnas: int | None = Field(default=None, ge=2, le=12)
    reduccion: str | None = Field(
        default=None,
        description=(
            "Only used when modo='reducida'. One of "
            f"{sorted(REDUCCIONES.keys())}."
        ),
    )
    precio_apuesta_eur: Decimal | None = Field(
        default=None, gt=0,
        description=(
            "Per-column price; if None the admin endpoint defaults to 0.50 "
            "or the live Losilla value for the jornada."
        ),
    )

    @model_validator(mode="after")
    def _validate_dt(self) -> "PorraCreateIn":
        # ``multiple`` keeps the legacy D/T rules.
        if self.modo == PorraModo.multiple:
            if self.dobles == 0 and self.triples == 0:
                raise ValueError(
                    "minimum format is 2 columns (1 doble OR 1 triple). "
                    "Quiniela does not sell 1-column boletos — set dobles=1 "
                    "or triples=1 (or higher)."
                )
            if self.dobles + self.triples > 14:
                raise ValueError(
                    f"dobles ({self.dobles}) + triples ({self.triples}) must be ≤ 14"
                )
            return self
        # ``sencilla`` — N independent columns of simples. ``columnas``
        # is OPTIONAL at the schema layer; the endpoint falls back to
        # the jornada's ``default_columnas`` and 422s if BOTH are
        # missing. Field-level bounds (``columnas ≥ 2 ≤ 12``) check the
        # admin's explicit value when set.
        if self.modo == PorraModo.sencilla:
            if self.columnas is not None:
                precio = self.precio_apuesta_eur or Decimal("0.50")
                cost = Decimal(self.columnas) * precio
                if cost > self.budget_eur:
                    raise ValueError(
                        f"modo='sencilla': cost ({self.columnas} × "
                        f"{precio}€ = {cost}€) exceeds the budget "
                        f"{self.budget_eur}€"
                    )
            return self
        # ``reducida`` — pick one of R1..R6. The reduction's D/T + cost
        # table is in ``app.services.reducciones``. We require ``reduccion``
        # to be set; the endpoint stamps the correct D/T onto the porra
        # from the spec (the client may send 0/0; we overwrite).
        if self.modo == PorraModo.reducida:
            if self.reduccion is None:
                raise ValueError(
                    "modo='reducida' requires 'reduccion' (one of "
                    f"{sorted(REDUCCIONES.keys())})"
                )
            if self.reduccion not in REDUCCIONES:
                raise ValueError(
                    f"unknown reduccion code: {self.reduccion!r}; "
                    f"valid: {sorted(REDUCCIONES.keys())}"
                )
            spec = REDUCCIONES[self.reduccion]
            # The spec sets the exact (D, T). We require the client to
            # either send them or 0/0; in the latter case the endpoint
            # stamps the spec values. Validate the cost-vs-budget here
            # so a too-small budget is rejected at POST time (not only
            # when the admin tries to PUT a boleto).
            spec_cost = Decimal(spec.apuestas) * (
                self.precio_apuesta_eur or Decimal("0.50")
            )
            if spec_cost > self.budget_eur:
                raise ValueError(
                    f"modo='reducida' {self.reduccion}: cost "
                    f"({spec.apuestas} × {self.precio_apuesta_eur or Decimal('0.50')}€"
                    f" = {spec_cost}€) exceeds the budget {self.budget_eur}€"
                )
            return self
        # ``intuicion`` carries its own validation: ``fijos`` is
        # required; ``columnas`` is optional (falls back to the
        # jornada's ``default_columnas`` at the endpoint). We DON'T
        # enforce ``fijos < columnas`` here because the slice-1
        # budget-advisor table (see ``INTUICION_OPTIONS`` in
        # ``app/api/v1/porras.py``) pairs a small column count with a
        # high fijos count — those are valid Quiniela boletos (13 of
        # 14 partidos locked, the 14th varies over 3 signs, but the
        # boleto plays only 2 columns). Field-level constraints
        # (``fijos: int ≥ 0 ≤ 13``, ``columnas: int ≥ 2 ≤ 12``) handle
        # the lower/upper bounds when the admin supplies an explicit
        # value.
        if self.modo == PorraModo.intuicion:
            if self.fijos is None:
                raise ValueError(
                    "modo='intuicion' requires 'fijos' (number of parties "
                    "locked to the same sign in every column, 0..13)"
                )
            if self.columnas is not None:
                precio = self.precio_apuesta_eur or Decimal("0.50")
                cost = Decimal(self.columnas) * precio
                if cost > self.budget_eur:
                    raise ValueError(
                        f"modo='intuicion': cost ({self.columnas} × "
                        f"{precio}€ = {cost}€) exceeds the budget "
                        f"{self.budget_eur}€"
                    )
            return self
        return self


class PorraPatchIn(BaseModel):
    """Admin PATCH body — partial update to budget / format while open.

    ``precio_apuesta_eur`` is NOT patchable here (it's a fact from Losilla).
    All fields optional; missing fields keep their current value.

    Mode-specific rules:

    - ``modo`` may flip between modes (e.g. multiple → intuicion). The
      mode-specific fields (``dobles``/``triples`` for multiple,
      ``fijos``/``columnas`` for intuicion, ``reduccion`` for reducida)
      are applied against the CURRENT persisted state. The endpoint
      re-validates the merged state after the patch, so a partial
      patch that omits a required field passes Pydantic and only fails
      at the endpoint-level re-validation (where the merged porra is
      known).
    """
    model_config = ConfigDict(extra="forbid")

    budget_eur: Decimal | None = Field(default=None, gt=0)
    modo: PorraModo | None = Field(default=None)
    dobles: int | None = Field(default=None, ge=0, le=14)
    triples: int | None = Field(default=None, ge=0, le=14)
    fijos: int | None = Field(default=None, ge=0, le=13)
    columnas: int | None = Field(default=None, ge=2, le=12)
    reduccion: str | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_dt(self) -> "PorraPatchIn":
        # ``multiple`` shape only — the endpoint re-validates the merged
        # state (D + T ≤ 14) after the patch.
        if self.modo is None or self.modo == PorraModo.multiple:
            d = self.dobles
            t = self.triples
            if d is not None and t is not None and (d + t) > 14:
                raise ValueError(
                    f"dobles ({d}) + triples ({t}) must be ≤ 14"
                )
            return self
        # ``reducida`` requires the code to be a known spec.
        if self.modo == PorraModo.reducida and self.reduccion is not None:
            if self.reduccion not in REDUCCIONES:
                raise ValueError(
                    f"unknown reduccion code: {self.reduccion!r}; "
                    f"valid: {sorted(REDUCCIONES.keys())}"
                )
        # ``intuicion`` doesn't enforce ``fijos < columnas`` here either
        # (see PorraCreateIn for the rationale — the budget-advisor
        # table pairs high fijos with low columnas). Field-level
        # constraints handle the bounds. The endpoint re-validates the
        # merged state (columnas × precio ≤ budget + the DB CHECK on
        # modo='intuicion' requires both fields).
        return self


class PorraFormatOut(BaseModel):
    """One feasible (D, T) pair returned by /porra-formats.

    Ordered by columns DESC (best coverage first); ties broken by
    triples DESC then dobles DESC. Each ``columns`` = ``2^D × 3^T``;
    ``coste_eur`` = ``columns × precio_apuesta_eur``.
    """
    model_config = ConfigDict(extra="forbid")

    dobles: int
    triples: int
    columns: int
    coste_eur: Decimal


class IntuicionOptionOut(BaseModel):
    """One feasible (columnas, fijos) pair returned by /intuicion-options.

    The budget-advisor table picks 11 pairs from 2 cols × 13 fijos up to
    12 cols × 7 fijos, all priced at ``columnas × 0.75€`` (the slice-1
    ``intuicion`` rate; the per-column price from ``precio_apuesta_eur``
    is unchanged from the other modes).

    ``variables`` is the convenience counter ``14 - fijos`` (the number
    of partidos on the 14-partido boleto that the peña does NOT lock to
    a single sign). Always in 0..14 and matches the Quiniela mental
    model: "13 fijos, 1 variable" means 13 partidos locked + 1 partido
    that the peña varies across the N columns. Independent of
    ``columnas`` — varying a single partido over 2 or 12 columns is
    both valid; the columns count just sets the cost.
    """
    model_config = ConfigDict(extra="forbid")

    columnas: int = Field(..., ge=2, le=12)
    fijos: int = Field(..., ge=0, le=13)
    variables: int = Field(..., ge=1, le=14)
    cost_eur: Decimal


class IntuicionOptionsOut(BaseModel):
    """Read wrapper for /intuicion-options — budget + the options list.

    ``options`` is sorted by ``columnas`` DESC (best coverage first).
    Empty list if no option fits the budget (defensive — even at budget
    €0 the 2-column row costs €1.50 and never fits).
    """
    model_config = ConfigDict(extra="forbid")

    budget_eur: Decimal
    options: list[IntuicionOptionOut] = Field(default_factory=list)


class PorraOut(BaseModel):
    """Read shape for a porra — summary only (no picks, no boleto detail).

    ``modo`` is the boleto mode (multiple | reducida | sencilla | intuicion);
    ``fijos`` and ``columnas`` are populated when ``modo='intuicion'`` (they
    describe the user's pre-locked vs varying parties). For ``multiple``
    and ``reducida`` they are ``None`` and the column count is computed
    from ``dobles`` × ``triples`` via ``column_cost``. ``columns`` on the
    read shape is the unified value used everywhere: for ``intuicion`` it
    echoes ``porra.columnas``; for the other modes it's the computed value.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    group_id: int
    jornada_id: int
    created_by_user_id: int
    budget_eur: Decimal
    dobles: int
    triples: int
    precio_apuesta_eur: Decimal
    modo: PorraModo
    fijos: int | None
    columnas: int | None
    status: str
    created_at: datetime
    updated_at: datetime
    columns: int
    cost_eur: Decimal
    has_boleto: bool


class PorraDetailOut(PorraOut):
    """Read shape with formats preview + participation + boleto flag.

    ``formats_preview`` is the list of feasible (D, T) pairs at the
    current budget (same shape as ``/porra-formats``). Lets the admin
    form show "here are 3 formats that fit" without a second round trip.

    ``participation`` is the participation tally for the CURRENT picks.
    ``missing_voters`` is the list of members who haven't voted yet.
    """
    model_config = ConfigDict(from_attributes=True)

    formats_preview: list[PorraFormatOut] = Field(default_factory=list)
    participation: "ParticipationOut | None" = None


class ParticipationOut(BaseModel):
    """How many members of the peña have voted for this porra."""
    model_config = ConfigDict(extra="forbid")

    voted: int
    total_members: int
    missing_voters: list["MissingVoterOut"] = Field(default_factory=list)


class MissingVoterOut(BaseModel):
    """A member who has not yet voted for this porra."""
    model_config = ConfigDict(extra="forbid")

    user_id: int
    display_name: str


# ---------- Pick shape (MVP-05) ----------


class PickPartidoIn(BaseModel):
    """One partido 1-14 in a submission.

    Free-form: ``signs`` is 1, 2, or 3 unique elements from ``SIGNS``.
    The porra's format (D, T) does NOT constrain this — every member can
    vote 1, 2, or 3 signs on any partido regardless of the format. The
    format only governs how many columns the FINAL BOLETO plays.
    """
    model_config = ConfigDict(extra="forbid")

    partido_orden: int = Field(..., ge=1, le=14)
    signs: list[Sign]

    @model_validator(mode="after")
    def _validate_signs(self) -> "PickPartidoIn":
        if len(self.signs) == 0:
            raise ValueError(
                f"partido_orden={self.partido_orden}: signs must be non-empty",
            )
        if len(self.signs) > 3:
            raise ValueError(
                f"partido_orden={self.partido_orden}: signs has "
                f"{len(self.signs)} elements; max 3",
            )
        if len(set(self.signs)) != len(self.signs):
            raise ValueError(
                f"partido_orden={self.partido_orden}: signs contains "
                f"duplicates ({self.signs})",
            )
        return self


class PlenoIn(BaseModel):
    """Pleno al 15 — goals for local + visitante.

    Each ∈ {"0","1","2","M"} (M = "more than 3 goals"). Default to "0"
    is NOT implicit — the client must send both values (empty pleno is
    rejected at the schema level to match Quiniela real's rule).
    """
    model_config = ConfigDict(extra="forbid")

    local: PlenoValue
    visitante: PlenoValue


class PicksSubmitIn(BaseModel):
    """Member POST body — submit / replace the current user's picks.

    Multi-column shape (oct 2026 sprint): the member submits N columns
    of predictions plus one shared Pleno al 15. The Pleno is SINGLE
    across all columns of the same submission — not per column. The
    number of columns required depends on the porra's ``modo``:

    - ``modo='multiple'`` or ``modo='reducida'``: exactly 1 column.
    - ``modo='sencilla'`` or ``modo='intuicion'``: exactly
      ``porra.columnas`` columns.

    Field shapes (legacy alias + new columns shape are both accepted):

    - ``columnas: [BoletoColumnaIn, ...]`` — the canonical shape. Each
      ``BoletoColumnaIn`` has 14 rows for partidos 1..14.
    - ``matches: [BoletoMatchIn, ...]`` — the LEGACY single-column
      alias. Backend wraps it into
      ``columnas=[BoletoColumnaIn(matches=...)]`` so old single-column
      clients still work.

    The Pleno (partido 15) is a SIBLING ``pleno`` field on the body,
    independent of which shape above is used.
    """
    model_config = ConfigDict(extra="forbid")

    columnas: list["BoletoColumnaIn"] | None = None
    matches: list[BoletoMatchIn] | None = None
    pleno: PlenoIn

    @model_validator(mode="after")
    def _coalesce(self) -> "PicksSubmitIn":
        if self.columnas is None and self.matches is None:
            raise ValueError(
                "must provide either 'columnas' or 'matches' (legacy alias)",
            )
        if self.columnas is not None and self.matches is not None:
            raise ValueError(
                "provide either 'columnas' or 'matches' (legacy alias), not both",
            )
        return self


class PickOut(BaseModel):
    """Read shape for one pick — same MVP-02 enrichment as the old pick.

    Picks are still per-partido: ``partido_orden`` + ``signs`` (list, 1-14)
    or ``pleno_local`` + ``pleno_visitante`` (partido 15). Includes the
    composicion team's IDs and embedded ``TeamSummary`` so the UI can
    render the partido next to each pick without hitting
    ``/api/v1/teams/{id}`` 30 times.
    """
    model_config = ConfigDict(from_attributes=True)

    partido_orden: int
    signs: list[str] | None = None
    pleno_local: str | None = None
    pleno_visitante: str | None = None
    # Enriched from composicion — never Optional, the FK guarantees the
    # composicion row exists for every pick's partido_orden.
    local_team_id: int
    visitante_team_id: int
    local_team: TeamSummary | None = None
    visitante_team: TeamSummary | None = None


class MemberPicksOut(BaseModel):
    """One member + their picks for a porra.

    ``voted`` is always set (true iff at least one pick row exists for
    this user+porra). ``picks`` is populated only when ``show_picks`` is
    True AND the member voted — otherwise it is null so the API never
    leaks private picks.
    """
    user_id: int
    display_name: str
    voted: bool
    voted_at: datetime | None = None
    picks: list[PickOut] | None = None


class PicksOut(BaseModel):
    """Wrapper for GET /picks — members + show_picks flag.

    Same shape as the MVP-02 GroupPicksOut but renamed for the porra
    context. ``porra_id`` echoes the scope.
    """
    porra_id: int
    members: list[MemberPicksOut]
    show_picks: bool


# ---------- Propuesta (computed media, not persisted) ----------


class PropuestaPartidoOut(BaseModel):
    """One partido in one column of the propuesta (partidos 1..14).

    Shape per row:

    - ``partido_orden`` — 1..14
    - ``vote_counts`` — ``{"1": float, "X": float, "2": float}`` — the
      normalised vote weights for this column (each member contributes
      ``1.0 / len(signos)`` to each of their signs). All three keys are
      always present; missing voters leave them at 0.0.
    - ``reparto`` — top-N signs by weight in THIS column, where ``N``
      depends on ``kind`` (1 for simple, 2 for doble, 3 for triple).
    - ``kind`` — ``"simple"`` | ``"doble"`` | ``"triple"``.

    Partido 15 (Pleno al 15) is NOT in this shape — it's a sibling
    ``pleno`` field on the propuesta, computed once across all members
    (the Pleno is shared per submission, not per column).
    """
    model_config = ConfigDict(extra="forbid")

    partido_orden: int = Field(..., ge=1, le=14)
    vote_counts: dict
    reparto: list[str]
    kind: str


class PropuestaColumnaOut(BaseModel):
    """One column in the propuesta.

    Oct 2026 (multi-column sprint, slice 2): each column is its own
    independiente propuesta. The number of entries in the parent's
    ``columnas`` list depends on the porra mode:

    - ``multiple`` / ``reducida`` — 1 entry (the boleto has 1 column).
    - ``sencilla`` / ``intuicion`` — N entries (``N = porra.columnas``).

    Each entry has 14 rows (partidos 1..14, in order). Partido 15 is
    shared across columns and lives on the parent's top-level
    ``pleno`` field.
    """
    model_config = ConfigDict(extra="forbid")

    columna_idx: int = Field(..., ge=1)
    partidos: list[PropuestaPartidoOut]


class PropuestaOut(BaseModel):
    """Computed media — NOT persisted.

    The admin uses this as the starting point for the final boleto. The
    participation block lists who hasn't voted yet; the admin can still
    confirm the boleto even with ``missing_voters`` (per spec P3 — yes,
    even with missing voters).

    **Multi-column shape (oct 2026, slice 2):** partidos 1..14 are
    grouped into one ``PropuestaColumnaOut`` per ``columna_idx``
    (1 entry for ``multiple`` / ``reducida``, ``N = porra.columnas``
    entries for ``sencilla`` / ``intuicion``). The Pleno al 15 is
    SHARED across columns — it sits on the top-level ``pleno`` field,
    NOT inside ``columnas`` — and is computed from the
    deduplicated-by-user Pleno votes.

    ``columns`` + ``cost_eur`` echo the boleto's column count and cost
    (mirrors ``PorraOut.columns`` / ``PorraOut.cost_eur`` so the UI
    can render the cost preview without a second trip to the API).
    ``modo`` is the pass-through porra mode; ``fijos`` is the
    intuicion-only lock count (``None`` for the other modes).
    """
    model_config = ConfigDict(extra="forbid")

    porra_id: int
    modo: str
    fijos: int | None = None
    columnas: list[PropuestaColumnaOut]
    pleno: PlenoIn
    participation: ParticipationOut
    columns: int
    cost_eur: Decimal


# ---------- Boleto (the FINAL boleto, persisted on the porra) ----------


class BoletoMatchIn(BaseModel):
    """One partido in the admin's final boleto.

    ``signs`` must be 1, 2, or 3 unique elements from ``SIGNS``. The
    per-match kind is implicit: 1 sign = simple, 2 = doble, 3 = triple.
    The endpoint validates the count of dobles / triples matches the
    porra's ``dobles`` / ``triples`` (i.e., the saved boleto must use
    exactly the same format as the porra).
    """
    model_config = ConfigDict(extra="forbid")

    partido_orden: int = Field(..., ge=1, le=14)
    signs: list[Sign]

    @model_validator(mode="after")
    def _validate_signs(self) -> "BoletoMatchIn":
        if len(self.signs) == 0:
            raise ValueError(
                f"partido_orden={self.partido_orden}: signs must be non-empty",
            )
        if len(self.signs) > 3:
            raise ValueError(
                f"partido_orden={self.partido_orden}: signs has "
                f"{len(self.signs)} elements; max 3",
            )
        if len(set(self.signs)) != len(self.signs):
            raise ValueError(
                f"partido_orden={self.partido_orden}: signs contains "
                f"duplicates ({self.signs})",
            )
        return self


class BoletoColumnaIn(BaseModel):
    """One column in the boleto. Always 14 rows for partidos 1..14.

    For multiple/reducida, a boleto has 1 column. For sencilla, it has
    N independent columns of simples. For intuicion, it has N columns
    where the ``fijos`` partidos share the same sign across all N.
    """
    model_config = ConfigDict(extra="forbid")
    matches: list[BoletoMatchIn] = Field(..., min_length=14, max_length=14)

    @model_validator(mode="after")
    def _check_14_rows_unique(self) -> "BoletoColumnaIn":
        ordenes = [m.partido_orden for m in self.matches]
        if sorted(ordenes) != list(range(1, 15)):
            raise ValueError(
                "each column must have exactly 14 rows for partidos 1..14 "
                f"(got ordenes={ordenes!r})"
            )
        return self


class BoletoIn(BaseModel):
    """Admin PUT /boleto body — N columns + pleno.

    New shape: ``columnas: [BoletoColumnaIn, ...]`` (1+ columns).
    Legacy alias: ``matches: list[BoletoMatchIn]`` is accepted as a single
    column for backwards compat with old clients (wrap into
    ``[BoletoColumnaIn(matches=...)]`` internally).
    """
    model_config = ConfigDict(extra="forbid")
    columnas: list[BoletoColumnaIn] | None = None
    matches: list[BoletoMatchIn] | None = None
    pleno: PlenoIn

    @model_validator(mode="after")
    def _check_one_of(self) -> "BoletoIn":
        if self.columnas is None and self.matches is None:
            raise ValueError("must provide either 'columnas' or 'matches' (legacy)")
        return self


class BoletoOut(BaseModel):
    """GET /boleto response — N columns + pleno + format info."""
    model_config = ConfigDict(extra="forbid")
    porra_id: int
    modo: PorraModo
    set_at: datetime
    set_by_user_id: int | None
    columnas: list[BoletoColumnaIn]
    pleno: PlenoIn
    columns: int
    cost_eur: Decimal
    reduccion: str | None = None   # only set when modo='reducida'


# Forward references resolution (Pydantic v2 strict mode).
ParticipationOut.model_rebuild()
MemberPicksOut.model_rebuild()