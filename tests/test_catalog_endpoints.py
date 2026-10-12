"""Tests for /api/v1/competitions and /api/v1/teams.

Assumes the catalog tables are populated. Run `python3 app/scripts/load_team_catalog.py`
before these tests, or set up a pytest fixture that loads the catalog if empty.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_health_db_still_works(client):
    """Smoke test — the /health/db endpoint should still return 200."""
    r = await client.get("/health/db")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"


async def test_list_competitions_returns_9(client):
    r = await client.get("/api/v1/competitions?limit=200")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 9  # >= in case the catalog grows
    assert len(body["items"]) == body["total"]
    ids = {c["id"] for c in body["items"]}
    assert "laliga-ea-sports" in ids
    assert "liga-f" in ids
    assert "uefa-champions-league" in ids


async def test_get_competition_by_id(client):
    r = await client.get("/api/v1/competitions/laliga-ea-sports")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "laliga-ea-sports"
    assert body["name"] == "LALIGA EA SPORTS"
    assert body["country"] == "Spain"
    assert body["tier"] == 1


async def test_get_competition_404(client):
    r = await client.get("/api/v1/competitions/does-not-exist")
    assert r.status_code == 404


async def test_filter_competitions_by_tier(client):
    r = await client.get("/api/v1/competitions?tier=1&limit=200")
    assert r.status_code == 200
    body = r.json()
    for c in body["items"]:
        assert c["tier"] == 1


async def test_filter_competitions_by_country(client):
    r = await client.get("/api/v1/competitions?country=Spain&limit=200")
    assert r.status_code == 200
    body = r.json()
    for c in body["items"]:
        assert c["country"] == "Spain"


async def test_list_teams_paginated(client):
    r = await client.get("/api/v1/teams?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 423
    assert len(body["items"]) == 10
    assert body["limit"] == 10
    assert body["offset"] == 0


async def test_filter_teams_by_competition(client):
    r = await client.get("/api/v1/teams?competition_id=laliga-ea-sports&limit=200")
    assert r.status_code == 200
    body = r.json()
    for t in body["items"]:
        assert t["competition_id"] == "laliga-ea-sports"


async def test_filter_teams_by_gender(client):
    r = await client.get("/api/v1/teams?gender=women&limit=200")
    assert r.status_code == 200
    body = r.json()
    for t in body["items"]:
        assert t["gender"] == "women"


async def test_filter_teams_by_q(client):
    r = await client.get("/api/v1/teams?q=Barcelona&limit=5")
    assert r.status_code == 200
    body = r.json()
    for t in body["items"]:
        assert "barcelona" in t["canonical_name"].lower()


async def test_get_team_by_id(client):
    # Find any team ID first
    r = await client.get("/api/v1/teams?limit=1")
    assert r.status_code == 200
    team_id = r.json()["items"][0]["id"]

    r = await client.get(f"/api/v1/teams/{team_id}")
    assert r.status_code == 200
    assert r.json()["id"] == team_id


async def test_get_team_404(client):
    r = await client.get("/api/v1/teams/9999999")
    assert r.status_code == 404


async def test_pagination_offsets(client):
    """Different offsets return different items, with consistent total."""
    r1 = await client.get("/api/v1/teams?limit=10&offset=0")
    r2 = await client.get("/api/v1/teams?limit=10&offset=10")
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["total"] == r2.json()["total"]
    assert r1.json()["items"][0]["id"] != r2.json()["items"][0]["id"]