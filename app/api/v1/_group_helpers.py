"""Shared helpers for groups router (auth dependencies + code generation + cap)."""
from __future__ import annotations

import secrets
import string

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import GroupMember, User

# Alphabet for join_code: uppercase letters + digits MINUS ambiguous chars
# (0/O, 1/I/l) so a human can read them off a phone screen.
_JOIN_ALPHABET = "".join(c for c in (string.ascii_uppercase + string.digits) if c not in "01OIL")


def generate_join_code() -> str:
    """6-char alphanumeric, no ambiguous chars (0/O/1/I/l)."""
    return "".join(secrets.choice(_JOIN_ALPHABET) for _ in range(6))


async def get_group_member_or_404(
    group_id: int,
    user_id: int,
    session: AsyncSession,
) -> GroupMember:
    """Return the GroupMember row, or 403 if user isn't in the group.

    Uses 403 (Forbidden) rather than 404 because the resource (the group)
    exists; only the caller's membership is missing. A 404 would leak
    "this group_id is valid" to non-members, which is itself a privacy
    boundary in a multi-tenant app. The detail is a generic message with
    no IDs so it can't be used to enumerate user_id or group_id values.
    """
    row = await session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a member of this group",
        )
    return row


async def get_admin_or_403(
    group_id: int,
    current_user: User,
    session: AsyncSession,
) -> GroupMember:
    """Return the actor's GroupMember row, or 403 if they're not admin."""
    membership = await get_group_member_or_404(group_id, current_user.id, session)
    if membership.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return membership