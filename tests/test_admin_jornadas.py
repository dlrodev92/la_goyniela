"""Tests for the admin jornada endpoint.

Slice 3 of the gestor-jornadas plan (``docs/producto/gestor-jornadas.md``
§ 3.2). Covers:

- happy path: admin sets the 15 kickoffs + sale_opens_at; a draft jornada
  transitions to active.
- non-admin → 403.
- unauthenticated → 401.
- missing jornada → 404.
- wrong kickoff count → 422 (both via Pydantic and via the manager).
- naive datetime → 422 (caught at the manager).
- the data persists to the DB after a successful call.

The "admin" gate is the same one ``admin_uploads`` uses — admin of at
least one group. Setup mirrors ``tests/test_image_uploads.py``.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text as sql_text

from app.db.models import (
    Composicion,
    Jornada,
    JornadaStatus,
    TeamIdentityMap,
)
from app.db.session import AsyncSessionLocal, engine
from app.services.jornada_manager import PARTIDOS_PER_JORNADA


# ==========================================================================
# Fixtures.
# ==========================================================================


# Per-process counter so per-test emails / display names don't collide even
# when the test database carries data across runs.
_seq = itertools.count()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_test_data():
    """Delete rows created by these tests after each one runs.

    Same wipe shape as ``test_jornada_manager`` / ``test_jornadas``: drop
    compos rows, jornada rows, the test-only teams, and any user/group
    created via ``_make_admin`` / ``_make_loner``. Order matters:
    ``group`` FKs to ``user`` (RESTRICT), so we delete memberships +
    groups before users.

    The wipe runs AFTER each test (``yield`` first) so a failing test
    still leaves the DB in a known state for the next one.
    """
    yield
    from app.db.session import engine
    async with engine.begin() as conn:
        # Get the user ids first so we can drop groups they own (FK is RESTRICT).
        rows = (await conn.execute(
            sql_text("SELECT id FROM \"user\" WHERE email LIKE 'adm_jrn_%@example.com'"),
        )).all()
        uids = [r[0] for r in rows]
        if uids:
            await conn.execute(
                sql_text("DELETE FROM group_member WHERE user_id = ANY(:uids)"),
                {"uids": uids},
            )
            await conn.execute(
                sql_text("DELETE FROM \"group\" WHERE created_by_user_id = ANY(:uids)"),
                {"uids": uids},
            )
            await conn.execute(
                sql_text("DELETE FROM \"user\" WHERE id = ANY(:uids)"),
                {"uids": uids},
            )
        # Clean jornada + compos rows.
        await conn.execute(sql_text("DELETE FROM jornada_escrutinio"))
        await conn.execute(sql_text("DELETE FROM jornada_comentario"))
        await conn.execute(sql_text("DELETE FROM composicion"))
        await conn.execute(sql_text("DELETE FROM fetch_session"))
        await conn.execute(sql_text("DELETE FROM jornada"))
        # Wipe the test-only team rows so the next test starts blank.
        await conn.execute(
            sql_text(
                "DELETE FROM team_identity_map "
                "WHERE entity_id LIKE 'test-jrn-%'"
            )
        )


# ---------- Auth + group helpers ----------


async def _register(client, email: str, display_name: str, password: str = "hunter22"):
    """Register a fresh user, return ``(token, user_id)``."""
    r = await client.post("/auth/register", json={
        "email": email, "password": password, "display_name": display_name,
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"], r.json()["user"]["id"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_admin(client) -> tuple[str, int, int]:
    """Register an admin user (creates a group, becomes its admin).

    Returns ``(admin_token, admin_id, group_id)``.
    """
    seq = next(_seq)
    email = f"adm_jrn_admin_{seq}@example.com"
    token, admin_id = await _register(client, email, "Admin")
    r = await client.post(
        "/groups", json={"name": f"JrnAdmSeq-{seq}"},
        headers=_bearer(token),
    )
    assert r.status_code == 201, r.text
    return token, admin_id, r.json()["id"]


async def _make_loner(client) -> tuple[str, int]:
    """Register a user who is admin of zero groups."""
    seq = next(_seq)
    email = f"adm_jrn_loner_{seq}@example.com"
    return await _register(client, email, "Loner")


# ---------- Jornada helpers ----------


async def _make_draft_jornada(numero: int = 8, temporada: int = 2026) -> int:
    """Create a draft jornada + 2 teams + 15 compos rows. Returns the id."""
    async with AsyncSessionLocal() as session:
        t1 = TeamIdentityMap(
            entity_id="test-jrn-team-a",
            competition_id="laliga-ea-sports",
            canonical_name="Jrn Team A",
        )
        t2 = TeamIdentityMap(
            entity_id="test-jrn-team-b",
            competition_id="laliga-ea-sports",
            canonical_name="Jrn Team B",
        )
        session.add_all([t1, t2])
        await session.flush()
        t1_id, t2_id = t1.id, t2.id

        j = Jornada(
            temporada=temporada,
            temporada_str="26/27",
            numero=numero,
            status=JornadaStatus.draft,
        )
        session.add(j)
        await session.flush()
        jid = j.id

        for pos in range(1, PARTIDOS_PER_JORNADA + 1):
            session.add(
                Composicion(
                    jornada_id=jid,
                    posicion=pos,
                    local_team_id=t1_id,
                    visitante_team_id=t2_id,
                    local_raw="Jrn Team A",
                    visitante_raw="Jrn Team B",
                ),
            )
        await session.commit()
        return jid


def _make_kickoffs(
    *,
    first: datetime,
    interval_minutes: int = 60,
) -> list[datetime]:
    """Generate 15 tz-aware kickoffs spaced ``interval_minutes`` apart."""
    return [
        first + timedelta(minutes=i * interval_minutes)
        for i in range(PARTIDOS_PER_JORNADA)
    ]


def _default_body() -> dict:
    """Return a valid body dict (15 kickoffs + sale_opens_at), tz-aware."""
    first = datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc)
    sale = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)
    return {
        "kickoff_per_partido": [
            k.isoformat().replace("+00:00", "Z") for k in _make_kickoffs(first=first)
        ],
        "sale_opens_at": sale.isoformat().replace("+00:00", "Z"),
    }


# ==========================================================================
# Happy path.
# ==========================================================================


async def test_admin_can_set_dates(client):
    """Happy path: 15 kickoffs + sale_opens_at → 200, status active."""
    admin_token, _aid, _gid = await _make_admin(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["id"] == jid
    assert resp["status"] == "active"
    assert resp["numero"] == 8
    assert resp["temporada"] == 2026
    assert resp["temporada_str"] == "26/27"
    # sale_opens_at and voting_closes_at round-trip as ISO 8601 UTC.
    assert resp["sale_opens_at"] == body["sale_opens_at"]
    # voting_closes_at = kickoff_last - 1h. First kickoff is 15:00, last
    # is 14h after the first (interval 60min × 14 = 14h) → 2026-10-12 05:00.
    # voting_closes_at = 04:00 UTC.
    assert resp["voting_closes_at"] == "2026-10-12T04:00:00Z"
    assert resp["kickoff_first_match_at"] == body["kickoff_per_partido"][0]
    assert resp["fetched_at"] is not None


# ==========================================================================
# Auth.
# ==========================================================================


async def test_unauthenticated_gets_401(client):
    """No Bearer token → 401."""
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()

    r = await client.post(f"/admin/jornadas/{jid}/dates", json=body)
    assert r.status_code == 401, r.text


async def test_non_admin_gets_403(client):
    """User who is admin of zero groups → 403."""
    loner_token, _ = await _make_loner(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(loner_token),
    )
    assert r.status_code == 403, r.text
    assert "system admin" in r.json()["detail"]


# ==========================================================================
# 404 / 422.
# ==========================================================================


async def test_missing_jornada_gets_404(client):
    """A jornada id with no DB row → 404."""
    admin_token, _aid, _gid = await _make_admin(client)
    body = _default_body()

    r = await client.post(
        "/admin/jornadas/999999/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 404, r.text


async def test_wrong_kickoff_count_gets_422(client):
    """14 kickoffs (instead of 15) → 422 (Pydantic ``min_length``)."""
    admin_token, _aid, _gid = await _make_admin(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()
    body["kickoff_per_partido"] = body["kickoff_per_partido"][:14]

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 422, r.text


async def test_too_many_kickoffs_gets_422(client):
    """16 kickoffs → 422 (Pydantic ``max_length``)."""
    admin_token, _aid, _gid = await _make_admin(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()
    body["kickoff_per_partido"] = body["kickoff_per_partido"] + [
            body["kickoff_per_partido"][-1],
        ]

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 422, r.text


async def test_naive_datetime_gets_422(client):
    """A single naive kickoff → 422 (manager raises ValueError)."""
    admin_token, _aid, _gid = await _make_admin(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()
    # Replace the last kickoff with a naive ISO string (no trailing Z).
    body["kickoff_per_partido"][-1] = "2026-10-12T05:00:00"

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 422, r.text
    # The manager's message mentions tz-awareness; the client gets it
    # surfaced as detail.
    assert "timezone" in r.json()["detail"] or "tz" in r.json()["detail"]


async def test_naive_sale_opens_at_gets_422(client):
    """Naive sale_opens_at → 422 (manager raises ValueError)."""
    admin_token, _aid, _gid = await _make_admin(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()
    body["sale_opens_at"] = "2026-10-08T19:00:00"  # naive

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 422, r.text


# ==========================================================================
# Persistence.
# ==========================================================================


async def test_dates_persist_correctly(client):
    """After the call, the 15 compos kickoffs + the 4 window fields live on the rows."""
    admin_token, _aid, _gid = await _make_admin(client)
    jid = await _make_draft_jornada(numero=8)
    body = _default_body()

    r = await client.post(
        f"/admin/jornadas/{jid}/dates",
        json=body,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 200, r.text

    # Compos rows: every kickoff_at matches what we sent.
    async with AsyncSessionLocal() as session:
        compos = (
            await session.execute(
                select(Composicion)
                .where(Composicion.jornada_id == jid)
                .order_by(Composicion.posicion)
            )
        ).scalars().all()
    assert len(compos) == PARTIDOS_PER_JORNADA
    sent_kickoffs = [
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        for s in body["kickoff_per_partido"]
    ]
    for c, k in zip(compos, sent_kickoffs, strict=True):
        assert c.kickoff_at is not None
        assert c.kickoff_at == k
        # tz-aware column — round-trip must keep the tz.
        assert c.kickoff_at.tzinfo is not None

    # Jornada window fields persisted.
    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j is not None
        assert j.status == JornadaStatus.active
        assert j.sale_opens_at == datetime.fromisoformat(
            body["sale_opens_at"].replace("Z", "+00:00"),
        )
        assert j.kickoff_first_match_at == sent_kickoffs[0]
        assert j.kickoff_last_match_at == sent_kickoffs[-1]
        assert j.voting_closes_at == sent_kickoffs[-1] - timedelta(hours=1)
        # Slice 8 will surface this in the response; for now we just check
        # it's persisted.
        assert (
            j.escrutinio_expected_at
            == sent_kickoffs[-1] + timedelta(hours=2, minutes=30)
        )


# ==========================================================================
# PATCH /admin/jornadas/{id}/default-columnas — multi-column sprint (oct 2026)
# ==========================================================================


async def _promote_to_superadmin(user_id: int) -> None:
    """Flip ``is_superadmin`` on a user via direct SQL (no API endpoint).

    The superadmin-panel sprint seeds the dev user via the migration, but
    tests don't have a convenient "make me a superadmin" flow — the
    only public toggle is ``PATCH /admin/users/{id}``, which itself
    requires the caller to already BE a superadmin. Direct SQL keeps
    the test self-contained.
    """
    async with engine.begin() as conn:
        await conn.execute(
            sql_text(
                "UPDATE \"user\" SET is_superadmin = true WHERE id = :uid"
            ),
            {"uid": user_id},
        )


async def test_default_columnas_set_and_clear(client):
    """PATCH default-columnas → stored on the jornada; PATCH null clears it.

    Validates the new superadmin endpoint ``PATCH
    /api/v1/admin/jornadas/{id}/default-columnas`` end to end:

    - happy path: body ``{"default_columnas": 4}`` → response carries
      ``default_columnas=4`` AND the row in the DB is updated.
    - clearing: body ``{"default_columnas": null}`` (Pydantic sees it as
      ``None`` with ``exclude_unset=True`` distinguishing "absent" from
      "present as null"; the endpoint stamps the row to NULL).
    """
    seq = next(_seq)
    admin_token, admin_id = await _register(
        client, f"adm_jrn_su_{seq}@example.com", "Super",
    )
    await _promote_to_superadmin(admin_id)

    jid = await _make_draft_jornada(numero=8)

    # 1) Set to 4. The route prefix is the superadmin panel's
    # ``/api/v1/admin/jornadas/...`` (NOT the legacy ``/admin/jornadas/...``
    # which is the looser-guarded ``/dates`` endpoint).
    r = await client.patch(
        f"/api/v1/admin/jornadas/{jid}/default-columnas",
        json={"default_columnas": 4},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["default_columnas"] == 4

    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j is not None
        assert j.default_columnas == 4

    # 2) Out of range (>12) → 422.
    r = await client.patch(
        f"/api/v1/admin/jornadas/{jid}/default-columnas",
        json={"default_columnas": 99},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 422, r.text

    # 3) Out of range (<2) → 422.
    r = await client.patch(
        f"/api/v1/admin/jornadas/{jid}/default-columnas",
        json={"default_columnas": 1},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 422, r.text

    # 4) Clear to null.
    r = await client.patch(
        f"/api/v1/admin/jornadas/{jid}/default-columnas",
        json={"default_columnas": None},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["default_columnas"] is None

    async with AsyncSessionLocal() as session:
        j = await session.get(Jornada, jid)
        assert j is not None
        assert j.default_columnas is None


async def test_default_columnas_rejects_non_superadmin(client):
    """Group-admin user (NOT superadmin) → 403 on the PATCH endpoint.

    The endpoint uses the strict ``require_superadmin`` guard. A user
    who is admin of a group but lacks the system-wide flag must NOT be
    able to set jornada-level defaults (those affect every peña that
    creates a porra against the jornada).
    """
    seq = next(_seq)
    admin_token, _admin_id = await _register(
        client, f"adm_jrn_pa_{seq}@example.com", "Plain Admin",
    )
    # No superadmin promotion — this user is admin of zero groups, hence
    # blocked by both gates (lack of group admin is enough to 403 the
    # group route, but we explicitly want to test the SUPERADMIN guard).
    # Make them admin of a group so they pass the looser guard.
    r = await client.post(
        "/groups", json={"name": f"JrnAdmSeq-{seq}"},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 201, r.text

    jid = await _make_draft_jornada(numero=8)

    r = await client.patch(
        f"/api/v1/admin/jornadas/{jid}/default-columnas",
        json={"default_columnas": 4},
        headers=_bearer(admin_token),
    )
    assert r.status_code == 403, r.text
    assert "superadmin" in r.json()["detail"].lower()