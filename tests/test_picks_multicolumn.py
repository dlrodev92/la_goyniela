"""Tests for the multi-column pick model, Jornada ``default_columnas``
+ per-column propuesta (POST /propuesta) integration.

Oct 2026 multi-column sprint — slices 1 + 2:

- ``test_picks_intuicion_2_columns_persisted`` — N columns → N×15 rows
  in ``pick`` for the same (user, porra).
- ``test_picks_intuicion_rejects_fijo_mismatch_across_columns`` —
  partido N has different signs in col 1 vs col 2 → 422.
- ``test_picks_intuicion_pleno_shared_across_columns`` — the Pleno al
  15 is stored once per (user, porra, columna_idx), same values across
  columns.
- ``test_picks_multiple_still_1_column`` — legacy 1-column submission
  still works for ``multiple`` mode (regression — picks are free-form,
  D/T is not enforced on submits).
- ``test_picks_sencilla_n_columns`` — submit N columns for a
  ``sencilla`` porra with N=columnas.
- ``test_picks_rejects_wrong_column_count`` — 3 cols against a
  2-column ``intuicion`` porra → 422.
- ``test_jornada_default_columnas_flows_to_porra`` — create a jornada
  with ``default_columnas=4``, POST /porras with ``modo=intuicion`` and
  no ``columnas`` field → porra has ``columnas=4``.
- ``test_propuesta_intuicion_2_columns_per_column_counts`` — POST
  /propuesta returns N columns with per-column counts.
- ``test_propuesta_intuicion_fijos_lock_through_endpoint`` —
  end-to-end check that the ``fijos`` lock produces consistent signs
  across columns in the wire respuesta.

All tests share the same fixtures/cleanup as the rest of the suite.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine


# ---------- Fixtures (local copies of test_intuicion.py's helpers) ----------


_multicol_test_seq = itertools.count()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_multicolumn_rows():
    """Drop rows created during each multi-column test.

    Defensive second wipe: conftest.py's autouse cleanup already trims
    every test-owned table per test, but a second pass here keeps the
    run isolated from neighbour files that may share email/prefix
    patterns.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(text(
            "DELETE FROM porra WHERE group_id IN "
            "(SELECT id FROM \"group\" WHERE name LIKE 'PorraMulti%')"
        ))
        await conn.execute(text(
            "DELETE FROM \"group\" WHERE name LIKE 'PorraMulti%'"
        ))
        await conn.execute(text(
            "DELETE FROM \"user\" WHERE email LIKE 'multicol_%@example.com'"
        ))
        await conn.execute(text("DELETE FROM jornada WHERE temporada = 2098"))


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
    seq = next(_multicol_test_seq)
    creator_email = f"multicol_creator_{seq}@example.com"
    joiner_email = f"multicol_joiner_{seq}@example.com"
    creator_token, creator_id = await _register(
        client, creator_email, f"Multicol Creator {seq}",
    )
    joiner_token, joiner_id = await _register(
        client, joiner_email, f"Multicol Joiner {seq}",
    )
    r = await client.post(
        "/groups", json={"name": f"PorraMulti{seq}"}, headers=_bearer(creator_token),
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


async def _make_jornada(
    numero: int = 11,
    *,
    default_columnas: int | None = None,
) -> int:
    """Create a fresh in-window jornada. Optionally sets ``default_columnas``."""
    from datetime import datetime, timedelta, timezone

    from app.db.models import Jornada, JornadaStatus

    now = datetime.now(tz=timezone.utc)
    async with AsyncSessionLocal() as session:
        j = Jornada(
            temporada=2098,
            temporada_str="2098/2099",
            numero=numero,
            status=JornadaStatus.active,
            sale_opens_at=now - timedelta(hours=2),
            voting_closes_at=now + timedelta(hours=2),
            default_columnas=default_columnas,
        )
        session.add(j)
        await session.commit()
        await session.refresh(j)
        return j.id


async def _create_porra(
    client, group_id: int, jornada_id: int, admin_token: str,
    *,
    modo: str = "intuicion",
    fijos: int = 0,
    columnas: int = 2,
    dobles: int = 1,
    triples: int = 0,
    budget_eur: str = "10.00",
    precio_apuesta_eur: str | None = None,
) -> int:
    """Create a porra via the API; return the new porra_id."""
    payload: dict = {
        "jornada_id": jornada_id,
        "budget_eur": budget_eur,
        "modo": modo,
        "dobles": dobles,
        "triples": triples,
        "fijos": fijos,
        "columnas": columnas,
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


def _intuicion_columnas_body(
    *,
    columnas: int = 2,
    fijos: int = 0,
    partido_signs: dict[int, list[str]] | None = None,
    pleno_local: str = "0",
    pleno_visitante: str = "0",
) -> dict:
    """Build a multi-column ``columnas`` body for an ``intuicion`` porra.

    The fixture partidos 1..14 ALL vote ``["1"]`` by default. Pass
    ``partido_signs={pos: [signs]}`` to override a single partido in
    EVERY column. (Used to set the fijo sign, and to flip a single
    column for the "fijo mismatch" rejection test.)
    """
    overrides = partido_signs or {}
    cols: list[dict] = []
    for _ in range(columnas):
        cols.append({
            "matches": [
                {
                    "partido_orden": pos,
                    "signs": overrides.get(pos, ["1"]),
                }
                for pos in range(1, 15)
            ],
        })
    return {
        "columnas": cols,
        "pleno": {"local": pleno_local, "visitante": pleno_visitante},
    }


def _single_column_body(partido_signs: list[str] | None = None) -> dict:
    """Build a 1-column body (legacy ``matches`` alias), each match = 1 sign."""
    signs = partido_signs or ["1"] * 14
    return {
        "matches": [
            {"partido_orden": pos, "signs": [signs[pos - 1]]}
            for pos in range(1, 15)
        ],
        "pleno": {"local": "0", "visitante": "0"},
    }


# ============================================================
#  Multi-column persistence
# ============================================================


async def test_picks_intuicion_2_columns_persisted(client):
    """Submit 2 columns for a 2-column intuicion porra → 30 rows for (user, porra).

    Each column contributes 14 rows for partidos 1..14 + 1 row for
    partido 15 (the Pleno, shared across columns). For ``columnas=2``,
    ``fijos=0`` that's 2×15 = 30 rows total.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="intuicion", fijos=0, columnas=2,
    )

    body = _intuicion_columnas_body(columnas=2, fijos=0)
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=body,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        n = (await conn.execute(
            text(
                "SELECT COUNT(*) FROM pick "
                "WHERE porra_id=:p AND user_id=:u"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).scalar()
    assert n == 30, f"expected 30 rows (2 cols × 15), got {n}"

    # And: partido 5 should have exactly 2 rows (one per columna_idx).
    async with engine.begin() as conn:
        rows = (await conn.execute(
            text(
                "SELECT columna_idx FROM pick "
                "WHERE porra_id=:p AND user_id=:u AND partido_orden=5 "
                "ORDER BY columna_idx"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).all()
    assert [r[0] for r in rows] == [1, 2]


async def test_picks_intuicion_rejects_fijo_mismatch_across_columns(client):
    """Partido 3 has different signs in col 1 vs col 2 → 422.

    With ``fijos=3`` and ``columnas=2``, the first 3 partidos must
    share the same sign across ALL columns. Setting partido 3 = "1" in
    column 0 and "X" in column 1 trips the check.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="intuicion", fijos=3, columnas=2,
    )

    # Build a 2-column body. Override partido 3 ONLY in column index 1
    # (the second column). col_idx=0 keeps "1", col_idx=1 has "X" →
    # fijo mismatch.
    body = {
        "columnas": [
            {
                "matches": [
                    {
                        "partido_orden": 3 if pos == 3 else pos,
                        "signs": (["X"] if pos == 3 and col_idx == 1 else ["1"]),
                    }
                    for pos in range(1, 15)
                ],
            }
            for col_idx in range(2)
        ],
        "pleno": {"local": "0", "visitante": "0"},
    }

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=body,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text
    assert "fijo" in r.text.lower()
    assert "3" in r.text


async def test_picks_intuicion_pleno_shared_across_columns(client):
    """The Pleno al 15 is stored once per (user, porra, columna_idx), same value.

    For ``columnas=2``, partido 15 has exactly 2 rows — one per
    columna_idx — both with the same ``pleno.local`` /
    ``pleno.visitante``. The Pleno is NOT per-column; it's a SIBLING
    field that the backend writes once per columna_idx.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="intuicion", fijos=0, columnas=2,
    )

    body = _intuicion_columnas_body(
        columnas=2, fijos=0,
        pleno_local="M", pleno_visitante="2",
    )
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=body,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        rows = (await conn.execute(
            text(
                "SELECT columna_idx, pleno_local, pleno_visitante "
                "FROM pick "
                "WHERE porra_id=:p AND user_id=:u AND partido_orden=15 "
                "ORDER BY columna_idx"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).all()
    assert len(rows) == 2
    for col_idx, local, visitante in rows:
        assert col_idx in (1, 2)
        assert local == "M"
        assert visitante == "2"


async def test_picks_multiple_still_1_column(client):
    """Legacy 1-column submission still works for ``multiple`` mode.

    ``modo='multiple'`` requires exactly 1 column. Picks are FREE per
    row (no D/T enforcement at submit time — that's only checked at
    PUT /boleto when the admin saves the FINAL boleto). The legacy
    ``matches`` alias is wrapped as a single column internally.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="multiple", dobles=1, triples=0,
    )

    body = _single_column_body()
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=body,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        n = (await conn.execute(
            text(
                "SELECT COUNT(*) FROM pick "
                "WHERE porra_id=:p AND user_id=:u"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).scalar()
    assert n == 15, f"multiple mode expects 15 rows (1 col), got {n}"


async def test_picks_sencilla_n_columns(client):
    """Submit N columns for a ``sencilla`` porra with N=columnas.

    ``columnas=3`` → 3×15 = 45 rows. Every match in every column has
    exactly 1 sign (Pydantic-side validator + endpoint enforces the
    single sign rule for ``sencilla``).
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="sencilla", fijos=0, columnas=3,
    )

    body = {
        "columnas": [
            {
                "matches": [
                    {"partido_orden": pos, "signs": ["1"]}
                    for pos in range(1, 15)
                ],
            }
            for _ in range(3)
        ],
        "pleno": {"local": "0", "visitante": "0"},
    }
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=body,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        n = (await conn.execute(
            text(
                "SELECT COUNT(*) FROM pick "
                "WHERE porra_id=:p AND user_id=:u"
            ),
            {"p": porra_id, "u": ctx["creator_id"]},
        )).scalar()
    assert n == 45, f"sencilla columnas=3 expects 45 rows, got {n}"


async def test_picks_rejects_wrong_column_count(client):
    """Submit 3 columns for a 2-column ``intuicion`` porra → 422.

    ``modo='intuicion'`` requires exactly ``porra.columnas`` columns.
    The endpoint validates the column count BEFORE any per-match
    validation, so this 422 is deterministic.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="intuicion", fijos=0, columnas=2,
    )

    body = _intuicion_columnas_body(columnas=3, fijos=0)
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=body,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 422, r.text
    assert "requires exactly 2 column" in r.text


# ============================================================
#  Jornada.default_columnas flows into Porra
# ============================================================


async def test_jornada_default_columnas_flows_to_porra(client):
    """Jornada with ``default_columnas=4`` + intuicion + no ``columnas`` body → porra has ``columnas=4``.

    The multi-column default is applied at ``create_porra`` time when
    the admin doesn't supply ``columnas`` explicitly. Path:

    - POST ``/admin/jornadas/{id}/default-columnas`` with 4 → jornada row updated.
    - POST ``/groups/{gid}/porras`` with ``modo=intuicion, fijos=0`` and
      NO ``columnas`` → ``porra.columnas`` becomes 4 (the jornada default).
    """
    ctx = await _setup_group_with_two_members(client)

    # Make a jornada and set default_columnas directly via the DB layer
    # (we test the API for PATCH /admin/jornadas/{id}/default-columnas
    # in test_admin_jornadas.py — here we focus on the create_porra flow
    # that consumes the value).
    jid = await _make_jornada(default_columnas=4)

    # Sanity: the jornada carries the value we set.
    async with AsyncSessionLocal() as session:
        from app.db.models import Jornada
        from sqlalchemy import select
        j = await session.scalar(select(Jornada).where(Jornada.id == jid))
        assert j is not None
        assert j.default_columnas == 4

    # Create an intuicion porra WITHOUT sending ``columnas``. The
    # endpoint should pick up the jornada's default.
    payload = {
        "jornada_id": jid,
        "budget_eur": "10.00",
        "modo": "intuicion",
        "fijos": 0,
        # NOTE: no ``columnas`` key — the default must apply.
    }
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras",
        json=payload,
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["modo"] == "intuicion"
    assert body["columnas"] == 4, (
        f"expected columns=4 (jornada default), got {body['columnas']}"
    )
    assert body["columns"] == 4  # read shape echoes the same value
    # Cost = 4 cols × default 0.50€ = €2.00.
    assert Decimal(str(body["cost_eur"])) == Decimal("2.00")


# ============================================================
#  Per-column propuesta (slice 2 — algorithm)
# ============================================================


async def test_propuesta_intuicion_2_columns_per_column_counts(client):
    """POST /propuesta after multi-column picks returns N columns with
    per-column counts (NOT aggregated across columns).

    Slice 2 (oct 2026): the propuesta algorithm produces one entry per
    column. Two members vote for an ``intuicion`` porra with
    ``columnas=2``; user A votes "1" everywhere, user B votes "X" in
    col 1 and "2" in col 2 for partido 5. The propuesta's partido 5
    count differs across columns.

    Also asserts the response shape:
    - ``modo`` + ``fijos`` pass-through from the porra.
    - ``columnas`` has N entries (one per ``columna_idx``).
    - Each column has 14 rows (partidos 1..14).
    - The top-level ``pleno`` is ONE value (deduplicated per user —
      not N copies).
    - ``columns`` + ``cost_eur`` echo the boleto cost.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="intuicion", fijos=0, columnas=2,
        precio_apuesta_eur="0.50",
    )

    # Creator (user A) votes "1" on all 14 rows of both columns.
    # Joiner (user B) votes "X" on partido 5 in col 1, "2" on partido 5 in col 2.
    def _col_body(p5_sign_col1: str, p5_sign_col2: str) -> dict:
        return {
            "columnas": [
                {
                    "matches": [
                        {
                            "partido_orden": 5 if pos == 5 else pos,
                            "signs": ([p5_sign_col1] if pos == 5 else ["1"]),
                        }
                        for pos in range(1, 15)
                    ],
                },
                {
                    "matches": [
                        {
                            "partido_orden": 5 if pos == 5 else pos,
                            "signs": ([p5_sign_col2] if pos == 5 else ["1"]),
                        }
                        for pos in range(1, 15)
                    ],
                },
            ],
            "pleno": {"local": "0", "visitante": "0"},
        }

    # User A: "1" on everything in both columns.
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_col_body("1", "1"),
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text

    # User B: "X" on partido 5 in col 1, "2" on partido 5 in col 2.
    # Partido 5 in col 1 sees {A:"1", B:"X"} → counts {"1": 1, "X": 1}.
    # Partido 5 in col 2 sees {A:"1", B:"2"} → counts {"1": 1, "2": 1}.
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_col_body("X", "2"),
        headers=_bearer(ctx["joiner_token"]),
    )
    assert r.status_code == 200, r.text

    # POST /propuesta.
    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/propuesta",
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Pass-through fields.
    assert body["porra_id"] == porra_id
    assert body["modo"] == "intuicion"
    assert body["fijos"] == 0

    # Two columns (one per ``columna_idx``).
    assert len(body["columnas"]) == 2
    assert [c["columna_idx"] for c in body["columnas"]] == [1, 2]

    # Each column has 14 rows for partidos 1..14 (no 15th entry — that's
    # on the top-level ``pleno`` field).
    for col in body["columnas"]:
        assert len(col["partidos"]) == 14
        assert [p["partido_orden"] for p in col["partidos"]] == list(range(1, 15))

    # Partido 5 counts are per-column, NOT aggregated. Col 1 sees
    # {"1": 1.0, "X": 1.0, "2": 0.0}; col 2 sees {"1": 1.0, "X": 0.0, "2": 1.0}.
    col1 = body["columnas"][0]
    col2 = body["columnas"][1]
    assert col1["partidos"][4]["vote_counts"] == {
        "1": 1.0, "X": 1.0, "2": 0.0,
    }
    assert col2["partidos"][4]["vote_counts"] == {
        "1": 1.0, "X": 0.0, "2": 1.0,
    }

    # In intuicion mode every match is a ``simple`` (the variation
    # lives across columns, not within rows).
    for col in body["columnas"]:
        assert all(p["kind"] == "simple" for p in col["partidos"])

    # The Pleno is ONE shared value across the response — not N copies.
    assert body["pleno"] == {"local": "0", "visitante": "0"}
    assert all("pleno" not in c for c in body["columnas"])

    # Cost = 2 cols × 0.50€ = €1.00 (intuicion cost model).
    assert body["columns"] == 2
    assert Decimal(str(body["cost_eur"])) == Decimal("1.00")

    # Both members voted.
    assert body["participation"]["voted"] == 2


async def test_propuesta_intuicion_fijos_lock_through_endpoint(client):
    """End-to-end: ``modo='intuicion'`` + ``fijos=2`` → the respuesta
    has 2 columns and the locked parties (1..2) carry the SAME sign
    across both columns.

    A direct algorithmic demonstration of the lock lives in
    ``tests/test_propuesta_per_column.py::test_propuesta_intuicion_fijos_locked_across_columns``
    (where the per-user fixture mismatch rule doesn't constrain the
    test). Here we verify the wire shape: the ``fijos`` field echoes
    the porra, and the locked parties come back consistent.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await _make_jornada()
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
        modo="intuicion", fijos=2, columnas=2,
        precio_apuesta_eur="0.50",
    )

    # Both users submit consistent fijo picks: party 1 and party 2
    # both = "1" in BOTH columns. The PUT /picks validator rejects
    # per-user fijo mismatch (see
    # ``test_picks_intuicion_rejects_fijo_mismatch_across_columns``),
    # so any legitimate submission keeps the locked parties
    # consistent by construction. The algorithm's ``fijos`` lock
    # ensures the PROPUESTA stays consistent even if the votes had
    # weighted differently per column — which we can't trigger
    # through the wire due to the per-user validator.
    picks_body = {
        "columnas": [
            {
                "matches": [
                    {"partido_orden": pos, "signs": ["1"]}
                    for pos in range(1, 15)
                ],
            }
            for _ in range(2)
        ],
        "pleno": {"local": "1", "visitante": "0"},
    }
    for tok in (ctx["creator_token"], ctx["joiner_token"]):
        r = await client.post(
            f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
            json=picks_body,
            headers=_bearer(tok),
        )
        assert r.status_code == 200, r.text

    r = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/propuesta",
        headers=_bearer(ctx["creator_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # 2 columns, 14 rows each.
    assert len(body["columnas"]) == 2
    for col in body["columnas"]:
        assert len(col["partidos"]) == 14

    # Fijos pass-through.
    assert body["modo"] == "intuicion"
    assert body["fijos"] == 2

    # Locked parties (1..2) carry the SAME sign in both columns.
    for pos in range(1, 3):
        s1 = body["columnas"][0]["partidos"][pos - 1]["reparto"]
        s2 = body["columnas"][1]["partidos"][pos - 1]["reparto"]
        assert s1 == s2, (
            f"fijo partido {pos} differs across columns: "
            f"col1={s1!r} col2={s2!r}"
        )
        assert s1 == ["1"], f"expected fijo {pos} = ['1'], got {s1!r}"
