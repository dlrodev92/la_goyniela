"""One-shot loader: populate the catalog tables from data/team-catalog.json
(and the legacy scripts/team_ids_api_football.py for API-Football IDs).

Idempotent. Safe to re-run. Prints a per-table summary.

Usage:
    .venv/bin/python app/scripts/load_team_catalog.py [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import unicodedata
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Competition, Source, TeamIdApiFootball, TeamIdentityMap
from app.db.session import AsyncSessionLocal


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "data" / "team-catalog.json"
LEGACY_TEAM_IDS_PATH = REPO_ROOT / "scripts" / "team_ids_api_football.py"

SOURCE_SEED: list[tuple[str, str, str, str]] = [
    # (code, name, kind, base_url)
    ("quiniela",       "Quiniwin",                    "xml",  "https://www.quiniwin.com"),
    ("losilla",        "Eduardo Losilla",             "xml",  "https://api.eduardolosilla.es"),
    ("api_football",   "API-Football v3",             "api",  "https://v3.football.api-sports.io"),
    ("the_odds_api",   "The Odds API",                "api",  "https://api.the-odds-api.com"),
    ("sofascore",      "Sofascore (read-only embed)", "embed", "https://www.sofascore.com"),
]

# Map the legacy file's `league` strings onto our campaign's `competition_id`
# values. Used to disambiguate teams that share a name across genders/tiers
# (e.g., Athletic Club men vs women).
LEAGUE_TO_COMPETITION_ID: dict[str, str] = {
    "LaLiga":                    "laliga-ea-sports",
    "Segunda Division":          "laliga-hypermotion",
    "Primera Federacion":        "primera-federacion",
    "Primera Division Femenina": "liga-f",
}


def _strip_accents(s: str) -> str:
    """Accent-insensitive lowercase for fuzzy name matching."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


# ---------------------------------------------------------------------------
# Legacy file loader
# ---------------------------------------------------------------------------


def load_legacy_team_ids() -> dict[str, dict]:
    """Import the legacy ``TEAM_IDS_API_FOOTBALL`` dict from the legacy file.

    We use ``importlib`` because the legacy file is plain Python (not JSON)
    and we want to honour any future edits without re-parsing by hand.
    """
    spec = importlib.util.spec_from_file_location(
        "legacy_team_ids", LEGACY_TEAM_IDS_PATH
    )
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Some legacy entries use duplicated keys (e.g. "granada" appears twice —
    # last write wins in Python literals). ``dict()`` reproduces that.
    return dict(getattr(module, "TEAM_IDS_API_FOOTBALL", {}))


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


async def upsert_competitions(
    session, catalog: dict, verbose: bool
) -> int:
    """Upsert the 9 competitions from ``catalog["competitions"]``."""
    rows: list[dict] = []
    for comp_id, info in catalog["competitions"].items():
        rows.append(
            {
                "id": comp_id,
                "name": info.get("name", comp_id),
                "country": info.get("country"),
                "gender": info.get("gender"),
                "tier": info.get("tier"),
                "season": info.get("season"),
                "official_source_url": info.get("official_source_url"),
            }
        )

    if not rows:
        return 0

    # Always include the "unknown" stub competition so ``team_identity_map``
    # rows created with ``competition_id='unknown'`` (auto-stubs for teams
    # the matcher can't resolve) have a valid FK target. The seeder
    # originally relied on this row being inserted manually; make it
    # deterministic so the test DB has it too.
    rows.append(
        {
            "id": "unknown",
            "name": "Competición desconocida (auto-stub)",
            "country": None,
            "gender": None,
            "tier": None,
            "season": None,
            "official_source_url": None,
        }
    )

    stmt = pg_insert(Competition).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Competition.id],
        set_={
            "name": stmt.excluded.name,
            "country": stmt.excluded.country,
            "gender": stmt.excluded.gender,
            "tier": stmt.excluded.tier,
            "season": stmt.excluded.season,
            "official_source_url": stmt.excluded.official_source_url,
        },
    )
    await session.execute(stmt)
    await session.commit()

    if verbose:
        for r in rows:
            print(f"  upserted competition: {r['id']} ({r['name']})")
    return len(rows)


