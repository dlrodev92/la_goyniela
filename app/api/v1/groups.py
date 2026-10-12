"""Group endpoints: create, list, get, join, leave + admin operations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._group_helpers import (
    generate_join_code,
    get_admin_or_403,
    get_group_member_or_404,
)
from app.api.v1.auth import get_current_user
from app.db.models import Group, GroupMember, User
from app.db.session import get_session
from app.schemas.group import (
    GroupCreateIn,
    GroupDetailOut,
    GroupOut,
    GroupPatchIn,
    JoinGroupIn,
    MemberOut,
)

router = APIRouter(prefix="/groups", tags=["groups"])


# ---------- Helpers (router-scoped) ----------

async def _member_count(session: AsyncSession, group_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
    ) or 0


async def _generate_unique_join_code(session: AsyncSession) -> str:
    """Generate a join_code that doesn't collide with existing rows. Max 5 retries."""
    for _ in range(5):
        code = generate_join_code()
        existing = await session.scalar(select(Group).where(Group.join_code == code))
        if existing is None:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not generate unique join_code after 5 retries",
    )


# ---------- Endpoints ----------

@router.post(
    "",
    response_model=GroupDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new group (the creator becomes admin)",
)
async def create_group(
    body: GroupCreateIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> GroupDetailOut:
    code = await _generate_unique_join_code(session)
    group = Group(
        name=body.name,
        description=body.description,
        created_by_user_id=current_user.id,
        join_code=code,
    )
    session.add(group)
    await session.flush()  # need group.id for the FK

    creator_membership = GroupMember(
        group_id=group.id,
        user_id=current_user.id,
        role="admin",
    )
    session.add(creator_membership)
    await session.commit()
    await session.refresh(group)

    return GroupDetailOut(
        **GroupOut.from_group(group, member_count=1, role="admin").model_dump(),
        members=[
            MemberOut.model_validate({
                "user_id": current_user.id,
                "display_name": current_user.display_name,
                "email": current_user.email,
                "avatar_url": current_user.avatar_url,
                "role": "admin",
                "joined_at": creator_membership.joined_at,
            })
        ],
    )


@router.get(
    "",
    response_model=list[GroupOut],
    summary="List groups the current user belongs to",
)
async def list_my_groups(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[GroupOut]:
    # Get my memberships
    memberships = (
        await session.execute(
            select(GroupMember).where(GroupMember.user_id == current_user.id)
        )
    ).scalars().all()
    if not memberships:
        return []

    # Bulk-load the groups
    group_ids = [m.group_id for m in memberships]
    groups = (
        await session.execute(select(Group).where(Group.id.in_(group_ids)))
    ).scalars().all()
    by_id = {g.id: g for g in groups}

    # Bulk-load member counts
    counts_rows = (
        await session.execute(
            select(GroupMember.group_id, func.count())
            .where(GroupMember.group_id.in_(group_ids))
            .group_by(GroupMember.group_id)
        )
    ).all()
    counts_by_gid = dict(counts_rows)

    out: list[GroupOut] = []
    for m in memberships:
        g = by_id.get(m.group_id)
        if g is None:
            continue
        out.append(GroupOut.from_group(g, member_count=counts_by_gid.get(g.id, 0), role=m.role))
    return out


@router.get(
    "/{group_id}",
    response_model=GroupDetailOut,
    summary="Get one group with full member list (member-only)",
)
async def get_group(
    group_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> GroupDetailOut:
    membership = await get_group_member_or_404(group_id, current_user.id, session)
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")

    members = (
        await session.execute(
            select(GroupMember, User)
            .join(User, User.id == GroupMember.user_id)
            .where(GroupMember.group_id == group_id)
            .order_by(GroupMember.joined_at)
        )
    ).all()

    member_count = await _member_count(session, group_id)
    return GroupDetailOut(
        **GroupOut.from_group(group, member_count=member_count, role=membership.role).model_dump(),
        members=[
            MemberOut.model_validate({
                "user_id": u.id,
                "display_name": u.display_name,
                "email": u.email,
                "avatar_url": u.avatar_url,
                "role": gm.role,
                "joined_at": gm.joined_at,
            })
            for gm, u in members
        ],
    )


@router.post(
    "/join",
    response_model=GroupDetailOut,
    summary="Join a group via its join_code (becomes member)",
    responses={
        400: {"description": "Group is full; detail.code == 'group_full'"},
        404: {"description": "No group with that join_code"},
        409: {"description": "Already a member; detail.code == 'already_member'"},
    },
)
async def join_group(
    body: JoinGroupIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> GroupDetailOut:
    code = body.join_code.strip().upper()
    group = await session.scalar(select(Group).where(Group.join_code == code))
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="join_code not found",
        )

    # Already member?
    existing = await session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group.id,
            GroupMember.user_id == current_user.id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "already_member",
                "message": "you are already a member of this group",
            },
        )

    # Cap check
    count = await _member_count(session, group.id)
    if count >= group.max_members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "group_full",
                "message": f"group full (max_members={group.max_members})",
            },
        )

    membership = GroupMember(
        group_id=group.id,
        user_id=current_user.id,
        role="member",
    )
    session.add(membership)
    await session.commit()
    await session.refresh(membership)

    # Reuse get_group to build the response (DRY)
    return await get_group(group.id, current_user, session)


