"""Tests for the groups router (5 normal + 4 admin endpoints).

Uses the same `client` fixture (in-process FastAPI via ASGITransport).
Each test creates fresh users and groups via API calls (no direct DB writes
except for cleanup), so the test database is shared across tests — helper
functions in this file clean up after each test.

Note on user IDs: previous test runs and seed data leave non-sequential IDs
behind, so tests cannot assume `creator.id == 1`. They fetch IDs from the
register response (which embeds the user in TokenOut) and pass them through.
"""
from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


# Per-process unique counter for admin-test emails. ``id(client)`` is
# not reliable: Python may recycle the same memory address for a new
# AsyncClient fixture instance if the previous one was garbage-collected
# between tests, causing two tests to share an email and the second
# ``_register`` call to fail with 409. This counter increments every
# time ``_setup_group_with_two_members`` is called, so each test gets
# a guaranteed-unique email.
_admin_test_seq = itertools.count()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_test_groups_users():
    """Delete any leftover users from prior test runs before each test.

    The shared Postgres DB accumulates test data across runs. This
    autouse fixture clears the rows that test_groups.py creates so each
    test starts from a clean slate. We use the literal email patterns
    this file is responsible for (creator@example.com, joiner@example.com,
    adm_creator_*, adm_joiner_*, adm_third_*) and any rows they transitively
    reference (group, group_member). The cleanup runs BEFORE the test,
    not after, so a failing test still leaves the DB in a known state.
    """
    from app.db.session import engine
    async with engine.begin() as conn:
        # Delete group_member for groups owned by any of the test users
        # (we may not know which users they are yet, so we delete by
        # group ownership)
        await conn.execute(text(
            "DELETE FROM group_member WHERE group_id IN ("
            "  SELECT id FROM \"group\" WHERE name IN ('Admin', 'Peña 1', 'Test group')"
            ")"
        ))
        await conn.execute(text(
            "DELETE FROM \"group\" WHERE name IN ('Admin', 'Peña 1', 'Test group')"
        ))
        await conn.execute(text(
            "DELETE FROM \"user\" WHERE email LIKE 'creator%@example.com' "
            "OR email LIKE 'joiner%@example.com' "
            "OR email LIKE 'dup@example.com' "
            "OR email LIKE 'g@example.com' "
            "OR email LIKE 'g2@example.com' "
            "OR email LIKE 'adm_%@example.com' "
            "OR email LIKE 'lonely@example.com'"
        ))
    yield


# ---------- DB cleanup helpers ----------

async def _cleanup_user(email: str) -> None:
    """Delete a user and any groups/memberships they own/reference.

    Mirrors the strategy in test_auth.py: select user_id first, then drop
    memberships, drop groups they created (FK on group.created_by_user_id is
    RESTRICT), and finally delete the user. Idempotent — no-op if user is gone.
    """
    from app.db.session import engine
    async with engine.begin() as conn:
        row = (await conn.execute(
            text("SELECT id FROM \"user\" WHERE email=:e"), {"e": email},
        )).first()
        if row is None:
            return
        uid = row[0]
        await conn.execute(text("DELETE FROM group_member WHERE user_id=:u"), {"u": uid})
        await conn.execute(
            text("DELETE FROM \"group\" WHERE created_by_user_id=:u"), {"u": uid},
        )
        await conn.execute(text("DELETE FROM \"user\" WHERE id=:u"), {"u": uid})


async def _cleanup_group(group_id: int) -> None:
    from app.db.session import engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM \"group\" WHERE id=:g"), {"g": group_id})


