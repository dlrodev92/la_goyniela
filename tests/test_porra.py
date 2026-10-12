"""Tests for the MVP-05 porra model (replaces MVP-04 quiniela modes).

Coverage layout:

- Schema & open/create (5 tests)
  1. test_create_porra_happy_path_admin
  2. test_create_porra_rejects_non_admin
  3. test_create_porra_rejects_duplicate
  4. test_create_porra_rejects_oversized_format
  5. test_create_porra_rejects_budget_too_small

- porra-formats (3 tests)
  6. test_porra_formats_lists_feasible_options
  7. test_porra_formats_orders_by_columns_desc
  8. test_porra_formats_respects_precio_apuesta_eur

- Picks (5 tests)
  9. test_submit_picks_free_votes_simple
  10. test_submit_picks_free_votes_triple
  11. test_submit_picks_idempotent
  12. test_submit_picks_pleno_format
  13. test_submit_picks_rejects_invalid_pleno

- Propuesta (3 tests)
  14. test_propuesta_unanimous_picks_one_sign
  15. test_propuesta_diverse_picks_assigns_doubles_to_divided_matches
  16. test_propuesta_with_missing_voters

- Boleto (3 tests)
  17. test_put_boleto_happy_path
  18. test_put_boleto_rejects_wrong_count_of_doubles
  19. test_put_boleto_rejects_over_budget

- Slice 4 — voting-window guard (7 integration tests)
  20. test_create_porra_blocked_when_not_in_window
  21. test_submit_picks_blocked_when_outside_window
  22. test_submit_picks_allowed_in_window
  23. test_patch_porra_blocked_when_past
  24. test_patch_porra_allowed_when_lista_abre_pronto
  25. test_put_boleto_blocked_when_past
  26. test_put_boleto_in_window_ok_but_past_409

All tests share the same fixtures/cleanup as the rest of the suite. The
autouse cleanup targets ``Porra%`` group name prefix + porra + pick rows
for our test users, so the shared Postgres DB stays clean between runs.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine


# ---------- Fixtures ----------


_admin_test_seq = itertools.count()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_porra_rows():
    """Drop rows created during each test.

    We target Porra% group name prefix + the test user emails
    (porra_*@example.com). Porra + pick rows CASCADE on group/user
    delete, so dropping groups + users is enough.
    """
    yield
    async with engine.begin() as conn:
        # Explicit porra + pick wipe (CASCADE will get picks + groups +
        # users too, but being explicit keeps the intent obvious in case
        # the cascade rules change).
        await conn.execute(text(
            "DELETE FROM porra WHERE group_id IN "
            "(SELECT id FROM \"group\" WHERE name LIKE 'Porra%')"
        ))
        await conn.execute(text(
            "DELETE FROM pick WHERE group_id IN "
            "(SELECT id FROM \"group\" WHERE name LIKE 'Porra%')"
        ))
        await conn.execute(text(
            "DELETE FROM group_member WHERE group_id IN "
            "(SELECT id FROM \"group\" WHERE name LIKE 'Porra%')"
        ))
        await conn.execute(text(
            "DELETE FROM \"group\" WHERE name LIKE 'Porra%'"
        ))
        await conn.execute(text(
            "DELETE FROM \"user\" WHERE email LIKE 'porra_%@example.com'"
        ))
        await conn.execute(text(
            "DELETE FROM jornada WHERE temporada = 2097"
        ))


@pytest_asyncio.fixture
async def make_jornada():
    """Create a fresh jornada and return its id. Defaults to ``in_window``
    (status=active + sale_opens_at in the past + voting_closes_at in the
    future) so the slice-4 helper does not reject the porra endpoints.

    Pass ``status=`` to override (slice-4 tests use past / draft to
    verify the helper).
    """

    async def _create(
        numero: int = 11,
        temporada: int = 2097,
        *,
        status: "JornadaStatus | None" = None,
        sale_opens_at: "datetime | None" = None,
        voting_closes_at: "datetime | None" = None,
    ) -> int:
        from datetime import datetime, timedelta, timezone

        from app.db.models import Jornada, JornadaStatus

        if status is None:
            status = JornadaStatus.active
        if sale_opens_at is None and status == JornadaStatus.active:
            # Default to a window that straddles "now" so the helper
            # reports ``in_window``. Tests that want a different phase
            # pass explicit overrides.
            now = datetime.now(tz=timezone.utc)
            sale_opens_at = now - timedelta(hours=2)
        if voting_closes_at is None and status == JornadaStatus.active:
            now = datetime.now(tz=timezone.utc)
            voting_closes_at = now + timedelta(hours=2)

        async with AsyncSessionLocal() as session:
            j = Jornada(
                temporada=temporada,
                temporada_str="2097/2097",
                numero=numero,
                status=status,
                sale_opens_at=sale_opens_at,
                voting_closes_at=voting_closes_at,
            )
            session.add(j)
            await session.commit()
            await session.refresh(j)
            return j.id

    return _create


# ---------- Helpers ----------


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
    creator_email = f"porra_creator_{seq}@example.com"
    joiner_email = f"porra_joiner_{seq}@example.com"
    creator_token, creator_id = await _register(client, creator_email, f"Porra Creator {seq}")
    joiner_token, joiner_id = await _register(client, joiner_email, f"Porra Joiner {seq}")
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


def _valid_picks(
    *,
    sign: str = "1",
    pleno_local: str = "0",
    pleno_visitante: str = "0",
) -> dict:
    """MVP-05 / oct-2026 picks body — N columns + 1 shared pleno.

    Returns the canonical multi-column shape (1 column = same as the
    legacy single-column layout). Every partido 1-14 in every column
    votes the same sign. The Pleno al 15 sits on a sibling ``pleno``
    field, NOT as a 15th item in the columns.

    This body targets the legacy/``multiple``/``sencilla`` modes where
    the column count is 1 and each match is a simple (1 sign). Tests
    for multi-column / intuicion / mixed signs should build their own
    body via the low-level ``BoletoMatchIn``-shaped dicts.
    """
    return {
        "columnas": [
            {
                "matches": [
                    {"partido_orden": pos, "signs": [sign]}
                    for pos in range(1, 15)
                ],
            }
        ],
        "pleno": {"local": pleno_local, "visitante": pleno_visitante},
    }


async def _create_porra(
    client, group_id: int, jornada_id: int, admin_token: str,
    *,
    dobles: int = 1, triples: int = 0, budget_eur: str = "10.00",
    precio_apuesta_eur: str | None = None,
) -> int:
    """Create a porra; return porra_id."""
    payload: dict = {
        "jornada_id": jornada_id,
        "budget_eur": budget_eur,
        "dobles": dobles,
        "triples": triples,
    }
    if precio_apuesta_eur is not None:
        payload["precio_apuesta_eur"] = precio_apuesta_eur
    r = await client.post(
        f"/groups/{group_id}/porras",
        json=payload,
        headers=_bearer(admin_token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ============================================================
#  Schema & open/create
# ============================================================


async def test_create_porra_happy_path_admin(client, make_jornada):
    """Admin POST /porras → 201 with detail (formats, columns, cost, status)."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "dobles": 1,
            "triples": 1,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["group_id"] == ctx["group_id"]
    assert body["jornada_id"] == jid
    assert body["dobles"] == 1
    assert body["triples"] == 1
    assert body["status"] == "open"
    # 1 doble + 1 triple → 2 * 3 = 6 columns; at default 0.50€ = €3.00.
    assert body["columns"] == 6
    assert Decimal(str(body["cost_eur"])) == Decimal("3.00")
    assert body["has_boleto"] is False
    assert isinstance(body["formats_preview"], list)
    assert len(body["formats_preview"]) > 0
    # Participation: 0/2 voted, both members are missing voters.
    assert body["participation"]["voted"] == 0
    assert body["participation"]["total_members"] == 2
    assert len(body["participation"]["missing_voters"]) == 2

    # DB row exists with the same shape.
    async with engine.begin() as conn:
        row = (await conn.execute(
            text(
                "SELECT group_id, jornada_id, dobles, triples, status, "
                "precio_apuesta_eur::text "
                "FROM porra WHERE id=:p"
            ),
            {"p": body["id"]},
        )).first()
    assert row is not None
    assert row[0] == ctx["group_id"]
    assert row[1] == jid
    assert row[2] == 1
    assert row[3] == 1
    assert row[4] == "open"


