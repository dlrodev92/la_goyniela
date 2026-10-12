"""Pydantic DTOs for Eduardo Losilla escrutinio data.

Shapes exposed by ``app.clients.eduardolosilla``. All monetary fields are kept
as strings with EU thousands/decimal formatting (``"1.453.174,50"``) to avoid
rounding the source; consumers can parse with ``float(s.replace(".", "").replace(",", "."))`` or ``decimal.Decimal``."""

from __future__ import annotations

import datetime as dt
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

class A:
    pass

class B:
    x: int = None
    y: str = None

class C:
    a: int = 1
    b: int = 2

class C:
    """A docstring."""

# comment

    # an indented comment

class C:
    """A docstring."""
    # a comment

class C:
    """Docstring with content."""
    a: int = 1
    # a comment

# ---------- Match-level ----------

class PartidoLosilla(BaseModel):
    """One match in a Quiniela jornada, with its editorial comentario if any.
    partido 15 (the ``Pleno al 15`` international match) usually has
    ``comentario=None``; only the 14 club matches carry an editorial."""
    model_config = ConfigDict(from_attributes=True)

    orden: int = Field(..., ge=1, le=15, description="1..15 position on the boleto")
    num: int = Field(..., ge=1, le=15)
    local: str
    local_corto: str | None = None
    visitante: str
    visitante_corto: str | None = None
    dia: str | None = Field(None, description="SAB|DOM|LUN|MIE|JUE|VIE")
    hora: str | None = Field(None, description="HH:MM local")
    resultado: str | None = Field(
        None, description="For played matches e.g. '2-1'; 'null-null' while open")
    escudo_local: str | None = None
    escudo_visitante: str | None = None
    id_besoccer: int | None = None
    comentario: str | None = Field(
        None, description="Editorial comentario; None for partido 15 / pleno")
    # Per-partido 1..14 1X2 percentages (slice 7 — gestor-jornadas.md §6.2).
    # Populated from the SSR ``probabilidades_*`` block when upstream is live;
    # ``None`` when the upstream didn't ship probabilities (past-season
    # pages, off-season, etc.). Values are whole-percent ints upstream; the
    # JSONB serialises them as ints and ``PartidoEstimacion`` re-coerces to
    # ``float`` on the persist schema side.
    uno_porcentaje: int | None = Field(
        None, description="Partidos 1..14 only. 1X2 percentage for '1' (local wins).")
    x_porcentaje: int | None = Field(
        None, description="Partidos 1..14 only. 1X2 percentage for 'X' (draw).")
    dos_porcentaje: int | None = Field(
        None, description="Partidos 1..14 only. 1X2 percentage for '2' (visitante wins).")
    pleno_local: dict[str, int | None] | None = Field(
        None, description=(
            "Partido 15 only. Splits 0/1/2/M for local; values are int "
            "percentages or None if upstream shipped '-'."))
    pleno_visitante: dict[str, int | None] | None = Field(
        None, description=(
            "Partido 15 only. Splits 0/1/2/M for visitante; values are int "
            "percentages or None if upstream shipped '-'."))


# ---------- Escrutinio ----------


class CategoriaPremio(BaseModel):
    """One row in the escrutinio table: aciertos / acertantes / premio_eur."""
    aciertos: int = Field(..., ge=10, le=15)
    acertantes: int = Field(..., ge=0)
    premio_eur: str


class Elige8Premio(BaseModel):
    """Elige 8 secondary row in the escrutinio block."""
    acertantes: int = Field(..., ge=0)
    premio_eur: str


