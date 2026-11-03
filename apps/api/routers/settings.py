"""M11 Phase 1: the persisted submission-dispatch switch.

Read: owner + reviewer, the same shared-workspace convention every other
read-only route in this router file uses. Write: owner only, via
`require_owner` - the locked M11 decision, and the same dependency M7 uses
for the workspace's other consequential, non-reviewable actions (importing
the master resume, requesting a first draft). Authentication happens before
any lookup, at the router level (`authenticated_api`), so there is nothing
to enumerate: this route touches exactly one row, identified by a fixed key
never taken from the request.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from apps.api.deps import CsrfProtected, DbSession, RequireOwner, authenticated_api, require_owner
from packages.core.services.settings import (
    set_submission_dispatch_enabled,
    submission_dispatch_enabled,
)

router = APIRouter(tags=["settings"], dependencies=[Depends(authenticated_api)])


class SubmissionEnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("/settings/submission-enabled")
async def get_submission_enabled(session: DbSession) -> dict[str, bool]:
    return {"submission_enabled": await submission_dispatch_enabled(session)}


@router.put("/settings/submission-enabled", dependencies=[Depends(require_owner)])
async def put_submission_enabled(
    payload: SubmissionEnabledInput,
    session: DbSession,
    owner: RequireOwner,
    _csrf: CsrfProtected,
) -> dict[str, bool]:
    enabled = await set_submission_dispatch_enabled(session, payload.enabled, actor=owner)
    await session.commit()
    return {"submission_enabled": enabled}
