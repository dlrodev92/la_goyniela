"""Bootstrap team logos to the configured storage backend.

One-shot CLI: reads ``data/team-assets.manifest.json``, uploads each PNG to
R2 (or local-disk in dev/tests), and writes the public URL to
``team_identity_map.logo_url`` in the DB.

Idempotent: teams that already have a ``logo_url`` are skipped unless
``--force`` is passed. Use ``--dry-run`` to preview without touching the DB.

Usage:
    python -m app.scripts.bootstrap_team_logos --dry-run
    python -m app.scripts.bootstrap_team_logos --limit 10
    python -m app.scripts.bootstrap_team_logos --team-id club-men-fc-barcelona
    python -m app.scripts.bootstrap_team_logos --force
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TeamIdentityMap
from app.db.session import AsyncSessionLocal
from app.storage import get_storage


LOG = logging.getLogger("bootstrap")

# Project-relative path. Adjust if the project layout changes.
MANIFEST_PATH = Path(__file__).resolve().parents[2] / "data" / "team-assets.manifest.json"

# Storage key prefix for the team's URL space — matches the one used by the
# /admin/teams/{id}/logo endpoint so an admin re-upload and this bootstrap
# produce the same key.
_TEAMS_KEY_PREFIX = "teams"

# Column width for the per-team status line so URLs line up in the console.
_ENTITY_ID_WIDTH = 40


@dataclass
class BootstrapSummary:
    """Per-run counters — printed at the end of the CLI."""

    ok: int = 0
    skipped: int = 0
    dry: int = 0
    missing_file: int = 0
    missing_team: int = 0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            "=== bootstrap_team_logos summary ===",
            f"  uploaded:        {self.ok}",
            f"  skipped (have):  {self.skipped}",
            f"  dry-run:         {self.dry}",
            f"  file missing:    {self.missing_file}",
            f"  team not in DB:  {self.missing_team}",
            f"  errors:          {len(self.errors)}",
        ]
        if self.errors:
            lines.append("  failed entity_ids:")
            for eid in self.errors:
                lines.append(f"    - {eid}")
        return "\n".join(lines)


def _content_type_for(path: Path) -> str:
    """Infer the MIME type from a file's extension. PNG-by-default since the
    script's input is the team-assets manifest (all entries are PNG).
    """
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".svg":
        return "image/svg+xml"
    return "image/png"


def _load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Read and minimally validate the manifest. Raises on missing /
    malformed JSON; the caller decides whether to fail fast.
    """
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(entries := raw.get("entries"), list):
        raise ValueError(
            f"manifest {manifest_path}: expected top-level 'entries' list"
        )
    return entries


async def _process_one(
    *,
    session: AsyncSession,
    storage,
    entry: dict[str, Any],
    manifest_dir: Path,
    dry_run: bool,
    force: bool,
    summary: BootstrapSummary,
) -> None:
    """Handle a single manifest entry. Never raises — logs and continues."""
    entity_id: str = entry["entity_id"]
    rel_file: str = entry["file"]

    # Resolve the file path relative to the manifest's directory (= project
    # root for ``data/team-assets.manifest.json``). Don't try to be clever
    # about absolute paths — the manifest is the source of truth.
    file_path = (manifest_dir / rel_file).resolve()
    if not file_path.is_file():
        print(
            f"WARN  {entity_id:<{_ENTITY_ID_WIDTH}} "
            f"file missing: {rel_file}"
        )
        summary.missing_file += 1
        return

    # Look up the team. Case-sensitive exact match on entity_id — that is the
    # catalog's stable slug.
    team = await session.scalar(
        select(TeamIdentityMap).where(TeamIdentityMap.entity_id == entity_id)
    )
    if team is None:
        print(
            f"WARN  {entity_id:<{_ENTITY_ID_WIDTH}} "
            f"team not in DB"
        )
        summary.missing_team += 1
        return

    # Idempotency check: skip if already populated.
    if team.logo_url and not force:
        print(
            f"SKIP  {entity_id:<{_ENTITY_ID_WIDTH}} "
            f"(already has logo)"
        )
        summary.skipped += 1
        return

    # Storage key matches the admin endpoint's convention so an admin
    # re-upload (or a later dashboard re-upload) hits the same object.
    key = f"{_TEAMS_KEY_PREFIX}/{entity_id}.png"
    content_type = _content_type_for(file_path)

    if dry_run:
        print(
            f"DRY   {entity_id:<{_ENTITY_ID_WIDTH}} "
            f"(would upload {rel_file})"
        )
        summary.dry += 1
        return

    try:
        data = file_path.read_bytes()
        new_url = await storage.save(key, data, content_type)
        team.logo_url = new_url
        await session.commit()
        print(
            f"OK    {entity_id:<{_ENTITY_ID_WIDTH}} "
            f"{new_url}"
        )
        summary.ok += 1
    except Exception as exc:  # noqa: BLE001  — log + continue, never abort batch
        LOG.warning(
            "upload failed for entity_id=%s: %s: %s",
            entity_id, type(exc).__name__, exc,
        )
        await session.rollback()
        print(
            f"ERROR {entity_id:<{_ENTITY_ID_WIDTH}} "
            f"{type(exc).__name__}: {exc}"
        )
        summary.errors.append(entity_id)


async def bootstrap_team_logos(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    team_id: str | None = None,
    force: bool = False,
    manifest_path: Path = MANIFEST_PATH,
) -> BootstrapSummary:
    """Upload all team logos and update their ``logo_url`` in the DB.

    Returns a :class:`BootstrapSummary` the caller can render. Raises if
    the manifest file is missing or unparseable.
    """
    entries = _load_manifest(manifest_path)

    # Filter by team_id first — that constraint may reduce the set below
    # ``limit``, which is the natural interpretation ("up to N entries").
    if team_id is not None:
        entries = [e for e in entries if e.get("entity_id") == team_id]
        if not entries:
            raise ValueError(
                f"--team-id={team_id!r} matched zero entries in manifest"
            )

    if limit is not None:
        entries = entries[:limit]

    # The manifest's ``file`` paths are relative to the project root, not
    # the manifest's own directory. For the default layout that means the
    # grandparent of the manifest path (``<root>/data/manifest.json`` →
    # ``<root>``). For an arbitrary ``--manifest`` we make the same
    # assumption and let the user point it at the right place with a flag.
    manifest_dir = manifest_path.resolve().parent.parent

    summary = BootstrapSummary()
    storage = get_storage()

    async with AsyncSessionLocal() as session:
        for entry in entries:
            await _process_one(
                session=session,
                storage=storage,
                entry=entry,
                manifest_dir=manifest_dir,
                dry_run=dry_run,
                force=force,
                summary=summary,
            )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be uploaded without touching anything",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N entries (default: all)",
    )
    parser.add_argument(
        "--team-id", type=str, default=None,
        help="Process only this one entity_id (exact match)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-upload and overwrite even if logo_url is set",
    )
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST_PATH,
        help=f"Path to the manifest (default: {MANIFEST_PATH})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    summary = asyncio.run(bootstrap_team_logos(
        dry_run=args.dry_run,
        limit=args.limit,
        team_id=args.team_id,
        force=args.force,
        manifest_path=args.manifest,
    ))
    print()
    print(summary.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())