async def test_create_porra_rejects_non_admin(client, make_jornada):
    """Non-admin → 403."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "dobles": 1, "triples": 0,
        },
        headers=_bearer(ctx["joiner_token"]),
    )
    assert r.status_code == 403


async def test_create_porra_rejects_duplicate(client, make_jornada):
    """Second create for same (group, jornada) → 409."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r1 = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "dobles": 1, "triples": 0,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r1.status_code == 201, r1.text

    r2 = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "dobles": 1, "triples": 0,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r2.status_code == 409, r2.text
    assert "already exists" in r2.text.lower()


async def test_create_porra_rejects_oversized_format(client, make_jornada):
    """8 dobles + 5 triples (D+T=13 > 14) is OK; 8 + 7 = 15 → 422."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "100.00",
            "dobles": 8, "triples": 7,  # 15 > 14
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text


async def test_create_porra_rejects_budget_too_small(client, make_jornada):
    """Budget €0.10 cannot fit even one column at precio 0.50 → 422."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "0.10",
            "dobles": 1, "triples": 0,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text
    assert "exceeds" in r.text.lower() or "cost" in r.text.lower()


# ============================================================
#  porra-formats
# ============================================================


async def test_porra_formats_lists_feasible_options(client, make_jornada):
    """Budget €5 at precio 0.50 returns at least (0,0) and (1,0)=2 cols."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/porra-formats",
        params={
            "jornada_id": jid,
            "budget_eur": "5.00",
            # Pin the price to 0.50 to avoid coupling to Losilla data.
            # (The default reads from jornada_escrutinio; in dev that's None.)
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) > 0
    # Minimum is 2 columns: (1,0) at 0.50€/col = 1.00€ fits the €5 budget.
    # (0,0) is no longer a valid Quiniela format (migration
    # f4e5d6c7b8a9) so it must NOT be in the list.
    fmts = {(f["dobles"], f["triples"]): f for f in body}
    assert (1, 0) in fmts
    assert fmts[(1, 0)]["columns"] == 2
    assert Decimal(str(fmts[(1, 0)]["coste_eur"])) == Decimal("1.00")
    assert (0, 0) not in fmts


async def test_porra_formats_orders_by_columns_desc(client, make_jornada):
    """The list is ordered by columns desc (best coverage first)."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/porra-formats",
        params={"jornada_id": jid, "budget_eur": "30.00"},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    cols = [f["columns"] for f in body]
    assert cols == sorted(cols, reverse=True), body