@router.post(
    "/{group_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave the group (creator cannot leave; must delete instead)",
    responses={
        400: {"description": "Creator cannot leave; must delete group"},
        404: {"description": "Not a member"},
    },
)
async def leave_group(
    group_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    membership = await get_group_member_or_404(group_id, current_user.id, session)
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")

    if group.created_by_user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="creator cannot leave; delete the group instead",
        )

    await session.delete(membership)
    await session.commit()
    return None


# ---------- Admin endpoints ----------

@router.post(
    "/{group_id}/members/{user_id}/promote",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[admin] Promote a member to admin (member → admin)",
    responses={
        403: {"description": "Actor is not admin, or target is already admin, or target is the creator"},
        404: {"description": "Group or target user not found / not a member"},
    },
)
async def promote_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    # Self-modify guard: an admin cannot promote themselves. The creator is
    # already protected (immutable), but a non-creator admin could otherwise
    # re-promote themselves with no effect — rejecting it up front keeps the
    # state-machine explicit on the client side.
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot promote yourself; ask another admin to do it",
        )
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    if group.created_by_user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="creator is already admin (immutable)",
        )
    await get_admin_or_403(group_id, current_user, session)

    target = await get_group_member_or_404(group_id, user_id, session)
    if target.role == "admin":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already admin")
    target.role = "admin"
    await session.commit()
    return None


@router.post(
    "/{group_id}/members/{user_id}/demote",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[admin] Demote an admin to member (admin → member)",
    responses={
        403: {"description": "Actor is not admin, or target is creator (cannot demote)"},
        404: {"description": "Group or target user not found / not a member"},
    },
)
async def demote_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    # Self-modify guard: an admin cannot demote themselves — to leave the
    # group entirely, use POST /groups/{id}/leave. The creator is already
    # protected (immutable) by the check below.
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot demote yourself; use POST /groups/{id}/leave instead",
        )
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    if group.created_by_user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="creator cannot be demoted (immutable)",
        )
    await get_admin_or_403(group_id, current_user, session)

    target = await get_group_member_or_404(group_id, user_id, session)
    if target.role != "admin":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not admin")
    target.role = "member"
    await session.commit()
    return None


@router.delete(
    "/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[admin] Remove a member from the group (creator cannot be removed)",
    responses={
        403: {"description": "Actor is not admin, or target is creator"},
        404: {"description": "Group or target user not found / not a member"},
    },
)
async def remove_member(
    group_id: int,
    user_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    # Self-modify guard: an admin cannot remove themselves — to leave the
    # group entirely, use POST /groups/{id}/leave. The creator is already
    # protected (immutable) by the check below.
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot remove yourself; use POST /groups/{id}/leave instead",
        )
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    if group.created_by_user_id == user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="creator cannot be removed (delete group instead)",
        )
    await get_admin_or_403(group_id, current_user, session)

    target = await get_group_member_or_404(group_id, user_id, session)
    await session.delete(target)
    await session.commit()
    return None


@router.delete(
    "/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="[creator only] Delete a group (cascades memberships)",
    responses={
        403: {"description": "Actor is not the creator"},
        404: {"description": "Group not found"},
    },
)
async def delete_group(
    group_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="group not found")
    if group.created_by_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the creator can delete the group",
        )
    await session.delete(group)  # memberships cascade
    await session.commit()
    return None


@router.patch(
    "/{group_id}",
    response_model=GroupOut,
    summary="[admin] Partial update of group settings (MVP-02: only show_picks)",
    responses={
        403: {"description": "Actor is not admin"},
        404: {"description": "Group not found"},
    },
)
async def patch_group(
    group_id: int,
    body: GroupPatchIn,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    """Partial PATCH: only the fields set in the body are updated.

    Reuses the admin role check from the existing admin endpoints
    (creator + admins may toggle the show_picks flag).
    """
    group = await session.scalar(select(Group).where(Group.id == group_id))
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="group not found",
        )
    await get_admin_or_403(group_id, current_user, session)

    if body.show_picks is not None:
        group.show_picks = body.show_picks

    await session.commit()
    await session.refresh(group)

    role_row = await get_group_member_or_404(group_id, current_user.id, session)
    member_count = await _member_count(session, group_id)
    return GroupOut.from_group(group, member_count=member_count, role=role_row.role)