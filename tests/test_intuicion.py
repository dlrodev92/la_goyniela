"""Tests for the ``modo = 'intuicion'`` slice-1 sprint.

Covers the new budget-advisor endpoint + the new validation rules on
``POST /porras``. The PATCH and boleto paths share the same helper
(``_porra_columns_and_cost``) and are exercised through the create
response (the read shape carries ``modo``/``fijos``/``columnas``).

Layout (8 tests):

- test_intuicion_options_filtered_by_budget
- test_intuicion_create_requires_fijos_y_columnas
- test_intuicion_create_validates_fijos_menor_columnas
- test_intuicion_create_validates_cost_le_budget
- test_intuicion_create_succeeds_with_valid_input
- test_intuicion_default_modo_is_multiple
- test_intuicion_out_includes_fijos_y_columnas
- test_intuicion_porra_columns_field

All tests share the same fixtures/cleanup as the rest of the suite.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text

from app.db.session import engine


# ---------- Fixtures (local copies of test_porra.py's helpers) ----------


_admin_test_seq = itertools.count()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_intuicion_rows():
    """Drop rows created during each intuicion test.

    We target the same prefix as ``test_porra.py`` so the runs don't
    leak into each other. The conftest's autouse cleanup handles the
    test-owned tables between tests; this fixture is a defensive second
    wipe scoped to this file.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(text(
            "DELETE FROM porra WHERE group_id IN "
            "(SELECT id FROM \"group\" WHERE name LIKE 'Porra%')"
        ))
        await conn.execute(text(
            "DELETE FROM \"group\" WHERE name LIKE 'Porra%'"
        ))
        await conn.execute(text(
            "DELETE FROM \"user\" WHERE email LIKE 'intuicion_%@example.com'"
        ))
        await conn.execute(text(
            "DELETE FROM \"user\" WHERE email LIKE 'porra_%@example.com'"
        ))
        await conn.execute(text("DELETE FROM jornada WHERE temporada = 2099"))