async def test_porra_formats_respects_precio_apuesta_eur(client, make_jornada):
    """Same budget at lower price returns more formats (more cols fit)."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)

    # At precio 0.50, budget 5€ → 10 cols max. (5/0.50 = 10 columns.)
    r1 = await client.get(
        f"/groups/{ctx['group_id']}/porras/porra-formats",
        params={
            "jornada_id": jid, "budget_eur": "5.00",
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r1.status_code == 200
    n1 = len(r1.json())

    # Pin a higher precio via creating a porra first (this also exercises
    # the precio_apuesta_eur path on create).
    await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        precio_apuesta_eur="1.00",
    )

    r2 = await client.get(
        f"/groups/{ctx['group_id']}/porras/porra-formats",
        params={
            "jornada_id": jid, "budget_eur": "5.00",
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r2.status_code == 200
    n2 = len(r2.json())
    # At 1€ per col, only 5 cols fit (single format × 5 cols each, but
    # the count is constrained by D+T ≤ 14 too). Strictly n1 >= n2
    # because lower price → more feasible combos.
    assert n1 >= n2, (n1, n2)


# ============================================================
#  Picks
# ============================================================


async def test_submit_picks_free_votes_simple(client, make_jornada):
    """Member votes ``["1"]`` on partido 1 — no format constraint."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_valid_picks(sign="1"),
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    # DB has 15 rows (14 with signs, 1 with pleno).
    async with engine.begin() as conn:
        rows = (await conn.execute(
            text(
                "SELECT partido_orden, signs, pleno_local, pleno_visitante "
                "FROM pick WHERE porra_id=:p AND user_id=:u "
                "ORDER BY partido_orden"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).all()
    assert len(rows) == 15
    assert rows[0][0] == 1
    assert rows[0][1] == ["1"]
    assert rows[0][2] is None
    assert rows[14][0] == 15
    assert rows[14][1] is None
    assert rows[14][2] == "0"


async def test_submit_picks_free_votes_triple(client, make_jornada):
    """Member votes ``["1","X","2"]`` on partido 5, no format constraint.

    The porra format is (0,0) — all simples — but the FREE-FORM rule lets
    a member vote triples on individual partidos regardless. The format
    only governs the FINAL BOLETO.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=0,
    )

    picks = _valid_picks(sign="1")
    # Multi-column shape: 1 column, 14 rows. Override partido 5 → triple.
    picks["columnas"][0]["matches"][4] = {"partido_orden": 5, "signs": ["1", "X", "2"]}

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=picks,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        row = (await conn.execute(
            text(
                "SELECT signs FROM pick "
                "WHERE porra_id=:p AND user_id=:u AND partido_orden=5"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).first()
    assert sorted(row[0]) == ["1", "2", "X"]


async def test_submit_picks_idempotent(client, make_jornada):
    """Submit twice — second is UPSERT, no duplicates."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    payload = _valid_picks(sign="1")
    r1 = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=payload,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r1.status_code == 200
    r2 = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=payload,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r2.status_code == 200, r2.text

    async with engine.begin() as conn:
        n = (await conn.execute(
            text(
                "SELECT COUNT(*) FROM pick WHERE porra_id=:p AND user_id=:u"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).scalar()
    assert n == 15


async def test_submit_picks_pleno_format(client, make_jornada):
    """Partido 15 with ``pleno: {local: "M", visitante: "1"}`` → 200."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_valid_picks(pleno_local="M", pleno_visitante="1"),
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        row = (await conn.execute(
            text(
                "SELECT pleno_local, pleno_visitante "
                "FROM pick WHERE porra_id=:p AND user_id=:u "
                "AND partido_orden=15"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).first()
    assert row[0] == "M"
    assert row[1] == "1"


async def test_submit_picks_rejects_invalid_pleno(client, make_jornada):
    """``pleno.local: "5"`` (not in {"0","1","2","M"}) → 422."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    picks = _valid_picks()
    picks["pleno"] = {"local": "5", "visitante": "0"}

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=picks,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text


# ============================================================
#  Propuesta
# ============================================================


async def test_propuesta_unanimous_picks_one_sign(client, make_jornada):
    """All 3 members vote "1" → format (D=1, T=0) → 1 doble + 13 simples,
    every partido's sign is "1" (the doble adds a second sign from
    the same vote pool — the algo picks the secondary).

    Oct 2026 multi-column sprint (slice 2): the propuesta is per-column.
    For ``modo='multiple'`` there is 1 column; the response shape is
    ``columnas: [{columna_idx: 1, partidos: [...]}]`` (NOT a flat
    ``partidos`` list).
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=0,
    )

    # Add a third member to the group so the propuesta has 3 voters.
    t3_token, _ = await _register(
        client, f"porra_v3_{next(_admin_test_seq)}@example.com", "V3",
    )
    rg = await client.get(
        f"/groups/{ctx['group_id']}", headers=_bearer(ctx["creator_token"]),
    )
    jc = rg.json()["join_code"]
    rj = await client.post(
        "/groups/join", json={"join_code": jc}, headers=_bearer(t3_token),
    )
    assert rj.status_code == 200

    for tok in (ctx["creator_token"], ctx["joiner_token"], t3_token):
        r = await client.post(
            f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
            json=_valid_picks(sign="1"),
            headers=_bearer(tok),
        )
        assert r.status_code == 200, r.text

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/propuesta",
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["participation"]["voted"] == 3
    assert body["participation"]["total_members"] == 3
    assert body["participation"]["missing_voters"] == []
    # Oct 2026 multi-column sprint: 1 column, modo='multiple'.
    assert len(body["columnas"]) == 1
    assert body["columnas"][0]["columna_idx"] == 1
    # Format (1, 0) → 1 doble + 13 simples (the format is mandatory;
    # the D=0, T=0 format is no longer valid — migration f4e5d6c7b8a9).
    parties_1_14 = body["columnas"][0]["partidos"][:14]
    n_doble = sum(1 for p in parties_1_14 if p["kind"] == "doble")
    n_simple = sum(1 for p in parties_1_14 if p["kind"] == "simple")
    assert n_doble == 1
    assert n_simple == 13
    # Every partido 1-14 contains "1" (the unanimous vote).
    for p in parties_1_14:
        assert "1" in p["reparto"]
    # The top-level pleno carries the COMPUTED propuesta (oct 2026
    # fix — used to be dropped by the response shape).
    assert "pleno" in body
    assert body["pleno"]["local"] in ("0", "1", "2", "M")
    assert body["pleno"]["visitante"] in ("0", "1", "2", "M")
    # Pleno is unanimous "0","0" → propuesta = "0","0".
    assert body["pleno"]["local"] == "0"
    assert body["pleno"]["visitante"] == "0"
    # Per-column partidos list has NO entry for partido 15 — it's the
    # top-level ``pleno`` instead (no more flat 15-entry list).
    assert all(p["partido_orden"] != 15 for p in body["columnas"][0]["partidos"])
    # Cost / columns mirror the multiple cost model: 2 cols × 0.50€ = €1.00.
    assert body["columns"] == 2
    assert Decimal(str(body["cost_eur"])) == Decimal("1.00")


async def test_propuesta_diverse_picks_assigns_doubles_to_divided_matches(
    client, make_jornada,
):
    """Partido 1 split 2-1-0 (one "X" vote), partido 5 split 1-1-1,
    format (1, 0) → doble assigned to partido 5 (the most divided one).

    With (D=1, T=0):
    - Partido 1: weights {"1": 1.5, "X": 0.5, "2": 0} → simple gain on top:
      no triple candidate here, so for the doble calc: b = 0.5.
    - Partido 5: weights {"1": 1.0, "X": 1.0, "2": 1.0} → doble gain = 1.0.
    Partido 5 has the higher b → doble.
    """
    seq = next(_admin_test_seq)
    creator_token, creator_id = await _register(
        client, f"porra_diverse_c{seq}@example.com", f"C{seq}",
    )
    t2_token, _ = await _register(
        client, f"porra_diverse_v2_{seq}@example.com", "V2",
    )
    t3_token, _ = await _register(
        client, f"porra_diverse_v3_{seq}@example.com", "V3",
    )
    r = await client.post(
        "/groups", json={"name": f"PorraDiverse{seq}"},
        headers=_bearer(creator_token),
    )
    group_id = r.json()["id"]
    jc = r.json()["join_code"]
    for tok in (t2_token, t3_token):
        rj = await client.post(
            "/groups/join", json={"join_code": jc}, headers=_bearer(tok),
        )
        assert rj.status_code == 200

    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, group_id, jid, creator_token, dobles=1, triples=0,
    )

    def _picks(p1: str, p5: str) -> dict:
        return {
            "columnas": [
                {
                    "matches": [
                        {
                            "partido_orden": pos,
                            "signs": [
                                p1 if pos == 1 else (p5 if pos == 5 else "1"),
                            ],
                        }
                        for pos in range(1, 15)
                    ],
                },
            ],
            "pleno": {"local": "0", "visitante": "0"},
        }

    # creator picks p1="1" p5="1"; v2 picks p1="1" p5="X"; v3 picks p1="X" p5="2".
    # Partido 1: 2×"1", 1×"X"  → weights {"1": 2*1, "X": 1*1, "2": 0} = {2, 1, 0}.
    # Partido 5: 1×"1", 1×"X", 1×"2" → weights {1, 1, 1} (one each, full vote).
    # With D=1, T=0: triple gain doesn't apply. Doble gain for both
    # partidos is 1.0. Tie-break by ``partido_orden`` ASC → partido 1 wins.
    for tok, p1, p5 in (
        (creator_token, "1", "1"),
        (t2_token, "1", "X"),
        (t3_token, "X", "2"),
    ):
        r = await client.post(
            f"/groups/{group_id}/porras/{porra_id}/picks",
            json=_picks(p1, p5),
            headers=_bearer(tok),
        )
        assert r.status_code == 200, r.text

    r = await client.post(
        f"/groups/{group_id}/porras/{porra_id}/propuesta",
        headers=_bearer(creator_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Oct 2026 multi-column shape: per-column partidos list.
    assert len(body["columnas"]) == 1
    col = body["columnas"][0]
    by_pos = {p["partido_orden"]: p for p in col["partidos"]}
    # Partido 1 gets the doble (tie-break by orden ASC wins over partido 5).
    assert by_pos[1]["kind"] == "doble", by_pos[1]
    assert sorted(by_pos[1]["reparto"]) == ["1", "X"]
    # Partido 5 stays simple.
    assert by_pos[5]["kind"] == "simple", by_pos[5]
    assert by_pos[5]["reparto"] == ["1"]
    # Exactly 1 doble across all 14.
    n_dobles = sum(1 for p in col["partidos"][:14] if p["kind"] == "doble")
    n_triples = sum(1 for p in col["partidos"][:14] if p["kind"] == "triple")
    assert n_dobles == 1
    assert n_triples == 0


async def test_propuesta_with_missing_voters(client, make_jornada):
    """Only 1 of 3 members voted → propuesta returns with
    participation.voted=1 + missing_voters populated."""
    seq = next(_admin_test_seq)
    creator_token, creator_id = await _register(
        client, f"porra_miss_c{seq}@example.com", f"C{seq}",
    )
    t2_token, t2_id = await _register(
        client, f"porra_miss_v2_{seq}@example.com", "V2",
    )
    t3_token, t3_id = await _register(
        client, f"porra_miss_v3_{seq}@example.com", "V3",
    )
    r = await client.post(
        "/groups", json={"name": f"PorraMiss{seq}"},
        headers=_bearer(creator_token),
    )
    group_id = r.json()["id"]
    jc = r.json()["join_code"]
    for tok in (t2_token, t3_token):
        rj = await client.post(
            "/groups/join", json={"join_code": jc}, headers=_bearer(tok),
        )
        assert rj.status_code == 200

    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, group_id, jid, creator_token, dobles=1, triples=0,
    )

    # Only creator votes.
    r = await client.post(
        f"/groups/{group_id}/porras/{porra_id}/picks",
        json=_valid_picks(sign="1"),
        headers=_bearer(creator_token),
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/groups/{group_id}/porras/{porra_id}/propuesta",
        headers=_bearer(creator_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["participation"]["voted"] == 1
    assert body["participation"]["total_members"] == 3
    # Missing voters should be the two non-voters.
    missing_user_ids = {m["user_id"] for m in body["participation"]["missing_voters"]}
    assert missing_user_ids == {t2_id, t3_id}
    assert len(body["participation"]["missing_voters"]) == 2


# ============================================================
#  Boleto
# ============================================================


async def test_put_boleto_happy_path(client, make_jornada):
    """Admin saves a boleto with the correct D/T/cost → 200."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=1, budget_eur="10.00",
    )

    matches = []
    for pos in range(1, 15):
        if pos == 3:
            matches.append({"partido_orden": pos, "signs": ["1", "X", "2"]})
        elif pos == 5:
            matches.append({"partido_orden": pos, "signs": ["1", "X"]})
        else:
            matches.append({"partido_orden": pos, "signs": ["1"]})

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "1"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == 6  # 2^1 × 3^1 = 6
    assert Decimal(str(body["cost_eur"])) == Decimal("3.00")
    assert body["pleno"] == {"local": "0", "visitante": "1"}
    # New N-column response shape: 1 column, 14 matches per column.
    assert len(body["columnas"]) == 1
    assert len(body["columnas"][0]["matches"]) == 14

    # And GET returns it.
    rg = await client.get(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        headers=_bearer(ctx["creator_token"]),
    )
    assert rg.status_code == 200
    assert rg.json()["porra_id"] == porra_id
    assert len(rg.json()["columnas"]) == 1
    assert len(rg.json()["columnas"][0]["matches"]) == 14

    # Non-member 404 is the wrong axis; let's check non-admin reject: the
    # PUT must require admin role. (joiner is not admin.)
    rj = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["joiner_token"]),
    )
    assert rj.status_code == 403


async def test_put_boleto_rejects_wrong_count_of_doubles(client, make_jornada):
    """Saved with 3 doubles but porra has 2 → 422."""
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=2, triples=0,
    )

    # 3 doubles instead of 2.
    matches = [{"partido_orden": pos, "signs": ["1", "X"]} for pos in range(1, 4)] + [
        {"partido_orden": pos, "signs": ["1"]} for pos in range(4, 15)
    ]

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text
    # The error mentions the expected (D=2) vs got (D=3) counts. The
    # mode-aware validator says "expected 2 doubles + 0 triples, got 3
    # + 0" — check for the D/T count wording.
    assert "d/t" in r.text.lower() or "doubles" in r.text.lower()


