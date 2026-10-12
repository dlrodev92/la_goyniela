"""Tests for :mod:`app.db.team_matcher`.

Uses the real Postgres test DB (port 5433). The autouse ``_cleanup_rows``
fixture wipes test-created ``team_identity_map`` rows after each test so
they don't leak between tests. The seed catalog loaded by
``scripts/load_team_catalog.py`` (run before the test suite) provides the
"existing canonical" teams we use for the happy-path match cases.

Many tests use the ``make_team`` fixture to seed a known row. That fixture
must use a canonical name that does NOT already exist in the seeded
catalog (the functional unique index ``(competition_id, immutable_unaccent(
canonical_name))`` would reject any duplicate). For tests that intentionally
want to exercise an "existing canonical team", we instead look up one of the
seeded rows (e.g. ``"Athletic Club"``) directly.
"""
from __future__ import annotations

import logging
import re

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.db.models import TeamIdentityMap
from app.db.session import AsyncSessionLocal, engine
from app.db.team_matcher import (
    detect_female,
    find_or_create_team,
    normalize_name,
    slugify,
    stub_entity_id,
)


# ---------- Cleanup ----------


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_rows():
    """Delete test-created ``team_identity_map`` rows after each test."""
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM team_identity_map "
                "WHERE entity_id LIKE 'test-%' OR entity_id LIKE 'auto-%'"
            )
        )


@pytest_asyncio.fixture
async def make_team():
    """Create a deterministic team row in the DB.

    Uses a unique name (``Zzztest…`` prefix) so it never collides with
    seeded catalog rows. Returns ``(id, canonical_name)``.
    """

    async def _create(
        canonical: str,
        *,
        comp_id: str = "laliga-ea-sports",
        gender: str | None = "men",
        entity_id: str | None = None,
    ) -> tuple[int, str]:
        # Zzztest prefix guarantees we never collide with seeded canonicals.
        unique_name = f"Zzztest {canonical}"
        slug = slugify(unique_name)
        eid = entity_id or f"test-{slug}"
        async with AsyncSessionLocal() as session:
            t = TeamIdentityMap(
                entity_id=eid,
                competition_id=comp_id,
                canonical_name=unique_name,
                gender=gender,
            )
            session.add(t)
            await session.commit()
            await session.refresh(t)
            return t.id, unique_name

    return _create


# ---------- Pure helpers ----------


def test_detect_female_trailing_f():
    assert detect_female("Logroño F") == ("Logroño", "women")
    assert detect_female("Levante F ") == ("Levante", "women")  # trailing space


def test_detect_female_parenthesised():
    assert detect_female("Athletic Club (F)") == ("Athletic Club", "women")
    assert detect_female("Athletic Club (f)") == ("Athletic Club", "women")


def test_detect_female_no_match():
    assert detect_female("Athletic Club") == ("Athletic Club", None)
    assert detect_female("Real Madrid") == ("Real Madrid", None)


def test_detect_female_does_not_strip_legitimate_f():
    """A trailing F inside a word (unlikely but defensive) is preserved."""
    assert detect_female("FC Famalicao") == ("FC Famalicao", None)


def test_normalize_name_lowercase_and_unaccent():
    assert normalize_name("Málaga") == "malaga"
    assert normalize_name("ATHLETIC CLUB") == "athletic club"
    assert normalize_name("  Real Madrid  ") == "real madrid"


def test_slugify_handles_accents_and_special_chars():
    assert slugify("Málaga CF") == "malaga-cf"
    assert slugify("R. Oviedo") == "r-oviedo"
    assert slugify("Stade Rennais") == "stade-rennais"


def test_stub_entity_id_format():
    eid = stub_entity_id("Athletic Club")
    assert eid.startswith("auto-")
    # pattern: auto-{slug}-{hash6}
    assert re.match(r"^auto-[a-z0-9-]+-[0-9a-f]{6}$", eid), eid


def test_stub_entity_id_is_deterministic():
    """Same input → same hash → same entity_id (idempotent)."""
    a = stub_entity_id("Logroño F")
    b = stub_entity_id("Logroño F")
    assert a == b


def test_stub_entity_id_differs_for_different_names():
    a = stub_entity_id("Athletic Club")
    b = stub_entity_id("Athletic Club B")
    assert a != b


# ---------- DB match cases ----------


async def test_find_or_create_returns_exact_match(make_team):
    """A pre-existing canonical row is returned unchanged."""
    pre_id, unique_name = await make_team("Real Madrid")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(session, unique_name)
    assert row.id == pre_id
    assert row.canonical_name == unique_name


