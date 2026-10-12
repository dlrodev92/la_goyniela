#!/usr/bin/env bash
# Setup the test database (run once after cloning or after schema changes).
#
# Creates ``la_goyniela_test`` as a separate database in the same Postgres
# instance as the dev DB, applies migrations, and seeds the catalog. After
# this, ``uv run pytest`` is safe to run against the dev DB without losing
# data — the test suite operates on this database.
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEV_DB_URL="${DATABASE_URL:-postgresql+psycopg://la_goyniela:la_goyniela@localhost:5433/la_goyniela}"
TEST_DB_NAME="la_goyniela_test"
TEST_DB_URL="${TEST_DATABASE_URL:-$(echo "$DEV_DB_URL" | sed "s|/[^/]*\$|/${TEST_DB_NAME}|")}"

echo "Dev DB:    $DEV_DB_URL"
echo "Test DB:   $TEST_DB_URL"
echo

# ---------------------------------------------------------------------------
# 1. Create the test DB if it doesn't exist.
# ---------------------------------------------------------------------------
if ! uv run python -c "
import asyncio, asyncpg
async def main():
    conn = await asyncpg.connect(
        host='localhost', port=5433, user='la_goyniela', password='la_goyniela',
        database='postgres',
    )
    exists = await conn.fetchval(
        \"SELECT 1 FROM pg_database WHERE datname = 'la_goyniela_test'\"
    )
    print('EXISTS' if exists else 'MISSING')
    await conn.close()
asyncio.run(main())
" 2>&1 | grep -q EXISTS; then
    echo "[1/3] Test DB 'la_goyniela_test' already exists, skipping CREATE"
else
    echo "[1/3] Creating test DB 'la_goyniela_test'..."
    uv run python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    eng = create_async_engine(
        'postgresql+psycopg://la_goyniela:la_goyniela@localhost:5433/postgres',
        isolation_level='AUTOCOMMIT',
    )
    async with eng.begin() as conn:
        await conn.execute(text('CREATE DATABASE la_goyniela_test'))
    await eng.dispose()
asyncio.run(main())
"
fi

# ---------------------------------------------------------------------------
# 2. Apply migrations to the test DB.
# ---------------------------------------------------------------------------
echo
echo "[2/3] Applying migrations to test DB..."
DATABASE_URL="$TEST_DB_URL" uv run alembic upgrade head

# ---------------------------------------------------------------------------
# 3. Seed the catalog (competitions, sources, teams, api-football IDs).
# ---------------------------------------------------------------------------
echo
echo "[3/3] Seeding catalog in test DB..."
DATABASE_URL="$TEST_DB_URL" uv run python -m app.scripts.load_team_catalog

echo
echo "Done. You can now run tests safely:"
echo "  uv run pytest tests/"
