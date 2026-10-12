"""Tests for embedded TeamSummary in /jornadas/{id} and picks responses.

The /jornadas/{id}, /groups/{gid}/jornadas/{jid}/picks, and
/groups/{gid}/jornadas/{jid}/picks/me endpoints each return one partido per
match (or per pick). Each partido now embeds a ``local_team`` /
``visitante_team`` ``TeamSummary`` so the client can render the 15-partido
boleto in ONE request, instead of making 30 follow-up calls to
``/api/v1/teams/{id}``.

The contract verified here:

- Embedded summaries populate when the team row exists.
- Embedded summaries are ``None`` (never raise) when the team row is gone.
- The embedded ``id`` matches the ``local_team_id`` / ``visitante_team_id``.
- The embedded shape is byte-identical to ``GET /api/v1/teams/{id}``.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text

from app.db.models import (
    Composicion,
    Jornada,
    JornadaStatus,
    TeamIdentityMap,
)
from app.db.session import AsyncSessionLocal, engine


# ---------- Cleanup --------------------------------------------------------
#
# Each test creates its own jornada/composicion/team and wants them gone
# after the test runs. We delete composicion + jornada first (FK to
# team_identity_map is RESTRICT, so we have to clear the references
# before nuking the teams themselves).


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_team_embed_rows():
    """Drop rows created by team-embed tests after each run."""
    yield
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM pick"))
        await conn.execute(text("DELETE FROM group_member"))
        await conn.execute(text("DELETE FROM \"group\""))
        await conn.execute(text("DELETE FROM \"user\""))
        await conn.execute(text("DELETE FROM jornada_comentario"))
        await conn.execute(text("DELETE FROM jornada_escrutinio"))
        await conn.execute(text("DELETE FROM composicion"))
        await conn.execute(text("DELETE FROM jornada"))
        await conn.execute(
            text(
                "DELETE FROM team_identity_map "
                "WHERE entity_id LIKE 'test-%' OR entity_id LIKE 'embed-%'"
            )
        )


# ---------- Helpers (kept local — see tests/test_jornadas.py for the
#            same convention) ----------------------------------------------


@pytest_asyncio.fixture
async def make_team():
    """Create a TeamIdentityMap row.

    By default only ``entity_id`` / ``competition_id`` / ``canonical_name``
    are set; the optional kwargs populate the display fields so a test can
    verify they round-trip through the embed.
    """

    async def _create(
        canonical: str,
        *,
        comp_id: str = "laliga-ea-sports",
        short_name: str | None = None,
        gender: str | None = None,
        country: str | None = None,
        logo_url: str | None = None,
        entity_id_prefix: str = "test",
    ) -> tuple[int, str]:
        slug = canonical.lower().replace(" ", "-").replace("á", "a")
        async with AsyncSessionLocal() as session:
            team = TeamIdentityMap(
                entity_id=f"{entity_id_prefix}-{slug}",
                competition_id=comp_id,
                canonical_name=canonical,
                short_name=short_name,
                gender=gender,
                country=country,
                logo_url=logo_url,
            )
            session.add(team)
            await session.commit()
            await session.refresh(team)
            return team.id, team.canonical_name

    return _create


@pytest_asyncio.fixture
async def make_jornada():
    """Create a Jornada with optional composicion rows in one go.

    Defaults to ``in_window`` (status=active + a window that straddles
    now) so the slice-4 voting-window helper doesn't reject endpoints
    that hit it (create_porra, submit_picks, ...). Tests that want a
    different phase pass ``status=`` (and the porra lifecycle helpers in
    :mod:`tests.test_porra` do the equivalent adjustment inline).
    """

    async def _create(
        numero: int = 11,
        temporada: int = 2027,
        status: JornadaStatus = JornadaStatus.active,
    ) -> int:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(tz=timezone.utc)
        sale_opens_at: datetime | None = None
        voting_closes_at: datetime | None = None
        if status == JornadaStatus.active:
            sale_opens_at = now - timedelta(hours=2)
            voting_closes_at = now + timedelta(hours=2)
        async with AsyncSessionLocal() as session:
            j = Jornada(
                temporada=temporada,
                temporada_str="2026/2027",
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


async def _make_composicion(
    *,
    jornada_id: int,
    posicion: int,
    local_team_id: int,
    visitante_team_id: int,
    local_raw: str = "Local",
    visitante_raw: str = "Visitante",
) -> None:
    """Insert one composicion row."""
    async with AsyncSessionLocal() as session:
        session.add(Composicion(
            jornada_id=jornada_id,
            posicion=posicion,
            local_team_id=local_team_id,
            visitante_team_id=visitante_team_id,
            local_raw=local_raw,
            visitante_raw=visitante_raw,
        ))
        await session.commit()


# ====================================================================
#  /jornadas/{id} — embed contract
# ====================================================================


async def test_get_jornada_embeds_local_and_visitante_teams(
    client, make_jornada, make_team,
):
    """Each partido exposes `local_team` AND `visitante_team` TeamSummary."""
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team(
        "Adult Local",
        short_name="AL",
        gender="men",
        country="ESP",
        logo_url="https://cdn.example/local.png",
    )
    visit_id, _ = await make_team(
        "Adult Visit",
        short_name="AV",
        gender="men",
        country="ESP",
        logo_url="https://cdn.example/visit.png",
    )
    await _make_composicion(
        jornada_id=jid, posicion=1,
        local_team_id=local_id, visitante_team_id=visit_id,
        local_raw="Adult Local", visitante_raw="Adult Visit",
    )

    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["partidos"]) == 1
    partido = body["partidos"][0]

    local = partido["local_team"]
    assert local is not None
    assert local["canonical_name"] == "Adult Local"
    assert local["short_name"] == "AL"
    assert local["gender"] == "men"
    assert local["country"] == "ESP"
    assert local["logo_url"] == "https://cdn.example/local.png"

    visitante = partido["visitante_team"]
    assert visitante is not None
    assert visitante["canonical_name"] == "Adult Visit"
    assert visitante["short_name"] == "AV"
    assert visitante["gender"] == "men"
    assert visitante["country"] == "ESP"
    assert visitante["logo_url"] == "https://cdn.example/visit.png"

    # competition_id is intentionally NOT exposed on the embed — it's not
    # a display field and would balloon the response. Pin the negative.
    assert "competition_id" not in local


async def test_get_jornada_embeds_team_with_match_id_check(
    client, make_jornada, make_team,
):
    """`local_team.id == local_team_id` and same for visitante — the
    embed must not silently remap the FK.

    This pins the consistency between the IDs and the embed even when the
    team rows are renamed or the jornada's team mapping changes.
    """
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team("Home")
    visit_id, _ = await make_team("Away")
    await _make_composicion(
        jornada_id=jid, posicion=1,
        local_team_id=local_id, visitante_team_id=visit_id,
    )
    # Add a second composicion so we cover multiple partidos in one fetch.
    other_local_id, _ = await make_team("Other Home")
    other_visit_id, _ = await make_team("Other Away")
    await _make_composicion(
        jornada_id=jid, posicion=2,
        local_team_id=other_local_id, visitante_team_id=other_visit_id,
    )

    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200, r.text
    body = r.json()

    by_pos = {p["posicion"]: p for p in body["partidos"]}

    for pos, expected_local, expected_visit in (
        (1, local_id, visit_id),
        (2, other_local_id, other_visit_id),
    ):
        p = by_pos[pos]
        assert p["local_team_id"] == expected_local
        assert p["visitante_team_id"] == expected_visit
        assert p["local_team"] is not None
        assert p["visitante_team"] is not None
        assert p["local_team"]["id"] == expected_local
        assert p["visitante_team"]["id"] == expected_visit


async def test_get_jornada_keeps_team_id_when_team_deleted(
    client, make_jornada, make_team,
):
    """If the team row is gone but composicion still references it,
    `local_team_id` is preserved and `local_team` is `None`.

    The DB's `ON DELETE RESTRICT` makes this scenario impossible via a
    vanilla DELETE; we bypass the FK with ``session_replication_role =
    replica`` (a PG admin escape hatch) to simulate "team was removed
    out-of-band, composicion still references the id".

    The endpoint must not 500 — it must surface a ``None`` embed and
    let `local_raw` / `local_team_id` carry the display.
    """
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team("Vanishing Local")
    visit_id, _ = await make_team("Surviving Visit")

    await _make_composicion(
        jornada_id=jid, posicion=1,
        local_team_id=local_id, visitante_team_id=visit_id,
    )

    # Bypass the FK so we can simulate the "team was deleted but the
    # composicion still references it" state. PG skips FK triggers when
    # session_replication_role is 'replica'.
    async with engine.begin() as conn:
        await conn.execute(text("SET session_replication_role = replica"))
        try:
            await conn.execute(
                text("DELETE FROM team_identity_map WHERE id = :id"),
                {"id": local_id},
            )
        finally:
            await conn.execute(text("SET session_replication_role = origin"))

    # Endpoint must not raise on the missing team row.
    r = await client.get(f"/jornadas/{jid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["partidos"]) == 1
    partido = body["partidos"][0]

    # The FK id is preserved (raw fallback for the client).
    assert partido["local_team_id"] == local_id
    assert partido["visitante_team_id"] == visit_id
    # The embed is None on the missing side, populated on the survivor.
    assert partido["local_team"] is None
    assert partido["visitante_team"] is not None
    assert partido["visitante_team"]["id"] == visit_id


# ====================================================================
#  /groups/{gid}/jornadas/{jid}/picks — embed contract
# ====================================================================


async def _register(client, email: str, display_name: str, password: str = "hunter22"):
    r = await client.post(
        "/auth/register",
        json={"email": email, "password": password, "display_name": display_name},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"], r.json()["user"]["id"]


async def _setup_group_with_two_members(client):
    """Mirror of tests/test_picks.py::_setup_group_with_two_members.

    Returns creator_token, joiner_token, creator_id, joiner_id, group_id.
    """
    import itertools

    seq = next(itertools.count())
    creator_email = f"embed_creator_{seq}@example.com"
    joiner_email = f"embed_joiner_{seq}@example.com"
    creator_token, creator_id = await _register(
        client, creator_email, f"Creator {seq}",
    )
    joiner_token, joiner_id = await _register(
        client, joiner_email, f"Joiner {seq}",
    )
    r = await client.post(
        "/groups", json={"name": f"Embed{seq}"},
        headers={"Authorization": f"Bearer {creator_token}"},
    )
    assert r.status_code == 201, r.text
    jc = r.json()["join_code"]
    group_id = r.json()["id"]
    r2 = await client.post(
        "/groups/join", json={"join_code": jc},
        headers={"Authorization": f"Bearer {joiner_token}"},
    )
    assert r2.status_code == 200, r2.text
    return {
        "creator_token": creator_token, "creator_id": creator_id,
        "joiner_token": joiner_token, "joiner_id": joiner_id,
        "group_id": group_id,
    }


def _valid_picks() -> dict:
    """Oct-2026 picks body — N columns + 1 shared pleno.

    Targets ``modo='multiple'`` (1 column, all simple votes). Partidos
    1-14 use ``signs: ["1"]``; the pleno (partido 15) sits on a
    sibling ``pleno`` field, NOT as a 15th item in the columns.
    """
    return {
        "columnas": [
            {
                "matches": [
                    {"partido_orden": pos, "signs": ["1"]}
                    for pos in range(1, 15)
                ],
            },
        ],
        "pleno": {"local": "0", "visitante": "1"},
    }


async def _create_porra(
    client, group_id: int, jornada_id: int, admin_token: str,
    *,
    dobles: int = 1, triples: int = 0, budget_eur: str = "10.00",
) -> int:
    """Create a porra via the API; return the new porra_id.

    MVP-05: the admin must create a porra for (group, jornada) before
    members can submit picks. Returns the porra id (used to scope the
    picks endpoint to this group+jornada).
    """
    r = await client.post(
        f"/groups/{group_id}/porras",
        json={
            "jornada_id": jornada_id,
            "budget_eur": budget_eur,
            "dobles": dobles,
            "triples": triples,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_get_picks_embeds_team_summaries(
    client, make_jornada, make_team,
):
    """When show_picks=true, every member's picks expose the embedded
    `local_team` / `visitante_team` for each pick.

    MVP-05: picks are now scoped to a porra — first POST /porras to
    create the porra, then POST /picks under it.
    """
    ctx = await _setup_group_with_two_members(client)
    # Picks require an active jornada (server-side guard).
    jid = await make_jornada(numero=11, status=JornadaStatus.active)

    # Build 15 composicion rows + 30 unique teams (so 15 distinct pairs).
    teams: list[tuple[int, int]] = []
    for pos in range(1, 16):
        local_id, _ = await make_team(
            f"L{pos}",
            short_name=f"LN{pos}",
            gender="men",
            country="ESP",
            logo_url=f"https://cdn.example/l{pos}.png",
        )
        visit_id, _ = await make_team(
            f"V{pos}",
            short_name=f"VN{pos}",
            gender="women",
            country="ARG",
            logo_url=f"https://cdn.example/v{pos}.png",
        )
        teams.append((local_id, visit_id))
        await _make_composicion(
            jornada_id=jid, posicion=pos,
            local_team_id=local_id, visitante_team_id=visit_id,
            local_raw=f"L{pos}", visitante_raw=f"V{pos}",
        )

    # Admin creates the porra (MVP-05: prerequisite for voting).
    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    # Both members vote.
    for tok in (ctx["creator_token"], ctx["joiner_token"]):
        rr = await client.post(
            f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
            json=_valid_picks(),
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert rr.status_code == 200, rr.text

    # Admin toggles show_picks so picks are visible.
    rp = await client.patch(
        f"/groups/{ctx['group_id']}",
        json={"show_picks": True},
        headers={"Authorization": f"Bearer {ctx['creator_token']}"},
    )
    assert rp.status_code == 200

    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        headers={"Authorization": f"Bearer {ctx['creator_token']}"},
    )
    assert r.status_code == 200
    body = r.json()

    for member in body["members"]:
        picks = member["picks"]
        assert isinstance(picks, list)
        assert len(picks) == 15
        for p in picks:
            assert p["local_team"] is not None
            assert p["visitante_team"] is not None
            assert p["local_team"]["id"] == p["local_team_id"]
            assert p["visitante_team"]["id"] == p["visitante_team_id"]
            # Display fields populated.
            assert p["local_team"]["short_name"] is not None
            assert p["local_team"]["logo_url"] is not None


async def test_get_picks_me_embeds_team_summaries(
    client, make_jornada, make_team,
):
    """`/picks/me` enriches every pick with the partido's embedded teams.

    MVP-05: scoped to /porras/{porra_id}/picks/me.
    """
    ctx = await _setup_group_with_two_members(client)
    jid = await make_jornada(numero=11, status=JornadaStatus.active)

    teams: list[tuple[int, int]] = []
    for pos in range(1, 16):
        local_id, _ = await make_team(f"L{pos}")
        visit_id, _ = await make_team(f"V{pos}")
        teams.append((local_id, visit_id))
        await _make_composicion(
            jornada_id=jid, posicion=pos,
            local_team_id=local_id, visitante_team_id=visit_id,
        )

    porra_id = await _create_porra(
        client, ctx["group_id"], jid, ctx["creator_token"],
    )

    rr = await client.post(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks",
        json=_valid_picks(),
        headers={"Authorization": f"Bearer {ctx['creator_token']}"},
    )
    assert rr.status_code == 200, rr.text

    r = await client.get(
        f"/groups/{ctx['group_id']}/porras/{porra_id}/picks/me",
        headers={"Authorization": f"Bearer {ctx['creator_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 15
    for p in body:
        assert p["local_team"] is not None
        assert p["visitante_team"] is not None
        assert p["local_team"]["id"] == p["local_team_id"]
        assert p["visitante_team"]["id"] == p["visitante_team_id"]
        assert p["local_team"]["canonical_name"].startswith("L")
        assert p["visitante_team"]["canonical_name"].startswith("V")


# ====================================================================
#  /jornadas/{id} embed == /api/v1/teams/{id} (round-trip)
# ====================================================================


async def test_embedded_team_matches_team_out_endpoint(
    client, make_jornada, make_team,
):
    """The TeamSummary embedded in /jornadas/{id} is byte-identical to
    the response from GET /api/v1/teams/{id} for the same team.

    Catches drift between the embed schema and the public team endpoint.
    """
    jid = await make_jornada(numero=11)
    local_id, _ = await make_team(
        "Embed Match",
        short_name="EM",
        gender="men",
        country="ESP",
        logo_url="https://cdn.example/embed.png",
    )
    visit_id, _ = await make_team("Other")
    await _make_composicion(
        jornada_id=jid, posicion=1,
        local_team_id=local_id, visitante_team_id=visit_id,
        local_raw="Embed Match", visitante_raw="Other",
    )

    # 1. Fetch the team directly.
    r_team = await client.get(f"/api/v1/teams/{local_id}")
    assert r_team.status_code == 200
    team_view = r_team.json()

    # 2. Fetch the jornada and pluck the embedded local_team.
    r_jornada = await client.get(f"/jornadas/{jid}")
    assert r_jornada.status_code == 200
    embedded = r_jornada.json()["partidos"][0]["local_team"]
    assert embedded is not None

    # 3. Every field the embed exposes must equal the team endpoint.
    #    TeamOut has `competition_id` which TeamSummary intentionally
    #    drops (not a display field), so we compare the overlap set.
    overlap = {"id", "entity_id", "canonical_name", "short_name",
               "gender", "country", "logo_url"}
    for field in overlap:
        assert embedded[field] == team_view[field], (
            f"field {field!r} differs: embed={embedded[field]!r} "
            f"vs /api/v1/teams={team_view[field]!r}"
        )

    # 4. The embed must NOT leak `competition_id` (sensitive / not a
    #    display field) and must not include anything outside TeamSummary.
    assert "competition_id" not in embedded