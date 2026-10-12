"""Tests for the one-off ``bootstrap_team_logos`` script.

Mirrors the storage patterns in ``tests/test_image_uploads.py``:
``STORAGE_BACKEND=local`` pointed at a per-test ``tmp_path`` so no AWS
credentials are needed. The script is imported and called directly — no
subprocess — to keep assertions fast and the failure output readable.

What's covered:

1. Happy path — pre-create 3 teams, run, all 3 logos land.
2. ``--dry-run`` — nothing uploaded, nothing written.
3. Idempotency — second run skips everything (no force).
5. ``--limit`` — only N entries are processed.
6. ``--team-id`` — only the named entry is processed.
7. Missing file on disk — WARN + skip.
8. Entity_id missing from DB — WARN + skip.
9. ``logo_url`` written to DB matches ``storage.get_public_url(key)``.
10. ``--force`` — already-set ``logo_url`` is overwritten.
"""
from __future__ import annotations

import json
import uuid as _uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import settings as live_settings
from app.db.models import TeamIdentityMap
from app.db.session import AsyncSessionLocal, engine
from app.scripts import bootstrap_team_logos


# ---------- Tiny valid PNG (1x1 transparent) ----------
# Same bytes used in tests/test_image_uploads.py — magic bytes are real PNG.
TINY_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C4"
    "890000000D49444154789C6300010000000500010D0A2DB40000000049454E44"
    "AE426082"
)

# Per-test isolation. ``btl-`` is the prefix every team_id created by this
# file uses; the autouse cleanup below deletes any leftover rows so the
# tests are independent of run order.
_TEST_PREFIX = "btl-"


# ---------- Fixtures ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_btl_rows():
    """Wipe ``btl-*`` teams after each test so the suite is hermetic."""
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM team_identity_map "
                f"WHERE entity_id LIKE '{_TEST_PREFIX}%'"
            )
        )


@pytest.fixture
def local_storage_path(monkeypatch, tmp_path):
    """Pin storage to local + a per-test ``tmp_path``.

    Mirrors ``local_storage_path`` in ``tests/test_image_uploads.py``: the
    settings instance is built at import time, so we mutate it in place
    AND set the env vars so a fresh factory call resolves correctly.
    """
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    live_settings.storage_backend = "local"
    live_settings.storage_local_path = str(uploads)
    live_settings.storage_public_base_url = "http://testserver/static/uploads"
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(uploads))
    monkeypatch.setenv("STORAGE_PUBLIC_BASE_URL", "http://testserver/static/uploads")
    return uploads


def _make_manifest(manifest_dir: Path, entries: list[dict]) -> Path:
    """Write a tiny manifest file + matching PNG files, return the manifest path.

    The PNGs are written from a constant so the bytes are tiny but valid
    PNGs (magic-byte check passes, ``StorageBackend.save`` accepts them).
    """
    asset_dir = manifest_dir / "data" / "team-assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    serialised_entries = []
    for e in entries:
        png_path = asset_dir / Path(e["file"]).name
        png_path.write_bytes(TINY_PNG)
        serialised_entries.append({
            "entity_id": e["entity_id"],
            "file": e["file"],
            "bytes": len(TINY_PNG),
            "width": 1,
            "height": 1,
            "mode": "RGBA",
        })
    manifest_path = manifest_dir / "data" / "team-assets.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {"schema_version": "1.0.0", "entries": serialised_entries},
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest_path


def _entries_for(entity_ids: list[str]) -> list[dict]:
    """Build a manifest entry for each entity_id. File paths are
    ``data/team-assets/<entity_id>.png``."""
    return [
        {"entity_id": eid, "file": f"data/team-assets/{eid}.png"}
        for eid in entity_ids
    ]


@pytest_asyncio.fixture
async def make_team():
    """Create a ``team_identity_map`` row with a unique ``btl-*`` slug so the
    cleanup fixture can find it. Returns ``(entity_id, team_id)``.
    """

    async def _create(slug_hint: str = "") -> tuple[str, int]:
        suffix = slug_hint or _uuid.uuid4().hex[:8]
        entity_id = f"{_TEST_PREFIX}{suffix}"
        async with AsyncSessionLocal() as session:
            t = TeamIdentityMap(
                entity_id=entity_id,
                competition_id="laliga-ea-sports",
                canonical_name=f"Bsl {suffix}",
            )
            session.add(t)
            await session.commit()
            await session.refresh(t)
            return entity_id, t.id

    return _create


# ---------- Helpers ----------