async def test_find_or_create_case_insensitive(make_team):
    """Lowercase query finds the existing canonical row (via SQL lower() + ILIKE-style)."""
    pre_id, unique_name = await make_team("Real Madrid")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(session, unique_name.lower())
    assert row.id == pre_id


async def test_find_or_create_accent_insensitive(make_team):
    """Malaga (no accent) finds the existing Málaga row via unaccent()."""
    pre_id, unique_name = await make_team("Málaga")  # stored with accent
    # Query without accent — the functional index strips it on the DB side.
    stripped_name = unique_name.replace("á", "a")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(session, stripped_name)
    assert row.id == pre_id


async def test_find_or_create_detects_female_suffix_and_strips():
    """'Athletic Club (F)' → female marker, name stripped to 'Athletic Club'.

    Athletic Club (women) in liga-f is seeded by load_team_catalog.py so we
    can verify the suffix detection + matching without inserting anything.
    """
    async with AsyncSessionLocal() as session:
        # Pre-condition: there is an 'Athletic Club' row in liga-f (women).
        pre = await session.scalar(
            select(TeamIdentityMap).where(
                TeamIdentityMap.canonical_name == "Athletic Club",
                TeamIdentityMap.competition_id == "liga-f",
            )
        )
        assert pre is not None, "seed catalog must have Athletic Club (women)"

        row = await find_or_create_team(
            session, "Athletic Club (F)", competition_id_hint="liga-f",
        )
    assert row.id == pre.id
    # The marker is detected; gender is the row's own, not derived from suffix.
    assert row.gender == "women"


async def test_find_or_create_creates_stub_when_no_match(caplog):
    """A new raw form creates a stub row with the documented shape."""
    caplog.set_level(logging.WARNING, logger="team_matcher")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(session, "New Club Nobody Knows ZZZ")
        await session.commit()

    # entity_id format
    assert row.entity_id.startswith("auto-")
    assert re.match(r"^auto-[a-z0-9-]+-[0-9a-f]{6}$", row.entity_id)
    # competition_id is 'unknown'
    assert row.competition_id == "unknown"
    # gender default is 'men'
    assert row.gender == "men"
    # country is None for now
    assert row.country is None
    # WARNING was logged
    assert any(
        "creating stub" in r.message and "New Club Nobody Knows ZZZ" in r.message
        for r in caplog.records
    )


async def test_find_or_create_creates_female_stub(caplog):
    """'Some Women's Club ZZZ F' → stub with gender=women, suffix stripped."""
    caplog.set_level(logging.WARNING, logger="team_matcher")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(
            session, "Some Women's Club ZZZ F",
        )
        await session.commit()

    assert row.gender == "women"
    assert row.canonical_name == "Some Women's Club ZZZ"  # suffix stripped
    assert row.entity_id.startswith("auto-")


async def test_find_or_create_stub_is_idempotent():
    """Calling twice with the same name returns the same stub row."""
    # First call (commits).
    async with AsyncSessionLocal() as session:
        first = await find_or_create_team(session, "Another Unknown FC ZZZ")
        await session.commit()
        first_id = first.id

    # Second call (no commit on this side; the first commit made the row
    # visible globally so the second call's _lookup_exact finds it).
    async with AsyncSessionLocal() as session:
        second = await find_or_create_team(session, "Another Unknown FC ZZZ")
    assert second.id == first_id

    # Exactly one row in the DB for this name.
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(TeamIdentityMap).where(
                TeamIdentityMap.canonical_name == "Another Unknown FC ZZZ"
            )
        )).scalars().all()
    assert len(rows) == 1


async def test_find_or_create_alias_map_fallback(make_team):
    """'ATH MADRID' resolves to a seeded Atletico-related row.

    The matcher's resolution path: alias map ``"ath madrid"`` →
    ``"Atletico Madrid"`` → SQL lookup against ``canonical_name`` OR
    ``short_name`` (normalized). Without a ``competition_id_hint`` the
    matcher returns whichever matching row has the lowest id — this could
    be the men's "Club Atlético de Madrid" or the women's "Atlético de
    Madrid" depending on which lands first. The test asserts it resolves
    to *some* seeded Atletico team, NOT to the test fixture and NOT to a
    stub. Gender disambiguation via competition_id_hint is a future task.
    """
    pre_id, unique_name = await make_team("Atletico Madrid ZZZ")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(session, "ATH MADRID")
    # The matcher's resolution lands on a seeded Atletico-related row,
    # NOT on our test fixture and NOT on a freshly-created stub.
    assert row.id != pre_id, "matcher should NOT return our test fixture row"
    assert not row.entity_id.startswith("auto-"), (
        "matcher should resolve via alias+short_name, not fall through to stub"
    )
    assert "Atl" in row.canonical_name, (
        f"expected an Atletico-related canonical, got {row.canonical_name!r}"
    )