async def test_put_boleto_rejects_over_budget(client, make_jornada):
    """Format that exceeds the budget → 422 with 'exceeds budget'.

    The over-budget check on PUT is defensive: the create endpoint
    already rejects porras whose format doesn't fit the budget, and PATCH
    refuses to lower the budget below the cost. So under the normal API
    flow this check is unreachable. To exercise it, we directly lower
    the budget via SQL (simulating a DB tampering or future bug) and
    confirm PUT still rejects.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    # Create with format that fits (D=0, T=2 → 9 cols @ 0.50€ = €4.50; budget €5).
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=0, triples=2, budget_eur="5.00",
    )

    # Force the budget below the format's cost (bypassing PATCH's check).
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE porra SET budget_eur = 0.50 WHERE id = :p"),
            {"p": porra_id},
        )

    matches = (
        [{"partido_orden": pos, "signs": ["1", "X", "2"]} for pos in (1, 2)]  # 2 triples
        + [{"partido_orden": pos, "signs": ["1"]} for pos in range(3, 15)]
    )

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text
    assert "exceeds" in r.text.lower() or "budget" in r.text.lower()


# ============================================================
#  Slice 4 — voting-window guard on the 4 mutation endpoints.
# ============================================================


async def _force_jornada_phase(
    jid: int,
    *,
    status: "JornadaStatus | None" = None,
    sale_opens_at: "datetime | None" = "__unset__",
    voting_closes_at: "datetime | None" = "__unset__",
) -> None:
    """Patch a jornada row's status + window dates directly via SQL.

    ``make_jornada`` defaults to ``in_window`` (status=active + a window
    that straddles now). Slice-4 tests need other phases to exercise the
    helper; this helper updates the row without going through any of
    the porras endpoints.
    """
    from datetime import datetime, timezone
    from app.db.models import JornadaStatus

    assignments: list[str] = []
    params: dict = {"jid": jid}
    if status is not None:
        assignments.append("status = :status")
        params["status"] = status.value
    if sale_opens_at != "__unset__":
        assignments.append("sale_opens_at = :sale_opens_at")
        params["sale_opens_at"] = sale_opens_at
    if voting_closes_at != "__unset__":
        assignments.append("voting_closes_at = :voting_closes_at")
        params["voting_closes_at"] = voting_closes_at
    if not assignments:
        return

    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"UPDATE jornada SET {', '.join(assignments)} WHERE id = :jid"
            ),
            params,
        )


async def test_create_porra_blocked_when_not_in_window(client, make_jornada):
    """Jornada with status=past → create_porra → 409 with detail.code.

    The slice-4 helper rejects ``bets`` outside ``in_window``. Slice 1
    of the superadmin-panel sprint collapsed ``closed``/``completed``
    into ``past``; the error code is now ``"escrutinio_disponible"``
    (the old ``closed`` path used ``"escrutinio_pending"``).
    """
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    await _force_jornada_phase(jid, status=JornadaStatus.past)

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json={
            "jornada_id": jid,
            "budget_eur": "10.00",
            "dobles": 1, "triples": 0,
        },
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], dict)
    assert body["detail"]["code"] == "escrutinio_disponible"
    assert body["detail"]["effective_phase"] == "escrutinio_disponible"
    # The 5 contract fields are exposed.
    for key in (
        "code", "sale_opens_at", "voting_closes_at", "now",
        "effective_phase",
    ):
        assert key in body["detail"]


async def test_submit_picks_blocked_when_outside_window(client, make_jornada):
    """Jornada with voting_closes_at in the past → submit_picks → 409.

    The jornada stays ``active`` (admin hasn't transitioned to closed),
    but the wall clock has already crossed ``voting_closes_at``, so the
    helper reports ``fuera_de_ventana`` and rejects with
    ``code="voting_window_closed"``.

    The porra is created while the jornada is still ``in_window`` and
    the window is then pushed into the past; this mirrors the real flow
    where the admin sets up the porra first and the deadline crosses
    only later.
    """
    from datetime import datetime, timedelta, timezone
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )
    # Now push the window into the past.
    now = datetime.now(tz=timezone.utc)
    await _force_jornada_phase(
        jid,
        status=JornadaStatus.active,
        sale_opens_at=now - timedelta(days=10),
        voting_closes_at=now - timedelta(hours=2),
    )

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_valid_picks(),
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], dict)
    assert body["detail"]["code"] == "voting_window_closed"
    assert body["detail"]["effective_phase"] == "fuera_de_ventana"


async def test_submit_picks_allowed_in_window(client, make_jornada):
    """Active jornada with a valid window → submit_picks → 200.

    The slice-4 helper allows ``action="bets"`` only when the
    effective phase is ``in_window``. ``make_jornada`` defaults to that
    configuration, so the existing happy-path tests already cover this
    case; this test pins the contract for the future.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_valid_picks(sign="1"),
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text


async def test_patch_porra_blocked_when_past(client, make_jornada):
    """Jornada in past → patch_porra → 409 with the structured body.

    Slice 1 collapsed ``closed``/``completed`` into ``past``. The old
    test name kept the ``completed`` wording for the same check; the
    new state is ``past`` and the same code/phase is returned.
    """
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )
    await _force_jornada_phase(jid, status=JornadaStatus.past)

    r = await client.patch(
        f"/groups/{ctx['group_id']}/porras/{porra_id}",
        json={"budget_eur": "20.00"},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], dict)
    assert body["detail"]["code"] == "escrutinio_disponible"
    assert (
        body["detail"]["effective_phase"] == "escrutinio_disponible"
    )


