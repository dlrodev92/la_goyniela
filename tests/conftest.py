"""Shared test fixtures and pytest configuration.

Tests run against a **separate test database** to avoid clobbering dev data.
The test DB URL is derived from ``DATABASE_URL`` (same host/user/password,
db name = ``la_goyniela_test``) unless ``TEST_DATABASE_URL`` is set explicitly
in the environment. The redirection happens at module-load time — BEFORE any
app imports — so the engine is created against the test DB.

Setup (one-time):

.. code-block:: bash

    # 1. Create the test database (requires superuser or CREATEDB privilege)
    createdb la_goyniela_test

    # 2. Run migrations + load the catalog seed against the test DB
    TEST_DATABASE_URL=postgresql+psycopg://la_goyniela:la_goyniela@localhost:5433/la_goyniela_test \\
        uv run alembic upgrade head
    TEST_DATABASE_URL=... \\
        uv run python -m app.scripts.load_team_catalog

Run tests:

.. code-block:: bash

    uv run pytest tests/

To use a custom test DB (e.g. on CI):

.. code-block:: bash

    TEST_DATABASE_URL=postgresql://...my_test_db uv run pytest tests/

The dev database (``la_goyniela``) is never touched by tests.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Bootstrap: redirect DATABASE_URL to the test DB BEFORE importing the app.
# ---------------------------------------------------------------------------
# ``Settings`` reads DATABASE_URL at instantiation; ``app.db.session`` builds
# the engine the moment ``app.main`` is imported. Both must see the test DB.
_TEST_DB_NAME = "la_goyniela_test"
_DEFAULT_TEST_URL = (
    f"postgresql+psycopg://la_goyniela:la_goyniela@localhost:5433/{_TEST_DB_NAME}"
)
if "TEST_DATABASE_URL" not in os.environ:
    if "DATABASE_URL" in os.environ:
        # Derive from the dev URL: same host/port/user/password, different db.
        base, _, _name = os.environ["DATABASE_URL"].rpartition("/")
        os.environ["TEST_DATABASE_URL"] = f"{base}/{_TEST_DB_NAME}"
    else:
        # No dev DB set either — use the default localhost test URL.
        # This is safe: if the test DB doesn't exist, the first test that
        # uses the engine will fail loudly with "database does not exist".
        os.environ["TEST_DATABASE_URL"] = _DEFAULT_TEST_URL
# Make the app see the test DB URL.
os.environ.setdefault("DATABASE_URL", os.environ["TEST_DATABASE_URL"])

# Now safe to import the app — engine is created with the test DB URL.
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402

# Pre-existing data-layer test from scripts/build_team_catalog.py requires PIL
# (Pillow), which is a data-only dependency not needed by the API. Skip it from
# pytest collection; it can still be run manually with `python3 -m unittest
# tests/test_team_catalog.py` if PIL is installed.
collect_ignore_glob = ["test_team_catalog.py"]


# Catalog tables seeded at DB-bootstrap. Their rows must SURVIVE every test
# (re-seeding inside the suite would require running ``load_team_catalog.py``
# per test — slow and racy). Used by ``_reset_test_data_between_tests`` to
# decide what NOT to touch.
_CATALOG_TABLES: frozenset[str] = frozenset({
    "competition",
    "source",
    "team_id_api_football",
})

# ``entity_id`` prefixes used by test fixtures to mark throwaway team rows.
# Catalog rows (loaded by ``app/scripts/load_team_catalog.py``) never use
# these prefixes — they look like ``club-...``, ``national-...``, or sport
# slugs. Test suites covered:
#   - test_jornadas / test_jornada_manager / test_team_matcher /
#     test_ingest_quiniwin / test_scheduler   → ``test-``, ``auto-``
#   - test_admin_jornadas                     → ``test-jrn-`` (covered by
#                                              ``test-``) + ``auto-``
#   - test_team_embed                         → ``test-``, ``embed-``
#   - test_bootstrap_team_logos               → ``btl-``
_TEST_TEAM_PREFIXES: tuple[str, ...] = ("test-%", "auto-%", "embed-%", "btl-%")


@pytest_asyncio.fixture(autouse=True)
async def _reset_test_data_between_tests():
    """Truncate test-owned tables before EVERY test, so the suite cannot leak.

    History (slice 10, pre-existing flake):
        Prior to this fixture, every test file cleaned up after itself via a
        per-file ``autouse`` fixture. That worked when the suite ran in
        isolation, but two classes of leakage still happened:

        1. **Setup-only wipes** (e.g. ``tests/test_groups.py``) deleted
           their known email patterns BEFORE the test ran but did nothing
           AFTER — so a later test in a *different* file that didn't know
           about those patterns could pick up the leftover rows.

        2. **Teardown-only wipes** (e.g. ``tests/test_porra.py``,
           ``tests/test_admin_jornadas.py``) ran only AFTER the test
           finished, so rows created mid-test lived in the DB while the
           next test started — and if the next test asserted on
           "the user is admin of ZERO groups", a leftover admin row from
           the previous test broke the 403 assertion. This is the
           ``test_non_admin_gets_403`` flake.

        The catalog tests (``test_catalog_endpoints.py``) hit a related
        variant of (2): ``test_pagination_offsets`` only broke when
        ``test_admin_jornadas`` had leaked team rows that then collided
        with the catalog seed during pagination.

        The fix: a single autouse, SETUP-only fixture here that truncates
        every test-owned table before each test. After this runs, the DB is
        in the same state as it would be right after a fresh
        ``load_team_catalog.py`` — empty apart from the catalog seed.
        Per-file autouse teardowns stay in place as defensive belt-and-
        suspenders (their DELETE statements are idempotent and cheap).

    Preservation (rows that MUST NOT be deleted):

    - ``alembic_version`` — never touched (would invalidate migrations).
    - ``competition``, ``source``, ``team_id_api_football`` — catalog seed
      loaded by ``app/scripts/load_team_catalog.py``; the catalog tests
      rely on these surviving.
    - ``team_identity_map`` — catalog seed PLUS test-created rows. We
      ``DELETE`` only rows whose ``entity_id`` starts with one of the
      known test prefixes (``_TEST_TEAM_PREFIXES``). Catalog rows are
      preserved.

    Performance: ~10 ``DELETE`` statements in one transaction. Empirically
    <5 ms per test on localhost, well under the 10 % suite-overhead
    budget. We use ``DELETE`` (not ``TRUNCATE``) because
    ``composicion``/``jornada_escrutinio``/``jornada_comentario``/
    ``fetch_session`` reference the catalog tables via RESTRICT FKs —
    ``TRUNCATE`` would either fail or require ``CASCADE`` that nukes the
    seed.
    """
    async with engine.begin() as conn:
        # Children first, parents last. Independent of test order: every
        # run starts from a known-empty state.
        await conn.execute(text("DELETE FROM pick"))
        await conn.execute(text("DELETE FROM jornada_escrutinio"))
        await conn.execute(text("DELETE FROM jornada_comentario"))
        await conn.execute(text("DELETE FROM composicion"))
        await conn.execute(text("DELETE FROM fetch_session"))
        await conn.execute(text("DELETE FROM porra"))
        await conn.execute(text("DELETE FROM group_member"))
        await conn.execute(text('DELETE FROM "group"'))
        await conn.execute(text("DELETE FROM jornada"))
        await conn.execute(text('DELETE FROM "user"'))
        # Test-only team rows. Catalog rows are preserved.
        await conn.execute(text(
            "DELETE FROM team_identity_map "
            "WHERE entity_id LIKE 'test-%' "
            "   OR entity_id LIKE 'auto-%' "
            "   OR entity_id LIKE 'embed-%' "
            "   OR entity_id LIKE 'btl-%'"
        ))
    yield


@pytest_asyncio.fixture
async def client():
    """An httpx.AsyncClient wired to the FastAPI app via ASGI transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