async def upsert_sources(session, verbose: bool) -> int:
    """Upsert the seed list of external data sources."""
    rows = [
        {"code": code, "name": name, "kind": kind, "base_url": base_url}
        for (code, name, kind, base_url) in SOURCE_SEED
    ]

    stmt = pg_insert(Source).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Source.code],
        set_={
            "name": stmt.excluded.name,
            "kind": stmt.excluded.kind,
            "base_url": stmt.excluded.base_url,
        },
    )
    await session.execute(stmt)
    await session.commit()

    if verbose:
        for r in rows:
            print(f"  upserted source: {r['code']} ({r['name']})")
    return len(rows)


async def upsert_team_identities(
    session, catalog: dict, verbose: bool
) -> tuple[int, int]:
    """Upsert the entities (clubs + national teams) into team_identity_map.

    Returns ``(inserted_count, skipped_count)``.

    Field mapping notes
    -------------------
    The JSON's ``entities`` block is an *array* (not a dict). Each element
    carries the actual canonical name under ``name``; the spec's
    ``canonical_name``/``display_name`` fallback chain does not apply here.

    ``country`` is stored as a 3-letter ISO code (``association_code`` for
    clubs, ``fifa_code`` for national teams) because the column is
    VARCHAR(8) and full country names like ``"Turks and Caicos Islands"``
    don't fit. Downstream (T7+) is expected to normalise for display.

    ``logo_url`` stores the JSON's ``image_path`` as-is (relative path
    under ``data/team-assets/``). The serving layer decides whether to
    prefix an R2/CDN domain.
    """
    entities = catalog["entities"]
    rows: list[dict] = []
    skipped = 0
    for entity in entities:
        comp_id = entity.get("competition_id")
        if not comp_id:
            skipped += 1
            continue

        # Prefer association_code (clubs); fall back to fifa_code (national
        # teams where association_code is null). Both are 3-letter ISO.
        country = entity.get("association_code") or entity.get("fifa_code")

        aliases = entity.get("aliases") or []
        short_name = aliases[0] if aliases else None

        rows.append(
            {
                "entity_id": entity["id"],
                "competition_id": comp_id,
                "canonical_name": entity.get("name") or entity["id"],
                "short_name": short_name if short_name and len(short_name) <= 40 else None,
                "gender": entity.get("gender"),
                "country": country,
                # NOTE: do NOT seed ``logo_url`` here. The local ``image_path``
                # is not a public URL — it's a relative filesystem path that
                # the seeder would overwrite on every run, clobbering any
                # public URL set by the bootstrap pipeline (R2 upload).
                # Logos are populated exclusively by the bootstrap script
                # (``python -m app.scripts.bootstrap_team_logos``).
            }
        )

    if not rows:
        return 0, skipped

    stmt = pg_insert(TeamIdentityMap).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[TeamIdentityMap.entity_id],
        set_={
            "competition_id": stmt.excluded.competition_id,
            "canonical_name": stmt.excluded.canonical_name,
            "short_name": stmt.excluded.short_name,
            "gender": stmt.excluded.gender,
            "country": stmt.excluded.country,
        },
    )
    await session.execute(stmt)
    await session.commit()

    if verbose:
        for r in rows[:5]:
            print(
                f"  upserted team: {r['entity_id']} "
                f"({r['canonical_name']}) → {r['competition_id']}"
            )
        if len(rows) > 5:
            print(f"  ... and {len(rows) - 5} more")
    return len(rows), skipped