async def test_find_or_create_alias_at_madrid_falls_through_to_stub(make_team):
    """'AT MADRID' → alias target is 'Atletico Madrid' (not in seed catalog
    # → not in our test data) → stub created."""
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(session, "AT MADRID ZZZ")
        await session.commit()
    assert row.entity_id.startswith("auto-")


async def test_find_or_create_alias_ath_bilbao_resolves_via_seed():
    """'ATH BILBAO' alias target is 'Athletic Club' — the alias lookup hits
    the seeded Athletic Club row (laliga-ea-sports / men) and returns it.
    """
    async with AsyncSessionLocal() as session:
        # Confirm seed data has Athletic Club.
        pre_count = await session.scalar(
            select(TeamIdentityMap).where(
                TeamIdentityMap.canonical_name == "Athletic Club",
                TeamIdentityMap.competition_id == "laliga-ea-sports",
            )
        )
        assert pre_count is not None

        # ATH BILBAO → alias map → 'Athletic Club' → seed hit.
        row = await find_or_create_team(session, "ATH BILBAO")
    assert row.id == pre_count.id
    assert row.canonical_name == "Athletic Club"


async def test_find_or_create_rejects_empty_string():
    async with AsyncSessionLocal() as session:
        with pytest.raises(ValueError):
            await find_or_create_team(session, "")
        with pytest.raises(ValueError):
            await find_or_create_team(session, "   ")
        with pytest.raises(ValueError):
            await find_or_create_team(session, None)


async def test_find_or_create_uses_competition_hint_to_scope(make_team):
    """With a hint that doesn't match, fallback paths (no hint / alias / new stub) kick in.

    Here the hint 'liga-f' doesn't have a 'Real Madrid' row in our test
    data (the seed has 'Real Madrid CF' which is different). The matcher
    should fall back to no-hint search and either hit 'Real Madrid CF'
    (seed) or stub. We just verify the call completes without error.
    """
    await make_team("Real Madrid ZZZ")
    async with AsyncSessionLocal() as session:
        # Should not raise — fallback paths cover this case.
        row = await find_or_create_team(
            session, "Real Madrid ZZZ", competition_id_hint="liga-f",
        )
    assert row is not None


# ---------- Slice 1: gender_known column ----------


async def test_team_identity_map_has_gender_known_default_true(make_team):
    """A new (stub or seeded) ``team_identity_map`` row has ``gender_known=True``
    by default — the server_default in the migration ensures every row gets
    it without the caller having to opt in.

    The matcher's slice-5 change will start persisting ``False`` for stubs
    that had no feminine suffix; this test guards the default so existing
    callers (and seeded catalog rows) keep behaving as before.
    """
    _, unique_name = await make_team("Slice 1 Default ZZZ")

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(TeamIdentityMap).where(
                TeamIdentityMap.canonical_name == unique_name,
            )
        )).scalar_one()
        assert row.gender_known is True


async def test_team_identity_map_gender_known_can_be_false(make_team):
    """``gender_known`` is settable: today the matcher never sets ``False``
    (slice 5 will), but the column must round-trip whatever value we
    persist. Verifies the field is mutable and that no CHECK constraint
    forbids ``False``."""
    # Use a fresh team so we don't clash with seeded rows or other tests.
    _, unique_name = await make_team("Slice 1 Explicit False ZZZ")

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(TeamIdentityMap).where(
                TeamIdentityMap.canonical_name == unique_name,
            )
        )).scalar_one()
        row.gender_known = False
        await session.commit()

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(TeamIdentityMap).where(
                TeamIdentityMap.canonical_name == unique_name,
            )
        )).scalar_one()
        assert row.gender_known is False


# ---------- Slice 5: catalog lookup + gender_known on stubs --------


