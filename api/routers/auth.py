from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import MembershipsDep, PrincipalDep

router = APIRouter(prefix="/auth", tags=["auth"])


class OrganizationMembershipOut(BaseModel):
    organization_id: str
    role: str


class MeOut(BaseModel):
    user_id: str
    email: str
    organizations: list[OrganizationMembershipOut]


@router.get("/me", response_model=MeOut)
async def me(principal: PrincipalDep, memberships: MembershipsDep) -> MeOut:
    return MeOut(
        user_id=str(principal.user_id),
        email=principal.email,
        organizations=[
            OrganizationMembershipOut(
                organization_id=str(m.organization_id), role=m.role.value
            )
            for m in memberships
        ],
    )
