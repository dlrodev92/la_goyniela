"""Async client for eduardolosilla.es escrutinio data.

Parses the Angular Universal SSR page at
``/quiniela/ayudas/escrutinio/jornada_<N>`` into the ``JornadaLosilla``
schema. The page is the live in-flight jornada view: its ``<script>`` SSR
state JSON always reflects the currently open jornada even when the URL
points at a closed one. The HTML-rendered ``<app-tabla-categorias>``
component does carry the requested jornada's real escrutinio. The client
preserves both and exposes ``JornadaLosilla.ssr_shipped_jornada`` to flag
the mismatch.

No external dependencies beyond httpx (already in pyproject). The
parser is ported from ``scripts/probe_losilla_escrutinio.py`` — kept
in sync manually when the upstream HTML schema changes.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
import time
from html import unescape
from pathlib import Path

import httpx

from app.schemas.losilla import (
    CategoriaPremio,
    Elige8Premio,
    EscrutinioOficial,
    JornadaLosilla,
    PartidoLosilla,
    RecaudacionBoteLive,
)

LOG = logging.getLogger("eduardolosilla")

BASE = "https://www.eduardolosilla.es"
USER_AGENT = "LaGoyniela/0.1 (read-only client; contact via repo)"
DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 5_000_000
MIN_THROTTLE_S = 0.4  # courtesy between requests to the same host

# ---------- Pure parsing (no I/O) — reuses the regex toolkit from the script ----------


_EURO_AMOUNT = r"[\d]{1,3}(?:\.\d{3})*,\d{2}"

_TABLA_CATS_PATTERN = re.compile(
    r"Recaudaci[oó]n\s+(?:(?P<estimada>Estimada)\s+)?"
    r"(?P<recaudacion>" + _EURO_AMOUNT + r")\s*\u20AC\s+"
    r"Bote\s+(?P<bote>" + _EURO_AMOUNT + r")\s*\u20AC\s+"
    r"Escrutinio\s+de\s+la\s+Quiniela\s+JORNADA\s+(?P<jornada_num>\d+)\s*"
    r"-\s*(?P<temporada>[^()]+?)\s*\((?P<fecha>\d{2}/\d{2}/\d{4})\)"
    r"(?P<resto>.*?)(?:quini\s*\u2014|Copyright)",
    re.DOTALL | re.IGNORECASE,
)

_FILA_RECOLECCION = re.compile(
    r"(\d+)\s*Aciertos\s+([\d.]+)\s+(" + _EURO_AMOUNT + r")\s*\u20AC",
)

_ELIGE8_RECOLECCION = re.compile(
    r"Elige\s*8\s+(\d+)\s+(?:" + _EURO_AMOUNT + r")?\s*(" + _EURO_AMOUNT
    + r")\s*\u20AC",
)

_COMENTARIO_PATTERN = re.compile(
    r'PARTIDO\s+(\d+)\s*:.*?'
    r'c-detalle-partido-simple__porcentajes__tabs__comentarios[^>]*>\s*'
    r'(.*?)\s*</div>',
    re.DOTALL,
)

_STATE_MARKER = "datosGeneralesQuiniela"
_STATE_KEY_RE = re.compile(r"jornada_(\d+)_(\d+)")


def _strip_tags(html: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", no_tags).strip()


def _parse_state(html: str) -> dict | None:
    """Extract the Angular Universal SSR state JSON.

    The block uses ``&q;`` as a stand-in for the JSON double-quote in
    addition to standard HTML entities. Returns the top-level dict, or
    ``None`` if the marker is missing or the JSON is malformed.
    """
    for block in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        if _STATE_MARKER not in block:
            continue
        try:
            return json.loads(unescape(block).replace("&q;", '"'))
        except json.JSONDecodeError as exc:
            LOG.warning("state JSON parse failed: %s", exc)
            return None
    return None


def _find_jornada_in_state(state: dict, jornada: int, temporada: int) -> dict | None:
    """Locate the jornada dict inside the SSR state.

    Checks the canonical key first, then a swapped-key fallback, then any
    lone ``jornada_*_*`` key the SSR shipped.
    """
    for key in (f"jornada_{jornada}_{temporada}",
                f"jornada_{temporada}_{jornada}"):
        if key in state:
            return state[key]
    keys = [k for k in state if k.startswith("jornada_")]
    if len(keys) == 1:
        return state[keys[0]]
    return None


def _parse_recaudacion_bote(j: dict) -> RecaudacionBoteLive:
    bote_data = j.get("boteData") or {}

    def _s(v):
        """Coerce upstream scalar (int, float, str, None) to str."""
        return None if v is None else str(v)

    return RecaudacionBoteLive(
        recaudacion_eur=_s(j.get("recaudacion")),
        bote_estimado_eur=_s(j.get("boteEstimacion")),
        bote_eur=_s(j.get("bote")),
        texto_bote_eur=_s(j.get("textoBote")),
        bote_unico_acertante_eur=_s(bote_data.get("unico_acertante")),
        coste_apuesta_eur=_s(j.get("costeApuesta")),
        fecha_fin_apuestas_ts=j.get("fechaFinApuestas"),
        fecha_fin_penas_ts=j.get("fechaFinPenas"),
        caducidad_ts=j.get("caducidad"),
    )


def _parse_partidos(
    j: dict,
    *,
    pleno_local: dict[str, int | None] | None = None,
    pleno_visitante: dict[str, int | None] | None = None,
    probs_por_partido: dict[int, dict[str, int | None]] | None = None,
) -> list[PartidoLosilla]:
    out = []
    for p in j.get("partidos") or []:
        is_p15 = p.get("orden") == 15
        # Per-partido 1..14 1X2 percentages (slice 7 — gestor-jornadas.md §6.2).
        # Only populated for partidos 1..14; partido 15 carries the Pleno
        # splits under ``pleno_local`` / ``pleno_visitante`` instead.
        odds: dict[str, int | None] = (
            probs_por_partido.get(p.get("orden")) if probs_por_partido else None
        ) or {}
        out.append(PartidoLosilla(
            orden=p.get("orden"),
            num=p.get("num"),
            local=p.get("local"),
            local_corto=p.get("local_corto"),
            visitante=p.get("visitante"),
            visitante_corto=p.get("visitante_corto"),
            dia=p.get("dia"),
            hora=p.get("hora"),
            resultado=p.get("resultado"),
            escudo_local=p.get("escudo_local"),
            escudo_visitante=p.get("escudo_visitante"),
            id_besoccer=p.get("id_besoccer"),
            comentario=None,
            uno_porcentaje=odds.get("1"),
            x_porcentaje=odds.get("X"),
            dos_porcentaje=odds.get("2"),
            # Pleno al 15 percentages only attach to partido 15 (the SSR
            # only ships porc_15L_* / porc_15V_* under the correspondiente
            # ``probabilidades_{j}_{t}.partidos.real[14]`` block).
            pleno_local=pleno_local if is_p15 else None,
            pleno_visitante=pleno_visitante if is_p15 else None,
        ))
    return out


_PLENO_KEYS = ("0", "1", "2", "M")
_1X2_KEYS = ("1", "X", "2")


def _pleno_side_from_raw(raw: dict, prefix: str) -> dict[str, int | None] | None:
    """Extract one side of the Pleno al 15 splits from the SSR partidos entry.

    ``prefix`` is the key name before the ``0/1/2/M`` suffix, e.g.
    ``"porc_15L_"`` for local or ``"porc_15V_"`` for visitante. Returns a
    dict keyed by ``{"0","1","2","M"}`` with int percentages; ``None`` for
    values upstream shipped as ``"-"`` (or any other non-int). Returns
    ``None`` (sentinel for "no pleno data shipped") only if NONE of the
    4 keys were present on the partido entry — partidos 1..14 will trigger
    this path and get ``pleno_local=None`` on the model.
    """
    out: dict[str, int | None] = {}
    any_present = False
    for k in _PLENO_KEYS:
        v = raw.get(f"{prefix}{k}")
        if v is None:
            out[k] = None
            continue
        any_present = True
        if isinstance(v, bool):
            # ``bool`` is an ``int`` subclass — guard before the int branch.
            out[k] = None
            continue
        if isinstance(v, int):
            out[k] = v
            continue
        s = str(v).strip()
        if s in ("", "-"):
            out[k] = None
            continue
        try:
            out[k] = int(float(s))
        except ValueError:
            out[k] = None
    return out if any_present else None


def _extract_pleno_15_splits(
    state: dict | None, jornada: int, temporada: int,
) -> tuple[dict[str, int | None] | None, dict[str, int | None] | None]:
    """Pull Pleno al 15 splits from the SSR ``probabilidades_*`` block.

    The percentages live at::

        state["probabilidades_{j}_{t}"]["partidos"]["real"][14]

    keyed ``porc_15L_{0,1,2,M}`` and ``porc_15V_{0,1,2,M}`` (integer
    percentages, whole numbers; the ``_dec`` siblings are float and
    ignored — slice 6 of gestor-jornadas §6.1 rounds to whole percent).

    Falls back the same way :func:`_find_jornada_in_state` does: tries
    ``probabilidades_{j}_{t}`` first, then the swapped-key, then any
    lone ``probabilidades_*`` key the SSR shipped. Returns ``(local,
    visitante)``; each may be ``None`` when the SSR didn't ship that
    side (or the whole probabilidades block is absent).
    """
    if not isinstance(state, dict):
        return None, None
    probs: object | None = state.get(f"probabilidades_{jornada}_{temporada}")
    if not isinstance(probs, dict):
        swapped = state.get(f"probabilidades_{temporada}_{jornada}")
        if isinstance(swapped, dict):
            probs = swapped
    if not isinstance(probs, dict):
        # Lone-key fallback — mirror _find_jornada_in_state.
        keys = [k for k in state if k.startswith("probabilidades_")]
        if len(keys) == 1:
            candidate = state.get(keys[0])
            if isinstance(candidate, dict):
                probs = candidate
    if not isinstance(probs, dict):
        return None, None
    partidos = probs.get("partidos")
    if not isinstance(partidos, dict):
        return None, None
    real = partidos.get("real")
    if not isinstance(real, list) or len(real) < 15:
        return None, None
    raw = real[14]
    if not isinstance(raw, dict):
        return None, None
    return (
        _pleno_side_from_raw(raw, "porc_15L_"),
        _pleno_side_from_raw(raw, "porc_15V_"),
    )


def _extract_1x2_por_partido(
    state: dict | None, jornada: int, temporada: int,
) -> dict[int, dict[str, int | None]] | None:
    """Pull the 1X2 percentages for partidos 1..14 from the SSR state.

    The percentages live at the same ``probabilidades_{j}_{t}.partidos.real``
    block the Pleno 15 helper already reads from — see :func:`_extract_pleno_15_splits`.
    Each partido 0..13 (1-indexed positions 1..14) carries keys
    ``porc_1``/``porc_X``/``porc_2`` (int, whole percent). The ``_dec``
    siblings (float) are ignored — slice 7 of gestor-jornadas §6.2 stores
    whole-percent ints in the JSONB and re-coerces to ``float`` on the
    persist side (``PartidoEstimacion``).

    Returns a dict ``{partido_orden: {"1": int|None, "X": int|None, "2": int|None}}``
    keyed 1..14, or ``None`` if the SSR didn't ship the probabilidades block
    at all. Each value is always present once a partido is present (None
     means upstream shipped '-' or omitted the key), so callers can rely on
     ``probs[orden]["1"]`` existing.

    The same key fallback chain as ``_extract_pleno_15_splits`` applies:
    canonical ``probabilidades_{j}_{t}`` → swapped ``probabilidades_{t}_{
    j}`` → lone-key.
    """
    if not isinstance(state, dict):
        return None
    probs: object | None = state.get(f"probabilidades_{jornada}_{temporada}")
    if not isinstance(probs, dict):
        swapped = state.get(f"probabilidades_{temporada}_{jornada}")
        if isinstance(swapped, dict):
            probs = swapped
    if not isinstance(probs, dict):
        keys = [k for k in state if k.startswith("probabilidades_")]
        if len(keys) == 1:
            candidate = state.get(keys[0])
            if isinstance(candidate, dict):
                probs = candidate
    if not isinstance(probs, dict):
        return None
    partidos = probs.get("partidos")
    if not isinstance(partidos, dict):
        return None
    real = partidos.get("real")
    if not isinstance(real, list) or len(real) < 14:
        return None

    def _coerce(v: object) -> int | None:
        """Best-effort coerce upstream scalar to int (whole percent)."""
        if v is None:
            return None
        if isinstance(v, bool):
            # ``bool`` is an ``int`` subclass — guard before the int branch.
            return None
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if s in ("", "-"):
            return None
        try:
            return int(float(s))
        except ValueError:
            return None

    out: dict[int, dict[str, int | None]] = {}
    for idx, raw in enumerate(real[:14]):
        orden = idx + 1
        if not isinstance(raw, dict):
            continue
        out[orden] = {k: _coerce(raw.get(f"porc_{k}")) for k in _1X2_KEYS}
    return out or None


def _parse_comentarios(html: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in _COMENTARIO_PATTERN.finditer(html):
        num = int(m.group(1))
        raw = m.group(2)
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out[num] = text
    return out


def _parse_escrutinio_oficial(html: str) -> EscrutinioOficial | None:
    flat = _strip_tags(html)
    m = _TABLA_CATS_PATTERN.search(flat)
    if not m:
        return None

    categorias: list[CategoriaPremio] = []
    for fila in _FILA_RECOLECCION.finditer(m.group("resto")):
        categorias.append(CategoriaPremio(
            aciertos=int(fila.group(1)),
            acertantes=int(fila.group(2).replace(".", "")),
            premio_eur=fila.group(3),
        ))

    elige8 = None
    e8 = _ELIGE8_RECOLECCION.search(m.group("resto"))
    if e8:
        elige8 = Elige8Premio(
            acertantes=int(e8.group(1)),
            premio_eur=e8.group(2),
        )

    return EscrutinioOficial(
        estimada=bool(m.group("estimada")),
        recaudacion_eur=m.group("recaudacion"),
        bote_eur=m.group("bote"),
        jornada_etiqueta=int(m.group("jornada_num")),
        temporada_etiqueta=m.group("temporada").strip(),
        fecha_jornada=m.group("fecha"),
        categorias=categorias,
        elige8=elige8,
    )


def _merge_comentarios(partidos: list[PartidoLosilla],
                       comentarios: dict[int, str]) -> None:
    for p in partidos:
        c = comentarios.get(p.orden) or comentarios.get(p.num)
        if c:
            p.comentario = c


# ---------- HTTP client with in-memory cache + throttle ----------


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class EduardoLosillaClient:
    """Async client for ``eduardolosilla.es/quiniela/ayudas/escrutinio``.

    Holds an ``httpx.AsyncClient`` shared across calls, an in-memory cache
    keyed by ``(jornada, temporada)``, and a host-wide throttle of at most
    one request every ``MIN_THROTTLE_S`` seconds.

    The cache TTL is applied per entry: entries for the currently open
    ``(shipped_jornada)`` are short-lived (default 5 min) because the
    recaudacion estimate and percentages change; entries for closed
    jornadas use a longer TTL (default 1 h) since the rendered numbers
    are immutable.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        open_cache_ttl_s: float = 300.0,
        closed_cache_ttl_s: float = 3600.0,
        cache_dir: Path | None = None,
    ) -> None:
        # If a client is passed in, we never own its lifecycle. Otherwise we
        # create our own and must ``await close()`` (or use ``async with``).
        if client is None:
            client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        self._client = client
        self._owns_client = True  # always: we replaced None with a fresh one
        self._open_ttl = open_cache_ttl_s
        self._closed_ttl = closed_cache_ttl_s
        self._cache_dir = cache_dir
        self._mem: dict[tuple[int, int], tuple[float, JornadaLosilla]] = {}
        self._last_request_at = 0.0

    async def __aenter__(self) -> "EduardoLosillaClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the owned httpx client. Safe to call multiple times."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- cache helpers ---

    def _cache_valid(self, jornada: int, temporada: int,
                     payload: JornadaLosilla) -> bool:
        key = (jornada, temporada)
        entry = self._mem.get(key)
        if entry is None:
            return False
        stored_at, stored = entry
        # Choose TTL by whether the shipped jornada matches the requested one.
        shipped = stored.ssr_shipped_jornada
        ttl = self._open_ttl if shipped and shipped == jornada else self._closed_ttl
        return (time.monotonic() - stored_at) < ttl

    def _cache_get(self, jornada: int, temporada: int) -> JornadaLosilla | None:
        key = (jornada, temporada)
        entry = self._mem.get(key)
        if entry is None:
            return None
        _, payload = entry
        if self._cache_valid(jornada, temporada, payload):
            return payload.model_copy(update={"fetched": False})
        self._mem.pop(key, None)
        return None

    def _cache_put(self, payload: JornadaLosilla) -> None:
        self._mem[(payload.jornada, payload.temporada)] = (
            time.monotonic(), payload)

    # --- throttle ---

    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = MIN_THROTTLE_S - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    # --- public ---

    async def get_jornada(self, jornada: int, temporada: int = 2027,
                          *, force_fresh: bool = False) -> JornadaLosilla:
        """Return the full Losilla payload for one jornada.

        ``force_fresh=True`` bypasses the in-memory cache but still
        reuses the on-disk HTML when ``cache_dir`` is set, mirroring
        ``scripts/probe_losilla_escrutinio.py --fetch``.
        """
        if not force_fresh:
            cached = self._cache_get(jornada, temporada)
            if cached is not None:
                return cached

        url = f"{BASE}/quiniela/ayudas/escrutinio/jornada_{jornada}"
        html = await self._fetch_html(url, jornada, temporada, force_fresh)
        payload = self._parse(html, jornada, temporada, url)
        self._cache_put(payload)
        return payload

    async def _fetch_html(self, url: str, jornada: int, temporada: int,
                          force_fresh: bool) -> str:
        """Fetch HTML with optional disk cache + throttle."""
        assert self._client is not None
        disk_path: Path | None = None
        if self._cache_dir is not None:
            disk_path = self._cache_dir / f"j_{jornada}_{temporada}.html"
            if not force_fresh and disk_path.exists():
                LOG.debug("disk cache hit %s", disk_path.name)
                return disk_path.read_text(encoding="utf-8", errors="replace")

        await self._throttle()
        LOG.debug("GET %s", url)
        resp = await self._client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise ValueError(f"Response exceeds {MAX_BYTES} bytes")
        html = resp.text
        if disk_path is not None:
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            disk_path.write_text(html, encoding="utf-8")
        return html

    # --- pure parsing entry point (exposed for tests) ---

    def _parse(self, html: str, jornada: int, temporada: int, url: str) -> JornadaLosilla:
        state = _parse_state(html)
        live: RecaudacionBoteLive | None = None
        shipped: int | None = None
        warning: str | None = None
        partidos: list[PartidoLosilla] = []
        # Pleno al 15 percentages live in a sibling block of the SSR state
        # (probabilidades_{j}_{t}.partidos.real[14]); pull them BEFORE
        # building partidos so we can attach to the p15 entry only.
        pleno_local, pleno_visitante = _extract_pleno_15_splits(
            state, jornada, temporada,
        )
        # Slice 7 (gestor-jornadas.md §6.2): pull the 1X2 percentages for
        # partidos 1..14 from the same probabilidades_* sub-tree.
        probs_por_partido = _extract_1x2_por_partido(
            state, jornada, temporada,
        )

        if state is not None:
            j = _find_jornada_in_state(state, jornada, temporada)
            if j is not None:
                live = _parse_recaudacion_bote(j)
                partidos = _parse_partidos(
                    j,
                    pleno_local=pleno_local,
                    pleno_visitante=pleno_visitante,
                    probs_por_partido=probs_por_partido,
                )
                shipped = j.get("jornada")
                if shipped and shipped != jornada:
                    warning = (
                        f"URL jornada={jornada} but SSR shipped "
                        f"jornada={shipped}; this page is a live view, "
                        "historical escrutinio lives in "
                        "/quiniela/ayudas/historico-1X2")
            else:
                LOG.warning("no jornada_*_* key matched in state; "
                            "keys=%s", sorted(k for k in state
                                              if k.startswith("jornada_")))

        comentarios = _parse_comentarios(html)
        _merge_comentarios(partidos, comentarios)
        escrutinio = _parse_escrutinio_oficial(html)

        sha = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
        return JornadaLosilla(
            jornada=jornada,
            temporada=temporada,
            url=url,
            checked_at=_utc_now(),
            fetched=True,
            ssr_shipped_jornada=shipped,
            warning=warning,
            recaudacion_bote_live=live,
            escrutinio_oficial=escrutinio,
            partidos=partidos,
            comentarios_presentes=sum(1 for p in partidos if p.comentario),
            sha256=sha,
        )