async def _register(client, email: str, display_name: str, password: str = "hunter22"):
    r = await client.post("/auth/register", json={
        "email": email, "password": password, "display_name": display_name,
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"], r.json()["user"]["id"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _setup_group_with_two_members(client) -> dict:
    """Create a group with creator (admin) + 1 joiner (member)."""
    seq = next(_admin_test_seq)
    creator_email = f"intuicion_creator_{seq}@example.com"
    joiner_email = f"intuicion_joiner_{seq}@example.com"
    creator_token, creator_id = await _register(
        client, creator_email, f"Intuicion Creator {seq}",
    )
    joiner_token, joiner_id = await _register(
        client, joiner_email, f"Intuicion Joiner {seq}",
    )
    r = await client.post(
        "/groups", json={"name": f"Porra{seq}"}, headers=_bearer(creator_token),
    )
    assert r.status_code == 201, r.text
    jc = r.json()["join_code"]
    group_id = r.json()["id"]
    r2 = await client.post(
        "/groups/join", json={"join_code": jc}, headers=_bearer(joiner_token),
    )
    assert r2.status_code == 200, r2.text
    return {
        "creator_token": creator_token, "creator_id": creator_id,
        "joiner_token": joiner_token, "joiner_id": joiner_id,
        "group_id": group_id,
    }


async def _make_jornada(numero: int = 11) -> int:
    """Create a fresh in-window jornada and return its id."""
    from datetime import datetime, timedelta, timezone

    from app.db.models import Jornada, JornadaStatus

    now = datetime.now(tz=timezone.utc)
    async with engine.begin() as conn:
        j = Jornada(
            temporada=2099,
            temporada_str="2099/2099",
            numero=numero,
            status=JornadaStatus.active,
            sale_opens_at=now - timedelta(hours=2),
            voting_closes_at=now + timedelta(hours=2),
        )
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            session.add(j)
            await session.commit()
            await session.refresh(j)
            return j.id


# ============================================================
#  Budget-advisor endpoint
# ============================================================


async def test_intuicion_options_filtered_by_budget(client):
    """Budget 1.50€ returns only the 2-col option; budget 5€ returns
    2..6 cols; budget 9€ returns all 11 options.

    The static advisor table prices every option at ``columnas × 0.75€``,
    so a €1.50 budget fits only (2 cols, 13 fijos), a €5 budget fits the
    rows up to 6 cols (€4.50), and a €9 budget fits the full table.
    """
    ctx = await _setup_group_with_two_members(client)

    # Budget €1.50 → only the 2-col row fits.
    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/intuicion-options",
        params={"budget_eur": "1.50"},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(str(body["budget_eur"])) == Decimal("1.50")
    assert len(body["options"]) == 1
    assert body["options"][0]["columnas"] == 2
    assert body["options"][0]["fijos"] == 13
    assert body["options"][0]["variables"] == 1  # 14 - 13 = 1 varying partido

    # Budget €5 → 2, 3, 4, 5, 6 cols (cost €1.50, 2.25, 3.00, 3.75, 4.50).
    # Ordered desc by columnas (best coverage first).
    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/intuicion-options",
        params={"budget_eur": "5.00"},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    cols = [o["columnas"] for o in r.json()["options"]]
    assert cols == [6, 5, 4, 3, 2]

    # Budget €9 → all 11 rows, ordered desc by columnas.
    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/intuicion-options",
        params={"budget_eur": "9.00"},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    cols = [o["columnas"] for o in r.json()["options"]]
    assert cols == [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2]


# ============================================================
#  POST /porras validation for intuicion
# ============================================================


async def test_intuicion_create_requires_fijos_y_columnas(client):
    """``modo='intuicion'`` without ``fijos``/``columnas`` → 422."""
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    # Missing both.
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text

    # Missing fijos only.
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "columnas": 6,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text

    # Missing columnas only.
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "fijos": 10,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text


async def test_intuicion_create_validates_fijos_menor_columnas(client):
    """Field-level bounds: ``fijos > 13`` or ``columnas < 2`` → 422.

    Slice 1 relaxed the ``fijos < columnas`` rule because the budget-advisor
          table pairs high fijos with low columnas (e.g. (2, 13) is a
          legitimate option meaning "13 of 14 partidos locked, only 1
          varies"). Field-level constraints still reject out-of-range
          values.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    # fijos > 13 → 422 (Pydantic field max=13).
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "fijos": 14, "columnas": 6,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text

    # columnas < 2 → 422 (Pydantic field min=2).
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "fijos": 0, "columnas": 1,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text


async def test_intuicion_create_validates_cost_le_budget(client):
    """``columnas=12`` and ``budget=8`` with precio=0.75 → 422
    (12 × 0.75€ = 9€, exceeds €8).

    Pins ``precio_apuesta_eur=0.75`` so the cost math is deterministic
    — the default 0.50€ (Losilla fallback) would fit the budget and the
    422 wouldn't fire.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "8.00",
            "modo": "intuicion",
            "fijos": 7, "columnas": 12,
            "precio_apuesta_eur": "0.75",
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text
    # The error message names the cost vs budget.
    assert "9" in r.text and "budget" in r.text.lower()


async def test_intuicion_create_succeeds_with_valid_input(client):
    """Valid ``intuicion`` body → 201 with detail + read shape.

    Uses the (columnas=4, fijos=11) row from ``INTUICION_OPTIONS`` —
    fijos < columnas is not enforced (the slice-1 advisor table pairs
    high fijos with low columnas), but field-level constraints cap
    both axes.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "fijos": 2, "columnas": 6,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["modo"] == "intuicion"
    assert body["fijos"] == 2
    assert body["columnas"] == 6
    # 6 cols at default 0.50€/col = €3.00.
    assert body["columns"] == 6
    assert Decimal(str(body["cost_eur"])) == Decimal("3.00")
    # Legacy D/T fields stay at 0 for intuicion.
    assert body["dobles"] == 0
    assert body["triples"] == 0


async def test_intuicion_default_modo_is_multiple(client):
    """POST without ``modo`` defaults to ``multiple`` (legacy)."""
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "dobles": 1, "triples": 0,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["modo"] == "multiple"
    assert body["fijos"] is None
    assert body["columnas"] is None


async def test_intuicion_out_includes_fijos_y_columnas(client):
    """The read response carries the new ``modo``/``fijos``/``columnas``."""
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "fijos": 5, "columnas": 9,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 201, r.text
    porra_id = r.json()["id"]

    # GET the porra to verify the read shape carries the new fields.
    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/{porra_id}",
        headers=_bearer(ctx["joiner_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["modo"] == "intuicion"
    assert body["fijos"] == 5
    assert body["columnas"] == 9


async def test_intuicion_porra_columns_field(client):
    """``columns`` on the read response uses ``porra.columnas`` (9),
    NOT the computed 2^D·3^T (1 for D=T=0)."""
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "modo": "intuicion",
            "fijos": 5, "columnas": 9,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["columns"] == 9
    # D=0, T=0 would give 1 column under the legacy math; the new
    # ``intuicion`` mode bypasses that and uses ``porra.columnas``.
    assert body["columns"] != 1
    # cost = 9 * 0.50 = 4.50€ at default precio.
    assert Decimal(str(body["cost_eur"])) == Decimal("4.50")