async def _read_logo_url(team_id: int) -> str | None:
    """Return the current ``logo_url`` for a team, or ``None`` if unset."""
    async with engine.begin() as conn:
        row = (await conn.execute(
            text("SELECT logo_url FROM team_identity_map WHERE id=:t"),
            {"t": team_id},
        )).first()
    if row is None:
        return None
    return row[0]


# ---------- Tests ----------


async def test_bootstrap_happy_path_uploads_missing_logos(
    local_storage_path, tmp_path, make_team,
):
    """3 teams, none with logos, run the script: all 3 end up with logos."""
    entity_ids: list[str] = []
    eid_to_id: dict[str, int] = {}
    for slug in ["alpha", "bravo", "charlie"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )

    assert summary.ok == 3
    assert summary.errors == []

    for eid in entity_ids:
        url = await _read_logo_url(eid_to_id[eid])
        assert url is not None, f"{eid} has no logo_url"
        assert url.endswith(f"/teams/{eid}.png")
        # File on disk
        assert (local_storage_path / "teams" / f"{eid}.png").exists()


async def test_bootstrap_dry_run_does_not_upload(
    local_storage_path, tmp_path, make_team,
):
    """``dry_run=True`` must NOT touch the DB or write files."""
    entity_ids: list[str] = []
    eid_to_id: dict[str, int] = {}
    for slug in ["alpha", "bravo", "charlie"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        dry_run=True,
        manifest_path=manifest_path,
    )

    assert summary.dry == 3
    assert summary.ok == 0

    for eid in entity_ids:
        assert await _read_logo_url(eid_to_id[eid]) is None
        assert not (local_storage_path / "teams" / f"{eid}.png").exists()


async def test_bootstrap_skips_existing_logos(
    local_storage_path, tmp_path, make_team,
):
    """Teams already with ``logo_url`` are skipped (idempotency)."""
    entity_ids: list[str] = []
    eid_to_id: dict[str, int] = {}
    for slug in ["alpha", "bravo"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid
        # Pre-set logo_url to something — the script must not overwrite it.
        async with AsyncSessionLocal() as session:
            row = await session.get(TeamIdentityMap, tid)
            row.logo_url = f"http://testserver/static/uploads/teams/{eid}.png"
            await session.commit()

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )

    assert summary.skipped == 2
    assert summary.ok == 0

    for eid in entity_ids:
        url = await _read_logo_url(eid_to_id[eid])
        # URL unchanged.
        assert url == f"http://testserver/static/uploads/teams/{eid}.png"


async def test_bootstrap_force_overwrites_existing(
    local_storage_path, tmp_path, make_team,
):
    """``--force`` re-uploads even when ``logo_url`` is already set."""
    eid, tid = await make_team("alpha")
    async with AsyncSessionLocal() as session:
        row = await session.get(TeamIdentityMap, tid)
        row.logo_url = "http://testserver/static/uploads/teams/OLD.png"
        await session.commit()

    manifest_path = _make_manifest(tmp_path, _entries_for([eid]))

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        force=True,
        manifest_path=manifest_path,
    )

    assert summary.ok == 1

    new_url = await _read_logo_url(tid)
    assert new_url is not None
    assert new_url != "http://testserver/static/uploads/teams/OLD.png"
    assert new_url.endswith(f"/teams/{eid}.png")


async def test_bootstrap_limit_processes_only_n(
    local_storage_path, tmp_path, make_team,
):
    """``--limit 3`` on 5 entries only processes the first 3."""
    entity_ids: list[str] = []
    eid_to_id: dict[str, int] = {}
    for slug in ["alpha", "bravo", "charlie", "delta", "echo"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        limit=3,
        manifest_path=manifest_path,
    )

    assert summary.ok == 3

    for eid in entity_ids[:3]:
        assert await _read_logo_url(eid_to_id[eid]) is not None
    for eid in entity_ids[3:]:
        assert await _read_logo_url(eid_to_id[eid]) is None


async def test_bootstrap_team_id_processes_only_that(
    local_storage_path, tmp_path, make_team,
):
    """``--team-id`` filters to exactly one entry."""
    eid_to_id: dict[str, int] = {}
    entity_ids: list[str] = []
    for slug in ["alpha", "bravo", "charlie"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        team_id=f"{_TEST_PREFIX}bravo",
        manifest_path=manifest_path,
    )

    assert summary.ok == 1
    target_eid = f"{_TEST_PREFIX}bravo"
    assert await _read_logo_url(eid_to_id[target_eid]) is not None
    assert await _read_logo_url(eid_to_id[f"{_TEST_PREFIX}alpha"]) is None
    assert await _read_logo_url(eid_to_id[f"{_TEST_PREFIX}charlie"]) is None