async def _register(client, email: str, display_name: str, password: str = "hunter22") -> tuple[str, int]:
    """Register a fresh user, return (token, user_id)."""
    r = await client.post("/auth/register", json={
        "email": email, "password": password, "display_name": display_name,
    })
    assert r.status_code == 201, r.text
    return r.json()["access_token"], r.json()["user"]["id"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------- Normal endpoints ----------

async def test_create_group_returns_201_with_join_code(client):
    await _cleanup_user("creator@example.com")
    token, creator_id = await _register(client, "creator@example.com", "Creator")
    r = await client.post("/groups", json={"name": "Peña 1", "description": "Test group"}, headers=_bearer(token))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Peña 1"
    assert body["description"] == "Test group"
    assert len(body["join_code"]) == 6
    assert body["created_by_user_id"] == creator_id
    assert body["role"] == "admin"
    assert body["member_count"] == 1
    assert len(body["members"]) == 1
    assert body["members"][0]["role"] == "admin"
    assert body["members"][0]["user_id"] == creator_id

    await _cleanup_group(body["id"])


async def test_list_my_groups_empty(client):
    await _cleanup_user("lonely@example.com")
    token, _ = await _register(client, "lonely@example.com", "Lonely")
    r = await client.get("/groups", headers=_bearer(token))
    assert r.status_code == 200
    assert r.json() == []


async def test_join_group_by_code(client):
    await _cleanup_user("creator2@example.com")
    await _cleanup_user("joiner@example.com")
    creator_token, _ = await _register(client, "creator2@example.com", "Creator")
    joiner_token, _ = await _register(client, "joiner@example.com", "Joiner")

    r = await client.post("/groups", json={"name": "Peña J"}, headers=_bearer(creator_token))
    assert r.status_code == 201
    join_code = r.json()["join_code"]
    group_id = r.json()["id"]

    r2 = await client.post("/groups/join", json={"join_code": join_code}, headers=_bearer(joiner_token))
    assert r2.status_code == 200, r2.text
    assert r2.json()["role"] == "member"
    assert r2.json()["member_count"] == 2

    await _cleanup_group(group_id)


async def test_join_group_invalid_code_404(client):
    await _cleanup_user("joiner2@example.com")
    token, _ = await _register(client, "joiner2@example.com", "J2")
    r = await client.post("/groups/join", json={"join_code": "NOPE99"}, headers=_bearer(token))
    assert r.status_code == 404


async def test_join_group_already_member_409(client):
    await _cleanup_user("creator3@example.com")
    await _cleanup_user("dup@example.com")
    creator_token, _ = await _register(client, "creator3@example.com", "Creator3")
    dup_token, _ = await _register(client, "dup@example.com", "Dup")
    r = await client.post("/groups", json={"name": "Peña"}, headers=_bearer(creator_token))
    join_code = r.json()["join_code"]
    group_id = r.json()["id"]

    # Creator is already a member
    r2 = await client.post("/groups/join", json={"join_code": join_code}, headers=_bearer(creator_token))
    assert r2.status_code == 409

    await _cleanup_group(group_id)


async def test_get_group_returns_members(client):
    await _cleanup_user("g@example.com")
    await _cleanup_user("g2@example.com")
    t1, _ = await _register(client, "g@example.com", "G1")
    t2, _ = await _register(client, "g2@example.com", "G2")
    r = await client.post("/groups", json={"name": "XGroup"}, headers=_bearer(t1))
    join_code = r.json()["join_code"]
    group_id = r.json()["id"]
    await client.post("/groups/join", json={"join_code": join_code}, headers=_bearer(t2))

    r2 = await client.get(f"/groups/{group_id}", headers=_bearer(t1))
    assert r2.status_code == 200
    body = r2.json()
    assert body["member_count"] == 2
    assert len(body["members"]) == 2

    await _cleanup_group(group_id)


async def test_get_group_non_member_403(client):
    await _cleanup_user("a@example.com")
    await _cleanup_user("b@example.com")
    t1, _ = await _register(client, "a@example.com", "Alice")
    t2, _ = await _register(client, "b@example.com", "Bob")
    r = await client.post("/groups", json={"name": "Private"}, headers=_bearer(t1))
    group_id = r.json()["id"]
    r2 = await client.get(f"/groups/{group_id}", headers=_bearer(t2))
    assert r2.status_code == 403
    assert r2.json()["detail"] == "not a member of this group"  # post-fix: no PII leak
    await _cleanup_group(group_id)


async def test_leave_group(client):
    await _cleanup_user("creator4@example.com")
    await _cleanup_user("leaver@example.com")
    t1, _ = await _register(client, "creator4@example.com", "C4")
    t2, _ = await _register(client, "leaver@example.com", "Leaver")
    r = await client.post("/groups", json={"name": "Leave"}, headers=_bearer(t1))
    jc = r.json()["join_code"]
    group_id = r.json()["id"]
    await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(t2))

    r2 = await client.post(f"/groups/{group_id}/leave", headers=_bearer(t2))
    assert r2.status_code == 204

    await _cleanup_group(group_id)


