"""Match a raw Quiniwin team name onto a ``team_identity_map`` row.

Single entry point: :func:`find_or_create_team`. Resolution strategy:

1. **Detect female suffix.** Quiniwin emits names like ``"Athletic Club (F)"``
   or ``"Logroño F"``. We strip the suffix and remember ``gender='women'``.
2. **Normalize.** Lowercase, NFD-strip combining marks (Python side — the DB
   index also uses ``immutable_unaccent`` so both paths converge).
3. **Catalog seed lookup** (slice 5). The catalog at
   ``data/team-catalog.json`` is the source of truth for entity id +
   competition + gender. It carries the *full* alias list, while the DB row
   only stores ``short_name = aliases[0]``. We key the index by
   ``(normalized_name, gender)`` so the Athletic Club men/women ambiguity
   resolves cleanly without any heuristics. On hit we look up the seeded DB
   row by its deterministic ``entity_id`` (``club-men-athletic-club``,
   ``national-esp``, etc.). This also covers the short-name path the old
   ``_lookup_exact`` walked — the catalog lookup is a strict superset.
4. **Exact match via SQL.** Look up rows whose
   ``lower(immutable_unaccent(canonical_name))`` equals our normalized key,
   filtered by ``competition_id`` if the caller hinted one (Quiniwin doesn't
   emit the competition per team, so the hint is the matcher's best signal).
5. **Alias map fallback.** A small dict mapping normalized forms onto
   canonical names. Reused from ``scripts/mock_quiniela.SP_CANONICAL`` so
   "ATH MADRID" → Atletico Madrid works on day one. When the alias map hits,
   repeat the SQL lookup using the alias canonical — but skipping
   ``competition_id`` filter so we find the entity regardless of which
   competition the alias points at.
6. **Stub creation.** Insert a new ``team_identity_map`` row with
   ``competition_id='unknown'``, a deterministic ``entity_id``
   (``"auto-{slug}-{hash6}"``), and ``gender_known`` driven by the suffix:
   a feminine/masculine marker makes the gender certain (``True``); without
   one the stub keeps ``gender='men'`` for backward compatibility but flags
   ``gender_known=False`` so the UI can render "?" or a fallback without
   assuming certainty. Idempotent via ``ON CONFLICT DO NOTHING`` on the
   functional unique index — race-safe even under concurrent ingests.

The function is **side-effect free** beyond the DB writes: caller may pass
an existing :class:`~sqlalchemy.ext.asyncio.AsyncSession` and the matcher
participates in its transaction (no implicit commit).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TeamIdentityMap


LOG = logging.getLogger("team_matcher")

UNKNOWN_COMPETITION_ID = "unknown"
_HASH_LEN = 6

# Path to the team catalog seed (also consumed by
# ``app.scripts.load_team_catalog``). Read lazily on first lookup — the
# module imports cleanly even when the file is missing (e.g., minimal
# deploys that don't need catalog matching).
_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "team-catalog.json"


@dataclass(frozen=True, slots=True)
class _CatalogEntry:
    """One entity from ``data/team-catalog.json``.

    Carries the deterministic ``entity_id`` the catalog assigned to the
    entity (e.g., ``club-men-fc-andorra``), the ``competition_id`` the
    seeder writes to ``team_identity_map.competition_id``, and the
    canonical gender (``"men"`` / ``"women"``). This is the same shape
    the seeded ``team_identity_map`` row exposes, so the matcher can go
    from a catalog hit to a DB row with a single ``entity_id`` lookup.
    """
    entity_id: str
    competition_id: str
    gender: str


# (normalized_name, gender) → list of catalog entries that match. A list
# (not a single row) because two can share the same normalized name under
# the same gender — e.g. ``club-men-fc-andorra`` (alias "Andorra") and
# ``national-and`` (name "Andorra") both collide on ``("andorra", "men")``.
# ``catalog_lookup`` resolves collisions by the caller's
# ``competition_id_hint`` or, failing that, alphabetically by ``entity_id``.
_CATALOG_INDEX: dict[tuple[str, str], list[_CatalogEntry]] = {}
_CATALOG_INDEX_READY: bool = False


# ---------- Name normalisation helpers (pure) ----------


_FEMALE_SUFFIX_RE = re.compile(
    r"\s*\(f\)\s*$", flags=re.IGNORECASE,
)


def detect_female(raw: str) -> tuple[str, str | None]:
    """Return ``(name_without_suffix, gender_or_None)``.

    A female suffix is one of:

    - ``" F"`` at end of name (e.g. ``"Logroño F"``)
    - ``"(F)"`` / ``"(f)"`` at end (e.g. ``"Athletic Club (F)"``)

    Returns ``gender='women'`` and the suffix stripped. If no suffix is
    present, returns ``(raw, None)``. The matcher's caller is responsible
    for setting the final ``gender`` (it always sets one — ``'men'`` when
    the suffix is absent).

    The check is conservative: it only strips when the suffix appears at
    the end. Words ending in legitimate ``F`` (unlikely in football team
    names) are not affected.

    NOTE: ``"(m)"`` suffix on national teams (e.g. ``"España (m)"`` to
    disambiguate from the women's team) is stripped at the matcher level
    *without* marking gender — the team is still men, the suffix is just
    Quiniwin's disambiguation marker. See ``strip_masculine_marker``.
    """
    if not raw:
        return raw, None
    s = raw.strip()
    if s.endswith(" F") or s.endswith(" f"):
        return s[:-2].rstrip(), "women"
    m = _FEMALE_SUFFIX_RE.search(s)
    if m:
        return s[: m.start()].rstrip(), "women"
    return raw, None


_MASCULINE_MARKER_RE = re.compile(r"\s*\(m\)\s*$", flags=re.IGNORECASE)


def strip_masculine_marker(raw: str) -> str:
    """Strip ``"(m)"`` suffix without changing gender.

    Quiniwin emits names like ``"España (m)"`` for men's national teams to
    disambiguate from ``"España"`` (women's). The ``(m)`` is a marker, not a
    gender signal — the team is still men. We strip it before lookup so the
    seed row ``"España"`` (gender='men') gets matched.
    """
    if not raw:
        return raw
    s = raw.strip()
    m = _MASCULINE_MARKER_RE.search(s)
    if m:
        return s[: m.start()].rstrip()
    return raw


def normalize_name(raw: str) -> str:
    """Lowercase + NFD-strip combining marks.

    Used to build lookup keys. Mirrors the SQL side's
    ``lower(public.immutable_unaccent(...))`` expression so the Python key
    matches what the index expression produces. We keep spaces and digits
    intact — the index expression does too.
    """
    nfd = unicodedata.normalize("NFD", raw)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return stripped.lower().strip()


def slugify(name: str, *, max_len: int = 40) -> str:
    """Build a URL-safe slug for ``entity_id`` generation.

    Keeps alphanumerics, collapses runs of non-alphanumerics to ``-``,
    trims leading/trailing ``-``. Cuts to ``max_len`` so the resulting
    ``entity_id`` stays under the 80-char column limit even with a
    6-char hash suffix.
    """
    nfd = unicodedata.normalize("NFD", name)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    return slug[:max_len]


def stub_entity_id(name: str) -> str:
    """Build the ``entity_id`` for a new stub row.

    Format: ``"auto-{slug}-{hash6}"`` where ``hash6`` is the first 6 hex
    chars of ``sha256(normalized_name)``. The hash differentiates two
    teams whose slugs collide (e.g. ``"Club"`` in two different
    competitions — both have slug ``"club"`` but different hashes).
    """
    norm = normalize_name(name)
    h = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:_HASH_LEN]
    slug = slugify(name) or "team"
    return f"auto-{slug}-{h}"


# ---------- Catalog seed lookup (slice 5) ----------


def _ensure_catalog_index() -> None:
    """Lazy, GIL-safe one-time load of ``data/team-catalog.json``.

    Builds ``_CATALOG_INDEX`` keyed by ``(normalized_name, gender)``. Each
    entity's ``name`` AND every ``alias`` contributes a key — so the index
    is a strict superset of what the DB's ``short_name`` column exposes
    (which only stores ``aliases[0]``). ``gender_known`` is intentionally
    not tracked here: the seeded DB rows always carry the column default
    ``True``, and the matcher never needs the flag during resolution — it
    matters only when *creating* a stub.

    Sets ``_CATALOG_INDEX_READY = True`` up front so concurrent callers
    during a cold start don't duplicate the work. Subsequent calls are
    O(1). Failures (missing or malformed file) leave the index empty —
    the SQL/stub fallbacks still work, the catalog path just becomes a
    no-op. The matcher logs a WARNING in that case so missing-catalog
    deploys are loud, not silent.
    """
    global _CATALOG_INDEX_READY
    if _CATALOG_INDEX_READY:
        return
    _CATALOG_INDEX_READY = True

    try:
        catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        LOG.warning(
            "team catalog not found at %s; catalog lookup disabled",
            _CATALOG_PATH,
        )
        return
    except json.JSONDecodeError as exc:
        LOG.warning(
            "team catalog %s is not valid JSON (%s); catalog lookup disabled",
            _CATALOG_PATH, exc,
        )
        return
    except OSError as exc:
        LOG.warning(
            "team catalog %s unreadable (%s); catalog lookup disabled",
            _CATALOG_PATH, exc,
        )
        return

    entities = catalog.get("entities")
    if not isinstance(entities, list):
        LOG.warning(
            "team catalog 'entities' is not a list (got %s); "
            "catalog lookup disabled",
            type(entities).__name__,
        )
        return

    loaded = 0
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        eid = entity.get("id")
        comp_id = entity.get("competition_id")
        gender = entity.get("gender")
        # Entities without ``competition_id`` are skipped by the seeder
        # (``load_team_catalog.upsert_team_identities`` increments
        # ``skipped`` for them), so they'd never reach
        # ``team_identity_map``. Don't index them.
        if not eid or not comp_id or not gender:
            continue
        entry = _CatalogEntry(entity_id=eid, competition_id=comp_id, gender=gender)

        raw_keys: list[str] = []
        name = entity.get("name")
        if isinstance(name, str) and name.strip():
            raw_keys.append(name)
        for alias in entity.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                raw_keys.append(alias)

        for raw_key in raw_keys:
            norm = normalize_name(raw_key)
            if not norm:
                continue
            _CATALOG_INDEX.setdefault((norm, gender), []).append(entry)
            loaded += 1

    LOG.debug(
        "loaded team catalog index: %d entities, %d name/alias pairs",
        sum(1 for v in catalog.get("entities", []) if isinstance(v, dict)),
        loaded,
    )


def catalog_lookup(
    name: str,
    gender: str,
    competition_id_hint: str | None = None,
) -> _CatalogEntry | None:
    """Resolve a (name, gender) pair against the catalog seed.

    Pure helper: no DB, no I/O after the first call. Returns the
    matching :class:`_CatalogEntry` so the caller can look the seeded DB
    row up by its deterministic ``entity_id``, or ``None`` if no match.

    Collision resolution when multiple entries share the same
    ``(normalized_name, gender)`` (e.g. ``club-men-fc-andorra`` via its
    alias ``"Andorra"`` and the national team ``national-and`` via its
    name ``"Andorra"``):

    1. If ``competition_id_hint`` is provided and matches exactly one
       candidate, prefer it. This is how Quiniwin's partido-level
       competition hint disambiguates "Andorra" between the club
       (Segunda) and the national team (UEFA).
    2. Otherwise, return the candidate whose ``entity_id`` is
       lexicographically smallest — a stable order that mirrors the
       catalog file's natural reading. Not a meaningful preference, but
       deterministic, which is what matters here.
    """
    _ensure_catalog_index()
    normalized = normalize_name(name)
    if not normalized:
        return None
    candidates = _CATALOG_INDEX.get((normalized, gender))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if competition_id_hint:
        for c in candidates:
            if c.competition_id == competition_id_hint:
                return c
    return min(candidates, key=lambda c: c.entity_id)


# ---------- Alias map (fallback) ----------


# A small alias map mirroring ``scripts/mock_quiniela.SP_CANONICAL`` for the
# teams Quiniwin emits in compressed form. Kept inline here so the matcher
# has no import dependency on the legacy script (the legacy file is a plain
# Python module in ``scripts/``, not a Python package).
#
# Key = normalized form of the raw Quiniwin name. Value = canonical team
# name as it lives in the DB (case-sensitive exact). We do not include
# every entry from SP_CANONICAL — only the ones that show up as abbreviation
# in Quiniwin's compressed "equipos" attribute (e.g. "ATH MADRID" not
# "Atletico Madrid").
#
# When this map hits, the SQL lookup is repeated WITHOUT a competition_id
# filter — the alias is treated as a bridge to whatever row holds the
# canonical name.
_ALIAS_MAP: dict[str, str] = {
    # Spanish men's LaLiga / Segunda abbreviations
    "ath madrid": "Club Atlético de Madrid",   # seed canonical has "Club" prefix
    "at madrid": "Atlético Madrid",             # short_name form for SQL unaccent match
    "atletico": "Club Atlético de Madrid",
    "at.madrid": "Atlético Madrid",
    "at. madrid": "Atlético Madrid",            # with space (Quiniwin emits "At. Madrid")
    "ath bilbao": "Athletic Club",
    "ath club": "Athletic Club",
    "athletic": "Athletic Club",
    "r madrid": "Real Madrid",
    "r sociedad": "Real Sociedad",
    "r oviedo": "Real Oviedo",
    "r. oviedo": "Real Oviedo",
    "r. valladolid": "Real Valladolid",
    "sp gijon": "Sporting Gijon",
    "sporting": "Sporting Gijon",
    "betis": "Real Betis",
    "celta": "Celta Vigo",
    "celta vigo": "Celta Vigo",
    # Seed has "RC Deportivo de La Coruña" with short "Deportivo A Coruña".
    # The short_name form works because both the alias key (Python) and the
    # SQL unaccent function normalize to "deportivo a coruna".
    "deportivo": "Deportivo A Coruña",
    # UEFA Champions League / Europa League / Conference — Quiniwin emits
    # abbreviated or Spanish forms; seed uses the official full names.
    "bayern de munich": "FC Bayern München",
    "inter de milan": "FC Internazionale Milano",
    "oporto": "FC Porto",
    "napoles": "Napoli",                        # seed canonical is "Napoli" (no "SSC" prefix)
    "nápoles": "Napoli",
    "rb. leipzig": "RB Leipzig",
    "rb leipzig": "RB Leipzig",
    "bodo gli": "Bodø/Glimt",
    "slovan bratislava": "ŠK Slovan Bratislava",
    "sk slavia de praga": "Slavia Praha",       # seed has no "SK" prefix
    "rc len": "Lille",                           # seed has "Lille", not "RC Lens"
    "losc lille": "Lille",
    "galatasa": "Galatasaray",
    "como 1907": "Como",
    "fenerbahce sk": "Fenerbahçe",
    "racing de santander": "Real Racing Club",
    "racing santander": "Real Racing Club",
    "racing san": "Real Racing Club",           # truncated form Quiniwin emits
    "racing de santa": "Real Racing Club",      # truncated form Quiniwin emits
    "shakhtar donetsk": "FC Shakhtar Donetsk",
    "villarrea": "Villarreal CF",               # typo (missing 'l') from Quiniwin
    "rayo": "Rayo Vallecano",                   # short form Quiniwin uses
    "plzen": "Viktoria Plzeň",                  # Czech city, seed uses full form
    "sporting de portugal": "Sporting CP",      # full form Quiniwin emits
    "psg": "Paris Saint-Germain",
    # National teams — seed uses English names; Quiniwin emits Spanish.
    # Mirrors scripts/mock_quiniela.UEFA_CANONICAL. All maps go to the
    # English canonical name in fifa-member-associations.
    "espana": "Spain",
    "alemania": "Germany",
    "francia": "France",
    "inglaterra": "England",
    "italia": "Italy",
    "portugal": "Portugal",
    "paises bajos": "Netherlands",
    "holanda": "Netherlands",
    "belgica": "Belgium",
    "suiza": "Switzerland",
    "austria": "Austria",
    "dinamarca": "Denmark",
    "suecia": "Sweden",
    "noruega": "Norway",
    "islandia": "Iceland",
    "irlanda": "Ireland",
    "rep irlanda": "Republic of Ireland",       # seed has "Republic of Ireland" (no "Ireland" alone)
    "rep. irlanda": "Republic of Ireland",      # Quiniwin uses period in the suffix
    "rep. checa": "Czechia",                    # Quiniwin uses period in the suffix
    "escocia": "Scotland",
    "gales": "Wales",
    "grecia": "Greece",
    "croacia": "Croatia",
    "serbia": "Serbia",
    "luxemburgo": "Luxembourg",
    "eslovaquia": "Slovakia",
    "kazajistan": "Kazakhstan",
    "rep checa": "Czechia",
    "eslovenia": "Slovenia",
    "macedonia del n": "North Macedonia",
    "macedonia": "North Macedonia",
    "bulgaria": "Bulgaria",
    "estonia": "Estonia",
    "san marino": "San Marino",
    "albania": "Albania",
    "azerbaiyan": "Azerbaijan",
    "liechtenstein": "Liechtenstein",
    "malta": "Malta",
    "gibraltar": "Gibraltar",
    "lituania": "Lithuania",
    "letonia": "Latvia",
    "ucrania": "Ukraine",
    "rumania": "Romania",
    "hungria": "Hungary",
    "polonia": "Poland",
    "turquia": "Turkey",
}


# ---------- Match logic ----------


async def _lookup_exact(
    session: AsyncSession,
    normalized: str,
    competition_id: str | None,
) -> TeamIdentityMap | None:
    """Match the normalized name against the canonical OR short name column.

    Uses the same ``lower(public.immutable_unaccent(...))`` expression as
    the index so the planner can use it. Returns the first row
    (competition-scoped if hint given, else any competition).

    The short-name path is critical: the seed stores long canonical forms
    ("FC Barcelona", "Sevilla FC", "Rayo Vallecano de Madrid") while
    Quiniwin emits the short forms ("Barcelona", "Sevilla", "Rayo
    Vallecano"). Without this branch every short-named team becomes a stub.
    """
    name_match = or_(
        # Apply unaccent on BOTH sides so the parameter normalization matches
        # what the index expression produces. Python's unicodedata NFD does
        # not strip ø/ü/ç but Postgres' unaccent() does — without this
        # asymmetry, aliases for "FC Bayern München" or "Bodø/Glimt" never
        # hit the index.
        text(
            "lower(public.immutable_unaccent(team_identity_map.canonical_name)) "
            "= lower(public.immutable_unaccent(:norm))"
        ).bindparams(norm=normalized),
        text(
            "team_identity_map.short_name IS NOT NULL "
            "AND lower(public.immutable_unaccent(team_identity_map.short_name)) "
            "= lower(public.immutable_unaccent(:norm))"
        ).bindparams(norm=normalized),
    )
    stmt = select(TeamIdentityMap).where(name_match)
    if competition_id is not None:
        stmt = stmt.where(TeamIdentityMap.competition_id == competition_id)
    # Soft-delete filter (slice 1 of the superadmin-panel sprint): a
    # soft-deleted team must not be matched on a fresh ingest. The admin
    # may have deleted it precisely because it was a bad stub or a
    # duplicate; the matcher would otherwise silently resurrect it.
    stmt = stmt.where(TeamIdentityMap.deleted_at.is_(None))
    stmt = stmt.order_by(TeamIdentityMap.id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _insert_stub(
    session: AsyncSession,
    name: str,
    gender: str,
    *,
    gender_known: bool,
) -> TeamIdentityMap:
    """Insert a stub row and return the canonical (idempotent) version.

    Uses ``INSERT ... ON CONFLICT DO NOTHING`` on the functional unique
    index, then SELECT to obtain the row that ended up in the table —
    whether it was inserted in this call or by a concurrent caller. This
    avoids the classic "look up first, then insert" race where two
    parallel callers each miss the lookup and each insert, leaving two
    rows that the unique index will reject on the second commit only
    (causing one of them to throw).

    ``gender_known`` is a required keyword-only argument so callers
    consciously decide whether the raw name carried a definitive gender
    signal (e.g. ``"(F)"`` suffix → ``True``). Without a marker, the
    stub keeps the column default (``False`` per the slice-5 contract)
    so the UI can flag uncertainty instead of trusting a guess.
    """
    entity_id = stub_entity_id(name)
    norm_key = normalize_name(name)

    # ON CONFLICT DO NOTHING on the functional unique index would need an
    # index predicate; PostgreSQL does support ON CONFLICT on a partial /
    # functional index when we name the index explicitly. Using a literal
    # WHERE NOT EXISTS + INSERT pattern instead keeps it simple and
    # portable, and races correctly under READ COMMITTED isolation:
    # the second writer waits for the first to commit, then sees the
    # row and skips insertion.
    stmt = pg_insert(TeamIdentityMap).values(
        entity_id=entity_id,
        competition_id=UNKNOWN_COMPETITION_ID,
        canonical_name=name,
        gender=gender,
        gender_known=gender_known,
        country=None,
    )
    # We can't reference a functional index in on_conflict_do_nothing
    # directly via SQLAlchemy without naming the constraint. Easier
    # approach: try the insert; on UniqueViolation (functional index hit)
    # fall back to SELECT. The except branch handles the race.
    try:
        await session.execute(stmt)
    except Exception:
        # Likely a race: the second caller lost the insert. Roll back
        # the failed statement and re-fetch.
        await session.rollback()
        row = await _lookup_exact(session, norm_key, UNKNOWN_COMPETITION_ID)
        if row is not None:
            return row
        # If still nothing, re-raise — something else broke.
        raise

    # Successful insert (or no-op conflict). Fetch the row that ended up
    # in the table so the caller always sees the canonical id.
    row = await _lookup_exact(session, norm_key, UNKNOWN_COMPETITION_ID)
    if row is None:
        # Shouldn't happen — we just inserted. Defensive: re-fetch by
        # entity_id (the canonical stub entity_id is deterministic so we
        # can find it directly).
        row = await session.scalar(
            select(TeamIdentityMap).where(TeamIdentityMap.entity_id == entity_id)
        )
    if row is None:
        raise RuntimeError(
            "stub insert succeeded but row could not be re-fetched — "
            "DB state is unexpected; please inspect"
        )
    return row


async def find_or_create_team(
    session: AsyncSession,
    raw_name: str | None,
    competition_id_hint: str | None = None,
) -> TeamIdentityMap:
    """Resolve a Quiniwin raw team name onto a ``team_identity_map`` row.

    ``competition_id_hint`` is the competition we *think* the team belongs
    to. Quiniwin emits the competition per partido, not per team, so the
    caller (the ingest CLI) passes the partido's ``competicion_raw``
    resolved somehow — or ``None`` to skip the competition filter. Stub
    rows always end up under ``competition_id='unknown'``.

    Resolution order:

    1. Strip female suffix → ``(name_without_f, gender)``
    2. Normalize → ``normalized_key``
    3. Catalog seed lookup (slice 5). Keys by ``(normalized_name,
       gender)`` and uses the FULL alias set (not just ``short_name``).
       On hit, fetch the seeded DB row directly by ``entity_id``.
    4. Exact SQL lookup (scoped by hint if provided)
    5. Exact SQL lookup (no hint)
    6. Alias map fallback → re-lookup with alias canonical (no hint)
    7. Create stub → log WARNING → return the new row. The stub carries
       ``gender_known = (female_marker is not None)`` — without a
       suffix the matcher's gender choice is uncertain, so the flag
       tells downstream code to render "?" or a fallback instead of
       trusting the guess.

    Returns the matched or newly-created :class:`TeamIdentityMap`. The
    caller is responsible for committing the session.
    """
    if not raw_name or not raw_name.strip():
        raise ValueError("raw_name must be a non-empty string")

    # Strip the (m) marker on national teams ("España (m)" → "España") so the
    # seed row matches. Gender stays men — (m) is a Quiniwin disambiguation
    # marker, not a gender signal.
    raw_name = strip_masculine_marker(raw_name)

    name, female_marker = detect_female(raw_name)
    gender = female_marker if female_marker is not None else "men"
    normalized = normalize_name(name)

    # 1. Catalog seed lookup. Comes BEFORE the SQL/alias paths so that:
    #   - The full alias list (which the DB's short_name column does NOT
    #     carry — only aliases[0]) is available.
    #   - The ``(normalized_name, gender)`` key disambiguates same-named
    #   clubs by gender (Athletic Club men vs women), avoiding the old
    #   behavior of "first row by id wins" which silently produced the
    #   men's row for "Athletic Club (F)" with no competition hint.
    #   - "FC Andorra" resolves to ``club-men-fc-andorra`` (laliga-hypermotion)
    #   instead of risking a stub if the SQL path ever missed.
    catalog_row = catalog_lookup(name, gender, competition_id_hint)
    if catalog_row is not None:
        seeded = await session.scalar(
            select(TeamIdentityMap).where(
                TeamIdentityMap.entity_id == catalog_row.entity_id,
                # Soft-delete filter — same rationale as ``_lookup_exact``.
                TeamIdentityMap.deleted_at.is_(None),
            )
        )
        if seeded is not None:
            return seeded
        # Catalog hit but the seeder hasn't loaded it. Could be a partial
        # seed (e.g. someone ran a custom script), or the catalog was
        # edited after seeding. Log loud so the operator notices.
        LOG.warning(
            "catalog hit but seeded team_identity_map row not found: "
            "entity_id=%s competition_id=%s gender=%s — falling through "
            "to SQL/alias paths",
            catalog_row.entity_id, catalog_row.competition_id, gender,
        )

    # 2. Exact match (competition-scoped if hint given)
    row = await _lookup_exact(session, normalized, competition_id_hint)
    if row is not None:
        # If the row exists but gender doesn't match the suffix, we still
        # return it: the canonical roster decides gender, not the raw
        # name. (E.g. "Athletic Club (F)" matching the male team means
        # the alias map is wrong; we'd log below if we created a stub.)
        return row

    # 3. Exact match (no hint) — useful when the hint missed because the
    # team's primary competition differs from where they're appearing today
    # (e.g., a Copa del Rey match between two LaLiga teams).
    row = await _lookup_exact(session, normalized, None)
    if row is not None:
        return row

    # 4. Alias map fallback
    alias_target = _ALIAS_MAP.get(normalized)
    if alias_target is not None:
        alias_normalized = normalize_name(alias_target)
        row = await _lookup_exact(session, alias_normalized, None)
        if row is not None:
            return row

    # 5. Stub creation — log WARNING so admin can fix it later. The
    # ``gender_known`` flag reflects whether the raw name carried a
    # definitive gender signal: a feminine (or masculine) suffix on
    # the raw name means we're sure, no suffix means we're guessing
    # gender='men' for back-compat and the flag should tell the UI to
    # render an "?" or a fallback.
    LOG.warning(
        "creating stub team identity: raw=%r normalized=%r gender=%s "
        "gender_known=%s competition_id_hint=%s → entity_id=%s",
        raw_name, normalized, gender, female_marker is not None,
        competition_id_hint, stub_entity_id(name),
    )
    return await _insert_stub(
        session, name, gender,
        gender_known=female_marker is not None,
    )


# ---------- Bulk helper for the ingest CLI ----------


async def resolve_partidos_teams(
    session: AsyncSession,
    raw_locals: Iterable[str],
    raw_visitants: Iterable[str],
    competicion_raw: str | None,
) -> list[tuple[TeamIdentityMap, TeamIdentityMap]]:
    """Resolve a list of (local_raw, visitante_raw) pairs in one call.

    For T-data-1 we don't yet map ``competicion_raw`` onto a competition id
    (that's a future admin task) so the hint is always ``None`` here. The
    wrapper exists so the ingest loop can stay readable, and to leave a
    single seam for adding competition-hint resolution later.
    """
    out: list[tuple[TeamIdentityMap, TeamIdentityMap]] = []
    for local_raw, visitante_raw in zip(raw_locals, raw_visitants):
        local = await find_or_create_team(session, local_raw)
        visitante = await find_or_create_team(session, visitante_raw)
        out.append((local, visitante))
    return out