async def upsert_team_ids_api_football(
    session,
    legacy_map: dict[str, dict],
    match_index: "LegacyMatchIndex",
    verbose: bool,
) -> tuple[int, int]:
    """Upsert the API-Football numeric IDs from the legacy map.

    Matching strategy (in priority order)
    -------------------------------------
    1. Exact (accent-stripped, lowercased) match on name OR alias, scoped to
       the competition_id implied by the legacy ``league`` field.
    2. For legacy keys that look like a women's team (contain "w",
       "women", "femenino", "femenina") — match against any women team.
    3. Last-resort: any name/alias match ignoring competition/gender.

    Entries that don't match any entity (e.g., a team that was relegated
    out of the 2026/27 Liga F roster) are logged and skipped.

    Returns ``(inserted_count, skipped_count)``.
    """
    src_row = await session.execute(
        select(Source).where(Source.code == "api_football")
    )
    src = src_row.scalar_one_or_none()
    if src is None:
        if verbose:
            print("  source api_football not yet seeded; skipping")
        return 0, len(legacy_map)

    inserts: list[dict] = []
    # Dedupe by (team_identity_id, season_seen) preserving last-write-wins.
    # The legacy file has aliases (e.g. "madrid cff" and "madrid cff w" both
    # pointing to the same Madrid CFF women's team) — last write wins to
    # mirror Python dict-literal semantics. ON CONFLICT DO UPDATE in one
    # statement can't handle two rows hitting the same constraint key.
    by_team_season: dict[tuple[int, str], dict] = {}
    skipped = 0
    for legacy_key, info in legacy_map.items():
        target_comp = LEAGUE_TO_COMPETITION_ID.get(info["league"])
        is_women_hint = (
            info["league"] == "Primera Division Femenina"
            or any(t in legacy_key for t in (" w", "women", "femenino", "femenina"))
        )

        match = match_index.resolve(legacy_key, target_comp, is_women_hint)
        if match is None:
            skipped += 1
            if verbose:
                print(
                    f"  skipped api_football_id (no entity match): "
                    f"{legacy_key!r} → {info.get('name')!r} ({info['league']})"
                )
            continue

        by_team_season[(match.id, "2024/25")] = {
            "source_id": src.id,
            "team_identity_id": match.id,
            "api_football_id": int(info["id"]),
            "season_seen": "2024/25",
        }
    inserts = list(by_team_season.values())

    if not inserts:
        return 0, skipped

    stmt = pg_insert(TeamIdApiFootball).values(inserts)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            TeamIdApiFootball.team_identity_id,
            TeamIdApiFootball.season_seen,
        ],
        set_={
            "api_football_id": stmt.excluded.api_football_id,
        },
    )
    await session.execute(stmt)
    await session.commit()

    if verbose:
        for r in inserts[:5]:
            print(
                f"  upserted api_football_id: team_id={r['team_identity_id']} "
                f"af_id={r['api_football_id']}"
            )
        if len(inserts) > 5:
            print(f"  ... and {len(inserts) - 5} more")
    return len(inserts), skipped


class LegacyMatchIndex:
    """Accent-insensitive lookup of catalog entities → DB rows.

    Built once from the catalog JSON plus the inserted TeamIdentityMap rows.
    Used to resolve legacy ``TEAM_IDS_API_FOOTBALL`` keys (lowercased, ASCII,
    no diacritics) onto the canonical team rows.
    """

    def __init__(
        self,
        entities: list[dict],
        rows_by_entity_id: dict[str, "TeamIdentityMap"],
    ) -> None:
        # (stripped_name_or_alias, competition_id) → row
        self._by_comp_key: dict[tuple[str, str], "TeamIdentityMap"] = {}
        # stripped_name_or_alias → list[(row, competition_id, gender)]
        self._by_name: dict[str, list[tuple["TeamIdentityMap", str, str]]] = {}
        for entity in entities:
            row = rows_by_entity_id.get(entity["id"])
            if row is None:
                continue
            comp_id = entity["competition_id"]
            gender = entity.get("gender")
            keys = [_strip_accents(entity.get("name") or entity["id"])] + [
                _strip_accents(a) for a in entity.get("aliases", [])
            ]
            for k in keys:
                self._by_comp_key.setdefault((k, comp_id), row)
                self._by_name.setdefault(k, []).append((row, comp_id, gender))

    def resolve(
        self,
        legacy_key: str,
        target_competition_id: str | None,
        is_women_hint: bool,
    ) -> "TeamIdentityMap | None":
        k = _strip_accents(legacy_key)

        # Tier 1: scoped to legacy league → competition_id
        if target_competition_id is not None:
            match = self._by_comp_key.get((k, target_competition_id))
            if match is not None:
                return match

        # Tier 2: women-hint override — match any women team
        if is_women_hint:
            for row, _comp, gender in self._by_name.get(k, []):
                if gender == "women":
                    return row
            # Strip common suffixes (" w", " women", " femenino", ...) and
            # try the stripped form, otherwise fall back to substring search
            # across women teams (catches cases like "logroño" → "Logroño
            # United" where no suffix is present).
            k2 = k
            for suffix in (
                " w", " women", "women", " femenino", "femenino",
                " femenina", "femenina", " (women)",
            ):
                if k2.endswith(suffix):
                    k2 = k2[: -len(suffix)].strip()
                    break
            for row, _comp, gender in self._by_name.get(k2, []):
                if gender == "women":
                    return row
            # Last-chance substring among women teams
            for stripped_name, candidates in self._by_name.items():
                for row, _comp, gender in candidates:
                    if gender == "women" and (
                        stripped_name == k2 or k2 in stripped_name
                    ):
                        return row

        # Tier 3: any name/alias match ignoring competition/gender
        candidates = self._by_name.get(k)
        if candidates:
            return candidates[0][0]
        return None


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def count_rows(session) -> dict[str, int]:
    """Count rows per table using COUNT(*) — no full materialisation."""
    out: dict[str, int] = {}
    for model, name in [
        (Competition, "competition"),
        (Source, "source"),
        (TeamIdentityMap, "team_identity_map"),
        (TeamIdApiFootball, "team_id_api_football"),
    ]:
        out[name] = await session.scalar(
            select(func.count()).select_from(model)
        ) or 0
    return out