async def test_creator_cannot_leave_400(client):
    await _cleanup_user("creator5@example.com")
    t, _ = await _register(client, "creator5@example.com", "C5")
    r = await client.post("/groups", json={"name": "Cre"}, headers=_bearer(t))
    group_id = r.json()["id"]
    r2 = await client.post(f"/groups/{group_id}/leave", headers=_bearer(t))
    assert r2.status_code == 400
    await _cleanup_group(group_id)


async def test_unauthenticated_requests_rejected(client):
    r = await client.get("/groups")
    assert r.status_code == 401
    r2 = await client.post("/groups", json={"name": "x"})
    assert r2.status_code == 401


# ---------- Admin endpoints ----------

async def _setup_group_with_two_members(client):
    """Helper: creator + 1 joiner; returns (creator_token, creator_id, joiner_token, joiner_id, group_id, join_code)."""
    seq = next(_admin_test_seq)
    creator_email = f"adm_creator_{seq}@example.com"
    joiner_email = f"adm_joiner_{seq}@example.com"
    creator_token, creator_id = await _register(client, creator_email, "Creator")
    joiner_token, joiner_id = await _register(client, joiner_email, "Joiner")
    r = await client.post("/groups", json={"name": "Admin"}, headers=_bearer(creator_token))
    jc = r.json()["join_code"]
    group_id = r.json()["id"]
    await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(joiner_token))
    return creator_token, creator_id, joiner_token, joiner_id, group_id, jc


async def test_promote_member_to_admin(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)

    r = await client.post(f"/groups/{group_id}/members/{joiner_id}/promote", headers=_bearer(creator_token))
    assert r.status_code == 204, r.text

    # Joiner's role should now be 'admin'
    r2 = await client.get(f"/groups/{group_id}", headers=_bearer(joiner_token))
    assert r2.status_code == 200
    joiner_role = next(m["role"] for m in r2.json()["members"] if m["user_id"] == joiner_id)
    assert joiner_role == "admin"

    await _cleanup_group(group_id)


async def test_non_admin_cannot_promote_403(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)

    # Joiner (non-admin) tries to promote the creator
    r = await client.post(f"/groups/{group_id}/members/{creator_id}/promote", headers=_bearer(joiner_token))
    assert r.status_code == 403
    await _cleanup_group(group_id)