class EscrutinioOficial(BaseModel):
    """The escrutinio block as rendered in the Losilla HTML body.

    Also carries the live SSR block (``recaudacion_bote_live``) which is
    the canonical home of the per-ticket price (``coste_apuesta_eur``).
    Pre-oct-2026, this field was missing and the backend fell back to
    the 0.50€ default while the client forced 0.75€ from the live page
    (mismatch). The live block is now persisted alongside the
    escrutinio so the price resolver finds it.
    """
    estimada: bool
    recaudacion_eur: str
    bote_eur: str
    jornada_etiqueta: int = Field(..., ge=1)
    temporada_etiqueta: str
    fecha_jornada: str
    categorias: list[CategoriaPremio]
    elige8: Elige8Premio | None = None
    # Live SSR block (price + bote). Optional because pre-oct-2026 rows
    # don't have it; new ingests always populate it from the upstream
    # ``probabilidades_*`` state.
    recaudacion_bote_live: RecaudacionBoteLive | None = None
    # Slice 7 (gestor-jornadas.md §6.2): live 1X2 + Pleno al 15 percentages
    # for the 15 matches, captured at ingest time from the SSR
    # ``probabilidades_*`` block and persisted in this JSONB. Defaults to
    # ``[]`` for pre-slice-7 rows that predate the Edit; ``field_validator``
    # below coerces an explicit ``null`` (legacy hand-written JSON) to ``[]``
    # so old payloads still round-trip cleanly.
    estimaciones: list["PartidoEstimacion"] = Field(
        default_factory=list,
        description=(
            "Per-partido live percentages (1..14 1X2 + 15 Pleno al 15), "
            "captured from the SSR probabilidades block at ingest time."),
    )

    @field_validator("estimaciones", mode="before")
    @classmethod
    def _coerce_none_estimaciones(cls, v):
        # Older rows may have been hand-written with ``"estimaciones": null``
        # before the slice 7 migration; treat that as an empty list.
        return [] if v is None else v


class PartidoEstimacion(BaseModel):
    """Live percentages for ONE partido, persisted at ingest time.

    Shape covers both kinds of partido on the Quiniela boleto (slice 7 of
    gestor-jornadas.md §6.2):

    - **Partidos 1..14 (1X2)**: ``uno_porcentaje``/``x_porcentaje``/
      ``dos_porcentaje`` carry the upstream live 1X2 distribution. Any of
      the three may be ``None`` when upstream didn't ship that key.
    - **Partido 15 (Pleno al 15)**: ``pleno_local``/``pleno_visitante`` are
      dicts keyed ``{"0","1","2","M"}`` with the 0/1/2/M splits for each
      side. The design-doc type is ``dict[str, float]``; upstream ships
      whole-percent ints and we keep them as ints in the JSONB (``float``
      is permissive — Pydantic auto-coerces on read). Missing keys (None)
      are stripped before persisting so the model never carries ``None``
      values inside the dict.

    ``fuente`` is the literal string ``"losilla_live"`` for now — only one
    upstream source is in scope (no other fuente data feeds a future
    ingestion path yet).

    ``fetched_at`` is the UTC timestamp at the moment the ingest parsed
    the SSR block. ISO 8601 on the wire; ``datetime`` in memory.
    """
    partido_orden: int = Field(..., ge=1, le=15)
    # Partidos 1..14 (1X2)
    uno_porcentaje: float | None = None
    x_porcentaje: float | None = None
    dos_porcentaje: float | None = None
    # Partido 15 (Pleno al 15): 0/1/2/M for each team
    pleno_local: dict[str, float] | None = None
    pleno_visitante: dict[str, float] | None = None
    fuente: str = Field(
        default="losilla_live",
        description="Source tag for this estimacion (e.g. 'losilla_live').",
    )
    fetched_at: datetime = Field(
        ..., description="UTC timestamp at which this estimacion was fetched.",
    )

    @staticmethod
    def _strip_none_values(
        d: dict[str, int | float | None] | None,
    ) -> dict[str, float] | None:
        """Return ``d`` with ``None`` entries removed; ``None`` stays ``None``.

        Used to clean Pleno al 15 splits before persisting — the schema
        type is ``dict[str, float]`` (no ``None``), so we drop keys
        upstream shipped as ``'-'``. Returns ``None`` when the input is
        ``None`` so callers can keep their ``pleno_local=... or None``
        pattern.
        """
        if d is None:
            return None
        cleaned = {k: float(v) for k, v in d.items() if v is not None}
        return cleaned or None

    @classmethod
    def from_partidos(
        cls,
        partidos: list["PartidoLosilla"],
        *,
        fetched_at: dt.datetime,
        fuente: str = "losilla_live",
    ) -> list["PartidoEstimacion"]:
        """Build one estimacion per partido from upstream ``PartidoLosilla``.

        Partidos 1..14 contribute the 1X2 percentages from
        ``PartidoLosilla.uno_porcentaje`` / ``x_porcentaje`` /
        ``dos_porcentaje``; partido 15 contributes the Pleno splits from
        ``pleno_local`` / ``pleno_visitante``. ``None``-valued keys inside
        the Pleno splits are stripped (the schema's ``dict[str, float]``
        has no ``None`` placeholders).

        Pure / no DB; deterministic given the same ``partidos`` list and
        ``fetched_at`` timestamp. Used by both the ingest CLI (when
        persisting) and the upstream-fallback path in
        ``app/api/v1/losilla.get_escrutinio`` — keeping the logic here
        avoids an ``app.scripts`` import in the API module.
        """
        out: list[PartidoEstimacion] = []
        for p in partidos:
            if p.orden is None:
                continue
            out.append(cls(
                partido_orden=p.orden,
                uno_porcentaje=(
                    float(p.uno_porcentaje) if p.uno_porcentaje is not None
                    else None),
                x_porcentaje=(
                    float(p.x_porcentaje) if p.x_porcentaje is not None
                    else None),
                dos_porcentaje=(
                    float(p.dos_porcentaje) if p.dos_porcentaje is not None
                    else None),
                pleno_local=cls._strip_none_values(p.pleno_local),
                pleno_visitante=cls._strip_none_values(p.pleno_visitante),
                fuente=fuente,
                fetched_at=fetched_at,
            ))
        return out


