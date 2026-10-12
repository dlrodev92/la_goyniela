"""Async client for the Quiniwin ``.xml`` jornada feed.

The Quiniwin website exposes a per-jornada XML at::

    https://www.quiniwin.com/xml/{temporada_inicio}-{temporada_fin}_j{NN}.xml

Where ``temporada_inicio`` and ``temporada_fin`` follow the European two-year
form, e.g. temporada 2026 → ``2026-2027``. The real XML schema (verified
against the live J8 sample on 2026-10-01) is::

    <XML>
      <QuinielaBASE>
        <DatosGeneracionFicheroXML fecha="D/M/Y" hora="H:M:S"/>
        <NumeroJornada valor="N"/>
        <Temporada valor="Y/Y"/>
        <Partidos>
          <partido numero="1" pronostico="1" equipos="Local-Visitante"/>
          ...
          <partido numero="15" pronostico="..." equipos="..." pronosticoPLENOGOLES="0-0" .../>
        </Partidos>
        <ColumnaGanadora valor="..." valorPLENOGOLES="..."/>
      </QuinielaBASE>
    </XML>

Notes:
- ``<partido>`` carries ``numero``, ``pronostico`` and ``equipos`` only —
  there is no per-partido ``competicion`` or ``fecha`` attribute. We persist
  ``competicion_raw=None`` and ``fecha_raw=None`` for every row.
- ``pronostico="1"`` everywhere is the template default; the doc
  ``docs/research/02-fuentes.md`` warns not to treat ``pronostico`` or
  ``ColumnaGanadora`` as official results.

The client mirrors :mod:`app.clients.eduardolosilla`'s pattern: async httpx,
in-memory cache keyed by ``(jornada, temporada)``, host throttle. The parser
itself is pure (no I/O) and exposed as ``parse_quiniwin_xml`` so unit tests
can run without HTTP.

Resolving ``competicion`` (per jornada, not per partido) to ``competition.id``
is left to a future admin task; stub teams always point to
``competition_id='unknown'``.

No new dependencies beyond httpx (already in pyproject).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import httpx


LOG = logging.getLogger("quiniwin")

BASE = "https://www.quiniwin.com"
USER_AGENT = "LaGoyniela/0.1 (read-only client; contact via repo)"
DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 2_000_000  # ~2 MB ceiling — the J8 XML is well under 100 KB
MIN_THROTTLE_S = 0.4  # courtesy between requests to the same host


class QuiniwinParseError(ValueError):
    """Raised when the Quiniwin XML cannot be parsed into ``PartidoRaw`` rows.

    Distinct from httpx transport / status errors so the ingest CLI can decide
    whether to skip + continue (parse error) vs. abort (network error).
    """


@dataclass(frozen=True)
class PartidoRaw:
    """One ``<partido>`` from a Quiniwin XML payload, before team matching.

    ``*_raw`` strings are kept verbatim — we do not canonicalise here. Team
    identity resolution happens in `app.db.team_matcher`.
    """
    posicion: int
    local_raw: str
    visitante_raw: str
    competicion_raw: str | None
    fecha_raw: str | None


# ---------- Pure parsing (no I/O) ----------


def _strip_accents(s: str) -> str:
    """NFD + drop combining marks. Used by slug generation only."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def build_url(temporada: int, jornada: int) -> str:
    """Build the Quiniwin XML URL for one jornada.

    Per ``docs/research/02-fuentes.md`` the URL form is::

        https://www.quiniwin.com/xml/{a}-{b}_j{NN}.xml

    Where ``a`` is the temporada start year and ``b`` is the end year. For
    temporada=2026 the file lives at ``2026-2027_j08.xml`` — confirmed live in
    the research doc (the doc captures ``j08.xml`` as the example).
    """
    if temporada < 1900 or temporada > 2200:
        raise ValueError(f"temporada out of plausible range: {temporada!r}")
    if not (1 <= jornada <= 99):
        raise ValueError(
            f"jornada must be 1..99 to fit the j(NN).xml pattern: {jornada!r}"
        )
    return f"{BASE}/xml/{temporada}-{temporada + 1}_j{jornada:02d}.xml"


def build_temporada_str(temporada: int) -> str:
    """Human-readable season string the DB stores on ``jornada.temporada_str``.

    Example: temporada=2026 → ``"2026/2027"``. Matches the existing
    ``test_jornadas.py`` fixture convention.
    """
    return f"{temporada}/{temporada + 1}"


def _split_equipos(equipos: str, num: int) -> tuple[str, str]:
    """Split ``"Local-Visitante"`` into (local, visitante).

    The first ``-`` is the separator; remaining ``-`` characters belong to
    the team name (e.g. ``"Real Madrid-Cadiz"`` or ``"Stade Rennais-Lens"``).
    Raises ``QuiniwinParseError`` if the input is empty or malformed.
    """
    if not equipos:
        raise QuiniwinParseError(f"partido num={num}: empty equipos attribute")
    if "-" not in equipos:
        raise QuiniwinParseError(
            f"partido num={num}: equipos={equipos!r} missing '-' separator"
        )
    local, visitante = equipos.split("-", 1)
    local = local.strip()
    visitante = visitante.strip()
    if not local or not visitante:
        raise QuiniwinParseError(
            f"partido num={num}: empty side after split equipos={equipos!r}"
        )
    return local, visitante