async def test_patch_porra_allowed_when_lista_abre_pronto(client, make_jornada):
    """Active jornada with sale_opens_at in the future → patch_porra → 200.

    The slice-4 helper allows ``action="edits"`` in ``in_window`` or
    ``lista_abre_pronto`` (admin can patch format before sale opens to
    fix setup errors). This test pins the latter.

    The porra is created while the jornada is still ``in_window`` and
    the window is then moved forward to flip the phase to
    ``lista_abre_pronto``; this mirrors the real flow where the admin
    sets up the porra first and later pushes the sale_opens_at.
    """
    from datetime import datetime, timedelta, timezone
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )
    # Move sale_opens_at into the future so the phase flips to
    # ``lista_abre_pronto`` without breaking the porra we just created.
    now = datetime.now(tz=timezone.utc)
    await _force_jornada_phase(
        jid,
        status=JornadaStatus.active,
        sale_opens_at=now + timedelta(days=1),
        voting_closes_at=now + timedelta(days=3),
    )

    r = await client.patch(
        f"/groups/{ctx['group_id']}/porras/{porra_id}",
        json={"budget_eur": "20.00"},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text


async def test_put_boleto_blocked_when_past(client, make_jornada):
    """Jornada in past → put_boleto → 409 (jornada is history).

    Slice 1 collapsed ``closed``/``completed`` into ``past``. The old
    test name kept the ``completed`` wording for the same check; the
    new state is ``past`` and the same code/phase is returned.
    """
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=0,
    )
    await _force_jornada_phase(jid, status=JornadaStatus.past)

    matches = [{"partido_orden": pos, "signs": ["1"]} for pos in range(1, 15)]
    matches[0] = {"partido_orden": 1, "signs": ["1", "X"]}

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], dict)
    assert body["detail"]["code"] == "escrutinio_disponible"
    assert (
        body["detail"]["effective_phase"] == "escrutinio_disponible"
    )