async def test_demote_admin_to_member(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    # Promote joiner first
    await client.post(f"/groups/{group_id}/members/{joiner_id}/promote", headers=_bearer(creator_token))
    # Now demote
    r = await client.post(f"/groups/{group_id}/members/{joiner_id}/demote", headers=_bearer(creator_token))
    assert r.status_code == 204

    r2 = await client.get(f"/groups/{group_id}", headers=_bearer(joiner_token))
    assert next(m["role"] for m in r2.json()["members"] if m["user_id"] == joiner_id) == "member"

    await _cleanup_group(group_id)


async def test_creator_cannot_be_demoted(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    # Promote joiner so they have admin rights
    await client.post(f"/groups/{group_id}/members/{joiner_id}/promote", headers=_bearer(creator_token))

    # Now joiner (also admin) tries to demote the creator
    r = await client.post(f"/groups/{group_id}/members/{creator_id}/demote", headers=_bearer(joiner_token))
    assert r.status_code == 403
    await _cleanup_group(group_id)


async def test_remove_member_from_group(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)

    # Creator removes joiner
    r = await client.delete(f"/groups/{group_id}/members/{joiner_id}", headers=_bearer(creator_token))
    assert r.status_code == 204, r.text

    # Joiner can no longer see the group
    r2 = await client.get(f"/groups/{group_id}", headers=_bearer(joiner_token))
    assert r2.status_code == 403
    await _cleanup_group(group_id)


async def test_non_admin_cannot_remove_403(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, jc = await _setup_group_with_two_members(client)
    # Add a third member (use the same seq counter so the email is unique)
    third_email = f"adm_third_{next(_admin_test_seq)}@example.com"
    third_token, third_id = await _register(client, third_email, "Third")
    await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(third_token))

    # Joiner (non-admin) tries to remove the third member
    r = await client.delete(f"/groups/{group_id}/members/{third_id}", headers=_bearer(joiner_token))
    assert r.status_code == 403
    await _cleanup_group(group_id)


async def test_delete_group_creator_only(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    # Joiner (non-admin in this case since we don't promote) tries to delete
    r = await client.delete(f"/groups/{group_id}", headers=_bearer(joiner_token))
    assert r.status_code == 403

    # Creator succeeds
    r2 = await client.delete(f"/groups/{group_id}", headers=_bearer(creator_token))
    assert r2.status_code == 204


async def test_delete_group_cascades_members(client):
    creator_token, creator_id, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    r = await client.delete(f"/groups/{group_id}", headers=_bearer(creator_token))
    assert r.status_code == 204

    # Group is gone — joiner can no longer find it
    r2 = await client.get(f"/groups/{group_id}", headers=_bearer(joiner_token))
    assert r2.status_code == 403


# ---------- MVP-02 PATCH /groups/{id} (show_picks) ----------


async def test_patch_group_toggles_show_picks(client):
    """Admin PATCH /groups/{id} with show_picks updates the field."""
    creator_token, _id, _jt, _jid, group_id, _ = await _setup_group_with_two_members(client)

    # Default is false
    r0 = await client.get(f"/groups/{group_id}", headers=_bearer(creator_token))
    assert r0.status_code == 200
    assert r0.json()["show_picks"] is False

    # Admin flips it on
    r = await client.patch(
        f"/groups/{group_id}", json={"show_picks": True},
        headers=_bearer(creator_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["show_picks"] is True

    # And back off
    r2 = await client.patch(
        f"/groups/{group_id}", json={"show_picks": False},
        headers=_bearer(creator_token),
    )
    assert r2.status_code == 200
    assert r2.json()["show_picks"] is False


async def test_patch_group_rejects_non_admin(client):
    """Non-admin PATCH /groups/{id} → 403."""
    _ct, _id, joiner_token, _jid, group_id, _ = await _setup_group_with_two_members(client)

    r = await client.patch(
        f"/groups/{group_id}", json={"show_picks": True},
        headers=_bearer(joiner_token),
    )
    assert r.status_code == 403


async def test_patch_group_404_when_missing(client):
    creator_token, _id, _jt, _jid, _gid, _ = await _setup_group_with_two_members(client)
    r = await client.patch(
        "/groups/999999", json={"show_picks": True},
        headers=_bearer(creator_token),
    )
    assert r.status_code == 404


# ---------- Self-modify guards (Fix #4) ----------


async def _promote_to_admin(client, creator_token: str, group_id: int, user_id: int) -> None:
    """Helper: promote ``user_id`` to admin via the creator token.

    Sets up the precondition so the self-modify tests can exercise the
    guard without a second admin doing the dirty work.
    """
    r = await client.post(
        f"/groups/{group_id}/members/{user_id}/promote",
        headers=_bearer(creator_token),
    )
    assert r.status_code == 204, r.text


async def test_admin_cannot_promote_self(client):
    """A non-creator admin cannot promote themselves (would be a no-op anyway,
    but rejecting it explicitly keeps the state-machine clear on the client).

    The creator is already protected via the existing "immutable" check —
    this is the second-tier guard for admins created by promote.
    """
    creator_token, _cid, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    await _promote_to_admin(client, creator_token, group_id, joiner_id)

    # Now joiner (admin) tries to promote themselves
    r = await client.post(
        f"/groups/{group_id}/members/{joiner_id}/promote",
        headers=_bearer(joiner_token),
    )
    assert r.status_code == 400, r.text
    assert "cannot promote yourself" in r.text.lower()


async def test_admin_cannot_demote_self(client):
    """A non-creator admin cannot demote themselves — they should use /leave."""
    creator_token, _cid, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    await _promote_to_admin(client, creator_token, group_id, joiner_id)

    r = await client.post(
        f"/groups/{group_id}/members/{joiner_id}/demote",
        headers=_bearer(joiner_token),
    )
    assert r.status_code == 400, r.text
    assert "cannot demote yourself" in r.text.lower()


async def test_admin_cannot_remove_self(client):
    """A non-creator admin cannot remove themselves — they should use /leave."""
    creator_token, _cid, joiner_token, joiner_id, group_id, _ = await _setup_group_with_two_members(client)
    await _promote_to_admin(client, creator_token, group_id, joiner_id)

    r = await client.delete(
        f"/groups/{group_id}/members/{joiner_id}",
        headers=_bearer(joiner_token),
    )
    assert r.status_code == 400, r.text
    assert "cannot remove yourself" in r.text.lower()


# ---------- Join error codes (Fix #5) ----------


async def test_join_group_full_returns_distinct_code(client):
    """When the cap is reached, /groups/join returns 400 with a machine-parseable
    detail code ``"group_full"`` (was 409 with text-only detail before the fix)."""
    from app.db.session import engine
    await _cleanup_user("cap_creator@example.com")
    await _cleanup_user("cap_filler@example.com")
    await _cleanup_user("cap_outsider@example.com")
    creator_token, _ = await _register(client, "cap_creator@example.com", "CapC")
    filler_token, _ = await _register(client, "cap_filler@example.com", "CapF")
    outsider_token, _ = await _register(client, "cap_outsider@example.com", "CapO")

    r = await client.post(
        "/groups",
        json={"name": "Capped"},
        headers=_bearer(creator_token),
    )
    assert r.status_code == 201, r.text
    jc = r.json()["join_code"]
    group_id = r.json()["id"]

    # ``GroupCreateIn`` doesn't expose ``max_members`` yet (the PATCH endpoint
    # only handles ``show_picks`` in MVP-02), so we shrink the cap via a
    # direct UPDATE to make the group fillable in two joins.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE \"group\" SET max_members = 2 WHERE id = :g"),
            {"g": group_id},
        )

    # Fill to max_members=2: creator (1) + filler (2)
    rj = await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(filler_token))
    assert rj.status_code == 200

    # Outsider tries to join → must be rejected with the group_full code
    r_out = await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(outsider_token))
    assert r_out.status_code == 400, r_out.text
    detail = r_out.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "group_full"
    assert "max_members" in detail["message"]

    await _cleanup_group(group_id)


async def test_join_already_member_returns_distinct_code(client):
    """When the caller is already a member, /groups/join returns 409 with
    ``detail.code == "already_member"`` (text-only detail before the fix)."""
    await _cleanup_user("dup_creator@example.com")
    await _cleanup_user("dup_self@example.com")
    creator_token, _ = await _register(client, "dup_creator@example.com", "DupC")
    self_token, _ = await _register(client, "dup_self@example.com", "DupS")

    r = await client.post("/groups", json={"name": "DupGroup"}, headers=_bearer(creator_token))
    jc = r.json()["join_code"]
    group_id = r.json()["id"]

    # Self joins once → 200
    r1 = await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(self_token))
    assert r1.status_code == 200

    # Self joins twice → 409 + distinct code
    r2 = await client.post("/groups/join", json={"join_code": jc}, headers=_bearer(self_token))
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "already_member"

    await _cleanup_group(group_id)