def parse_quiniwin_xml(xml_bytes: bytes) -> list[PartidoRaw]:
    """Parse the Quiniwin XML payload into 15 ``PartidoRaw`` rows.

    Pure function — no I/O. Accepts ``bytes`` so the caller can decide
    whether to cache the raw bytes (we don't, by design — see notes in the
    EduardoLosilla client for why disk caching is omitted here).

    Raises ``QuiniwinParseError`` for malformed XML, missing root, wrong
    partido count, or duplicate position numbers.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise QuiniwinParseError(f"XML parse failed: {exc}") from exc

    if root.tag != "XML":
        raise QuiniwinParseError(
            f"unexpected root element <{root.tag}>; expected <XML>"
        )

    base = root.find("./QuinielaBASE")
    if base is None:
        raise QuiniwinParseError("<XML> without <QuinielaBASE> child")

    partidos_el = base.findall("./Partidos/partido")
    if not partidos_el:
        raise QuiniwinParseError("no <partido> children under <Partidos>")

    out: list[PartidoRaw] = []
    seen_positions: set[int] = set()
    for p in partidos_el:
        num_raw = p.get("numero")
        if num_raw is None:
            raise QuiniwinParseError("<partido> without 'numero' attribute")
        try:
            num = int(num_raw)
        except ValueError as exc:
            raise QuiniwinParseError(
                f"<partido> numero={num_raw!r} is not an integer"
            ) from exc
        if num in seen_positions:
            raise QuiniwinParseError(
                f"duplicate <partido> numero={num} in same XML"
            )
        seen_positions.add(num)

        equipos = p.get("equipos") or ""
        local, visitante = _split_equipos(equipos, num)

        # Real Quiniwin schema has no per-partido competicion or fecha —
        # those attributes only exist at the <QuinielaBASE> level
        # (<NumeroJornada>, <Temporada>, <DatosGeneracionFicheroXML>).
        # We persist None so the audit trail makes the absence explicit.
        out.append(PartidoRaw(
            posicion=num,
            local_raw=local,
            visitante_raw=visitante,
            competicion_raw=None,
            fecha_raw=None,
        ))

    # Sort by posicion defensively — Quiniwin is always 1..15 in order but we
    # don't want to bet the DB order on it.
    out.sort(key=lambda pr: pr.posicion)
    return out


# ---------- HTTP client with in-memory cache + throttle ----------


class QuiniwinClient:
    """Async client for the per-jornada Quiniwin XML feed.

    Holds an ``httpx.AsyncClient`` shared across calls, an in-memory cache
    keyed by ``(jornada, temporada)``, and a host-wide throttle of at most
    one request every ``MIN_THROTTLE_S`` seconds. Same shape as
    :class:`app.clients.eduardolosilla.EduardoLosillaClient` — copied here
    rather than mixed into a common base to keep the clients standalone.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        cache_ttl_s: float = 3600.0,
        cache_dir: Path | None = None,
    ) -> None:
        if client is None:
            client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        self._client = client
        self._owns_client = True  # we always replace None with a fresh one
        self._ttl = cache_ttl_s
        self._cache_dir = cache_dir
        self._mem: dict[tuple[int, int], tuple[float, list[PartidoRaw]]] = {}
        self._last_request_at = 0.0

    async def __aenter__(self) -> "QuiniwinClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the owned httpx client. Safe to call multiple times."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- cache helpers ---

    def _cache_get(self, jornada: int, temporada: int) -> list[PartidoRaw] | None:
        entry = self._mem.get((jornada, temporada))
        if entry is None:
            return None
        stored_at, partidos = entry
        if (time.monotonic() - stored_at) < self._ttl:
            return partidos
        self._mem.pop((jornada, temporada), None)
        return None

    def _cache_put(self, jornada: int, temporada: int,
                   partidos: list[PartidoRaw]) -> None:
        self._mem[(jornada, temporada)] = (time.monotonic(), partidos)

    # --- throttle ---

    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = MIN_THROTTLE_S - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    # --- public ---

    async def get_jornada(self, jornada: int, temporada: int = 2027,
                          *, force_fresh: bool = False) -> list[PartidoRaw]:
        """Return the 15 ``PartidoRaw`` rows for one jornada.

        ``force_fresh=True`` bypasses the in-memory cache.
        """
        if not force_fresh:
            cached = self._cache_get(jornada, temporada)
            if cached is not None:
                return cached

        url = build_url(temporada, jornada)
        xml_bytes = await self._fetch_bytes(url)
        partidos = parse_quiniwin_xml(xml_bytes)
        self._cache_put(jornada, temporada, partidos)
        return partidos

    async def _fetch_bytes(self, url: str) -> bytes:
        assert self._client is not None
        await self._throttle()
        LOG.debug("GET %s", url)
        resp = await self._client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise ValueError(f"Response exceeds {MAX_BYTES} bytes from {url}")
        return resp.content


def sha256_hex(data: bytes) -> str:
    """Public helper for callers who want to fingerprint payloads (e.g. tests).

    Exposed at module level so callers do not need to import ``hashlib`` and
    to keep one canonical place for the digest format.
    """
    return hashlib.sha256(data).hexdigest()


# Convenience for callers that already have an XML string (e.g. fixtures).
def parse_quiniwin_xml_str(xml: str) -> list[PartidoRaw]:
    """Parse an XML string (UTF-8). Thin wrapper over :func:`parse_quiniwin_xml`."""
    return parse_quiniwin_xml(xml.encode("utf-8"))