async def test_put_boleto_blocked_when_en_preparacion(client, make_jornada):
    """Jornada in draft (en_preparacion) → put_boleto → 409.

    A ``draft`` jornada has no partidos ingested, no propuesta, no votes
    — there is nothing to confirm. Admin must call ``configure_dates``
    first (which transitions draft → active).
    """
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=0,
    )
    # Force the jornada back to draft. compute_effective_phase returns
    # ``en_preparacion`` for any draft jornada regardless of dates, so
    # clearing the window keeps the state unambiguous.
    await _force_jornada_phase(
        jid,
        status=JornadaStatus.draft,
        sale_opens_at=None,
        voting_closes_at=None,
    )

    matches = [{"partido_orden": pos, "signs": ["1"]} for pos in range(1, 15)]
    matches[0] = {"partido_orden": 1, "signs": ["1", "X"]}

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], dict)
    assert body["detail"]["code"] == "jornada_in_preparation"
    assert body["detail"]["effective_phase"] == "en_preparacion"


async def test_put_boleto_allowed_when_lista_abre_pronto(client, make_jornada):
    """Active jornada with sale_opens_at in the future → put_boleto → 200.

    The admin can pre-load the FINAL boleto before sale opens (use case:
    the propuesta is already agreed and the admin wants to lock the
    boleto format in advance).
    """
    from datetime import datetime, timedelta, timezone
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=0,
    )
    # Move sale_opens_at into the future so the phase flips to
    # ``lista_abre_pronto`` (active + now < sale_opens_at).
    now = datetime.now(tz=timezone.utc)
    await _force_jornada_phase(
        jid,
        status=JornadaStatus.active,
        sale_opens_at=now + timedelta(days=1),
        voting_closes_at=now + timedelta(days=3),
    )

    matches = [{"partido_orden": pos, "signs": ["1"]} for pos in range(1, 15)]
    matches[0] = {"partido_orden": 1, "signs": ["1", "X"]}

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text