# ---------- Recaudacion / bote (live SSR) ----------


class RecaudacionBoteLive(BaseModel):
    """Live recaudacion + bote block from the SSR state.

    All monetary fields are strings in EU formatting (``"1.453.174,50"``);
    the timestamp fields are UNIX seconds (int) from the upstream payload.
    """
    recaudacion_eur: str | None = None
    bote_estimado_eur: str | None = None
    bote_eur: str | None = None
    texto_bote_eur: str | None = None
    bote_unico_acertante_eur: str | None = None
    coste_apuesta_eur: str | None = None
    fecha_fin_apuestas_ts: int | None = None
    fecha_fin_penas_ts: int | None = None
    caducidad_ts: int | None = None


# ---------- Top-level jornada payload ----------


class JornadaLosilla(BaseModel):
    """Full Losilla payload for one jornada."""
    model_config = ConfigDict(from_attributes=True)

    source: Literal["eduardolosilla_escrutinio"] = "eduardolosilla_escrutinio"
    jornada: int = Field(..., ge=1)
    temporada: int = Field(..., ge=1998)
    url: str
    checked_at: str
    fetched: bool = False
    ssr_shipped_jornada: int | None = None
    warning: str | None = None
    recaudacion_bote_live: RecaudacionBoteLive | None = None
    escrutinio_oficial: EscrutinioOficial | None = None
    partidos: list[PartidoLosilla] = Field(default_factory=list)
    comentarios_presentes: int = 0
    sha256: str | None = None


# ---------- Router response wrappers ----------


class EscrutinioResponse(BaseModel):
    """Wrapper for ``/escrutinio`` responses with provenance tag."""
    source: Literal["db", "upstream"]
    escrutinio: EscrutinioOficial


class ComentariosResponse(BaseModel):
    """Wrapper for ``/comentarios`` responses with provenance tag."""
    source: Literal["db", "upstream"]
    comentarios: list[PartidoLosilla]