async def test_bootstrap_team_id_with_no_match_raises(
    local_storage_path, tmp_path, make_team,
):
    """``--team-id`` that matches zero entries must raise — not silently
    succeed with zero uploads."""
    eid_to_id: dict[str, int] = {}
    entity_ids: list[str] = []
    for slug in ["alpha", "bravo"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    with pytest.raises(ValueError, match="matched zero entries"):
        await bootstrap_team_logos.bootstrap_team_logos(
            team_id=f"{_TEST_PREFIX}ghost",
            manifest_path=manifest_path,
        )

    for tid in eid_to_id.values():
        assert await _read_logo_url(tid) is None


async def test_bootstrap_skips_missing_file(
    local_storage_path, tmp_path, make_team,
):
    """A manifest entry pointing at a non-existent file is WARN-and-skipped,
    the rest of the run continues."""
    good_eid, good_tid = await make_team("alpha")
    bad_eid, bad_tid = await make_team("bravo")

    # Build a manifest where one entry's PNG file does NOT exist on disk.
    entries = [
        {"entity_id": good_eid, "file": f"data/team-assets/{good_eid}.png"},
        {"entity_id": bad_eid, "file": "data/team-assets/does-not-exist.png"},
    ]
    manifest_dir = tmp_path
    asset_dir = manifest_dir / "data" / "team-assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / f"{good_eid}.png").write_bytes(TINY_PNG)
    # Deliberately do NOT write the bad one.
    manifest_path = manifest_dir / "data" / "team-assets.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "entries": [
                    {**e, "bytes": len(TINY_PNG), "width": 1, "height": 1, "mode": "RGBA"}
                    for e in entries
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )

    assert summary.ok == 1
    assert summary.missing_file == 1
    assert summary.errors == []

    # Good team got uploaded.
    assert await _read_logo_url(good_tid) is not None
    # Bad team untouched.
    assert await _read_logo_url(bad_tid) is None


async def test_bootstrap_skips_team_not_in_db(
    local_storage_path, tmp_path,
):
    """A manifest entry whose ``entity_id`` is NOT in the DB is WARN-skip."""
    # Build a manifest with one entry whose entity_id has NO matching DB row.
    missing_eid = f"{_TEST_PREFIX}ghost"
    manifest_path = _make_manifest(tmp_path, [
        {"entity_id": missing_eid, "file": f"data/team-assets/{missing_eid}.png"},
    ])

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )

    assert summary.missing_team == 1
    assert summary.ok == 0
    assert summary.errors == []


async def test_bootstrap_uses_storage_backend_public_url(
    local_storage_path, tmp_path, make_team,
):
    """``logo_url`` written to the DB matches ``storage.get_public_url(key)``.

    This is the contract the rest of the app relies on: the URL stored on
    the row is the public URL the storage backend would produce for the
    same key.
    """
    eid, tid = await make_team("alpha")
    manifest_path = _make_manifest(tmp_path, _entries_for([eid]))

    # Resolve through the factory (which honours STORAGE_BACKEND env).
    from app.storage import get_storage
    storage = get_storage()
    expected_key = f"teams/{eid}.png"
    expected_url = storage.get_public_url(expected_key)

    summary = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )

    assert summary.ok == 1
    assert await _read_logo_url(tid) == expected_url
    assert expected_url == f"http://testserver/static/uploads/{expected_key}"


async def test_bootstrap_idempotent_on_rerun(
    local_storage_path, tmp_path, make_team,
):
    """Run twice without ``--force``: the second run uploads zero teams."""
    entity_ids: list[str] = []
    eid_to_id: dict[str, int] = {}
    for slug in ["alpha", "bravo", "charlie"]:
        eid, tid = await make_team(slug)
        entity_ids.append(eid)
        eid_to_id[eid] = tid

    manifest_path = _make_manifest(tmp_path, _entries_for(entity_ids))

    first = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )
    assert first.ok == 3

    second = await bootstrap_team_logos.bootstrap_team_logos(
        manifest_path=manifest_path,
    )
    assert second.ok == 0
    assert second.skipped == 3

    # All teams still have their logos, unchanged from the first run.
    for eid in entity_ids:
        url = await _read_logo_url(eid_to_id[eid])
        assert url is not None
        assert url.endswith(f"/teams/{eid}.png")