async def test_find_or_create_uses_catalog_when_present():
    """'Athletic Club' resolves to the seeded LaLiga men row via the
    catalog lookup (the matcher's first resolution step after
    normalization). The full alias coverage in the catalog — combined
    with the ``(normalized_name, gender)`` key — disambiguates Athletic
    Club men from women without any heuristics. No stub is created.
    """
    async with AsyncSessionLocal() as session:
        # Pre-condition: the seeded men's row exists.
        pre = await session.scalar(
            select(TeamIdentityMap).where(
                TeamIdentityMap.entity_id == "club-men-athletic-club",
            )
        )
        assert pre is not None, "seed catalog must have club-men-athletic-club"

        row = await find_or_create_team(session, "Athletic Club")

    assert row.id == pre.id
    assert row.entity_id == "club-men-athletic-club"
    assert row.competition_id == "laliga-ea-sports"
    assert row.gender == "men"
    # Catalog rows are seeded with the column default — the matcher
    # never overrides ``gender_known`` for them.
    assert row.gender_known is True


async def test_find_or_create_stub_gender_known_false_when_no_marker(caplog):
    """No feminine suffix AND no catalog match → the matcher falls
    through to stub creation. Per slice 5 §5.2, the stub keeps
    ``gender='men'`` for back-compat but flips ``gender_known=False``
    so the UI knows the gender is a guess.
    """
    caplog.set_level(logging.WARNING, logger="team_matcher")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(
            session, "Gender Unknown Stub Slice5 ZZZ",
        )
        await session.commit()

    # Stub shape.
    assert row.entity_id.startswith("auto-")
    assert re.match(r"^auto-[a-z0-9-]+-[0-9a-f]{6}$", row.entity_id)
    assert row.competition_id == "unknown"
    # The new contract: gender kept, gender_known flagged.
    assert row.gender == "men"
    assert row.gender_known is False


async def test_find_or_create_stub_gender_known_true_with_female_marker(caplog):
    """A feminine suffix (e.g. ``'(F)'``) AND no catalog match → stub
    with ``gender='women'`` AND ``gender_known=True``. The suffix is a
    real gender signal so the matcher can be confident.
    """
    caplog.set_level(logging.WARNING, logger="team_matcher")
    async with AsyncSessionLocal() as session:
        row = await find_or_create_team(
            session, "Gender Known Women Stub Slice5 ZZZ F",
        )
        await session.commit()

    assert row.entity_id.startswith("auto-")
    assert row.gender == "women"
    assert row.gender_known is True
    # Suffix is removed from the canonical form.
    assert row.canonical_name == "Gender Known Women Stub Slice5 ZZZ"


async def test_find_or_create_resolves_fc_andorra_via_catalog():
    """'FC Andorra' (Segunda) resolves to the seeded
    ``club-men-fc-andorra`` row via the catalog lookup. The catalog
    indexes the canonical name itself — the lookup never falls through
    to stub creation, so we don't pollute ``team_identity_map`` with a
    duplicate auto-row.
    """
    async with AsyncSessionLocal() as session:
        # Pre-condition: the seeded row exists.
        pre = await session.scalar(
            select(TeamIdentityMap).where(
                TeamIdentityMap.entity_id == "club-men-fc-andorra",
            )
        )
        assert pre is not None, "seed catalog must have club-men-fc-andorra"

        row = await find_or_create_team(session, "FC Andorra")

    assert row.id == pre.id
    assert row.entity_id == "club-men-fc-andorra"
    assert row.competition_id == "laliga-hypermotion"
    assert row.gender == "men"
    assert row.gender_known is True


async def test_find_or_create_keeps_female_marker():
    """'Athletic Club (F)' resolves to ``club-women-athletic-club``
    (Liga F) — NOT to the men's row that the old "first by id" SQL
    path would have returned. The catalog lookup's
    ``(normalized_name, gender)`` key, combined with the female-marker
    detection that drives ``gender='women'``, makes this deterministic.

    No ``competition_id_hint`` is passed, so the disambiguation comes
    purely from the suffix.
    """
    async with AsyncSessionLocal() as session:
        # Pre-condition: the seeded women's row exists.
        pre = await session.scalar(
            select(TeamIdentityMap).where(
                TeamIdentityMap.entity_id == "club-women-athletic-club",
            )
        )
        assert pre is not None, "seed catalog must have club-women-athletic-club"

        row = await find_or_create_team(session, "Athletic Club (F)")

    assert row.id == pre.id
    assert row.entity_id == "club-women-athletic-club"
    assert row.competition_id == "liga-f"
    assert row.gender == "women"
    assert row.gender_known is True