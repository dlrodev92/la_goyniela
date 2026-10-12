"""Group-related Pydantic DTOs."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GroupCreateIn(BaseModel):
    """POST /groups body."""
    name: str = Field(..., min_length=2, max_length=80)
    description: str | None = Field(None, max_length=2000)


class GroupPatchIn(BaseModel):
    """PATCH /groups/{id} body (admin only, partial update).

    In MVP-02 only ``show_picks`` is editable. All fields are optional so
    a future field (e.g. ``description``) can be added without breaking
    the partial-PATCH contract.
    """
    model_config = ConfigDict(extra="forbid")

    show_picks: bool | None = None


class JoinGroupIn(BaseModel):
    """POST /groups/join body."""
    join_code: str = Field(..., min_length=6, max_length=8)


class MemberOut(BaseModel):
    """One member of a group."""
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    display_name: str
    email: str
    avatar_url: str | None = None
    role: str
    joined_at: datetime


class GroupOut(BaseModel):
    """Group summary (used in list and as base for detail)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    join_code: str
    max_members: int
    show_picks: bool = Field(
        False, description="Whether members can see each other's picks (admin toggle)",
    )
    created_by_user_id: int
    created_at: datetime
    member_count: int = Field(..., description="Current number of active members")
    role: str | None = Field(None, description="Actor's role in this group; null if not a member")

    @classmethod
    def from_group(cls, group: "Group", member_count: int, role: str | None) -> "GroupOut":
        return cls(
            id=group.id,
            name=group.name,
            description=group.description,
            join_code=group.join_code,
            max_members=group.max_members,
            show_picks=group.show_picks,
            created_by_user_id=group.created_by_user_id,
            created_at=group.created_at,
            member_count=member_count,
            role=role,
        )


class GroupDetailOut(GroupOut):
    """Group detail with full member list."""
    members: list[MemberOut]