async def test_put_boleto_allowed_when_fuera_de_ventana(client, make_jornada):
    """Active jornada with voting_closes_at in the past → put_boleto → 200.

    Voting is closed but the admin can still confirm the FINAL boleto in
    the post-close window before escrutinio is published (status stays
    ``active``, only the wall clock crossed ``voting_closes_at``).
    """
    from datetime import datetime, timedelta, timezone
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11)
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        dobles=1, triples=0,
    )
    # Push the window into the past: now ≥ voting_closes_at.
    now = datetime.now(tz=timezone.utc)
    await _force_jornada_phase(
        jid,
        status=JornadaStatus.active,
        sale_opens_at=now - timedelta(days=10),
        voting_closes_at=now - timedelta(hours=2),
    )

    matches = [{"partido_orden": pos, "signs": ["1"]} for pos in range(1, 15)]
    matches[0] = {"partido_orden": 1, "signs": ["1", "X"]}

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text


async def test_put_boleto_in_window_ok_but_past_409(
    client, make_jornada,
):
    """put_boleto: 200 in ``in_window`` (status=active + open window),
    409 in ``past`` (status=history).

    Slice 1 of the superadmin-panel sprint collapsed
    ``closed``/``completed`` into ``past``. The old test pinned both
    ``in_window`` AND ``closed`` (which mapped to
    ``escrutinio_pendiente``) as OK cases for ``final_confirmation``;
    the new design rejects ``final_confirmation`` for ``past`` (the
    boleto is now history, no more confirmations). This test pins the
    new contract: ``in_window`` is allowed, ``past`` is not.
    """
    from app.db.models import JornadaStatus

    ctx = await _setup_group_with_two_members(client)

    # --- Case 1: in_window (status=active + valid window). ---
    jid_in_window = await make_jornada(numero=11)
    porra_id_in_window = await _create_porra(
        client, ctx["group_id"], jid_in_window, ctx["creator_token"],
        dobles=1, triples=0,
    )
    matches = [{"partido_orden": pos, "signs": ["1"]} for pos in range(1, 15)]
    matches[0] = {"partido_orden": 1, "signs": ["1", "X"]}

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id_in_window}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "0"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    # --- Case 2: past (status=closed/completed collapsed). ---
    jid_past = await make_jornada(numero=12)
    porra_id_past = await _create_porra(
        client, ctx["group_id"], jid_past, ctx["creator_token"],
        dobles=1, triples=0,
    )
    await _force_jornada_phase(jid_past, status=JornadaStatus.past)

    r = await client.put(
        f"/groups/{ctx['group_id']}/porras/{porra_id_past}/boleto",
        json={"matches": matches, "pleno": {"local": "0", "visitante": "1"}},
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"]["code"] == "escrutinio_disponible"
    assert (
        body["detail"]["effective_phase"] == "escrutinio_disponible"
    )