async def _build_match_index(session) -> "LegacyMatchIndex":
    """Build a LegacyMatchIndex from the catalog and the inserted DB rows.

    We re-read the catalog because the JSON carries the alias list, while
    ``team_identity_map`` only stores ``canonical_name`` + ``short_name``.
    For 423 rows this is fine; a future task may denormalise aliases onto
    the row.
    """
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entities = catalog["entities"]

    rows = (await session.execute(select(TeamIdentityMap))).scalars().all()
    by_entity_id = {row.entity_id: row for row in rows}

    return LegacyMatchIndex(entities, by_entity_id)


async def main(dry_run: bool = False, verbose: bool = False) -> int:
    if not CATALOG_PATH.exists():
        print(f"ERROR: catalog not found at {CATALOG_PATH}", file=sys.stderr)
        return 1

    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    legacy_map = load_legacy_team_ids()

    n_comps_in_catalog = len(catalog.get("competitions", {}))
    n_entities_in_catalog = len(catalog.get("entities", []))

    if verbose:
        print(
            f"Catalog: schema_version={catalog.get('schema_version')} "
            f"snapshot={catalog.get('snapshot_date')}"
        )
        print(f"Competitions in catalog: {n_comps_in_catalog}")
        print(f"Entities in catalog:      {n_entities_in_catalog}")
        print(f"Legacy team_ids entries:  {len(legacy_map)}")
        print()

    if dry_run:
        print("[DRY-RUN] Would upsert:")
        print(f"  {n_comps_in_catalog} competitions")
        print(f"  {len(SOURCE_SEED)} sources (hardcoded seed)")
        print(f"  {n_entities_in_catalog} team identities")
        print(
            f"  up to {len(legacy_map)} API-Football IDs "
            "(some legacy teams are no longer in the 2026/27 roster)"
        )
        return 0

    async with AsyncSessionLocal() as session:
        n_comp = await upsert_competitions(session, catalog, verbose)
        n_src = await upsert_sources(session, verbose)
        n_team, n_team_skipped = await upsert_team_identities(
            session, catalog, verbose
        )

        # Re-load the team index now that the rows exist
        match_index = await _build_match_index(session)
        n_af, n_af_skipped = await upsert_team_ids_api_football(
            session, legacy_map, match_index, verbose
        )

        counts = await count_rows(session)

    print()
    print("=== Final row counts ===")
    for name, n in counts.items():
        print(f"  {name:<22} {n}")
    print()
    print("=== This run ===")
    print(f"  competitions upserted:     {n_comp}")
    print(f"  sources upserted:          {n_src}")
    print(f"  team identities upserted:  {n_team} "
          f"(skipped: {n_team_skipped})")
    print(f"  api-football IDs upserted: {n_af} "
          f"(skipped: {n_af_skipped} — legacy teams no longer in roster)")

    return 0


def cli() -> None:
    p = argparse.ArgumentParser(description="Load team catalog into the DB.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan, change nothing",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Per-row logging",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(dry_run=args.dry_run, verbose=args.verbose)))


if __name__ == "__main__":
    cli()