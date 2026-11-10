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

from typing import Literal, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from apps.api.deps import (
    AppSettings,
    CsrfProtected,
    DbSession,
    RequireOwner,
    authenticated_api,
    require_owner,
)
from apps.api.schemas.review_status import SubmissionGate
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


@router.get("/settings/submission-gate", response_model=SubmissionGate)
async def get_submission_gate(session: DbSession, settings: AppSettings) -> SubmissionGate:
    """Both switches, so a person can see that the persisted one alone sends nothing."""
    persisted = await submission_dispatch_enabled(session)
    return SubmissionGate(
        submission_mode=cast(Literal["dry_run", "enabled"], str(settings.submission_mode)),
        persisted_enabled=persisted,
        live=settings.submission_enabled and persisted,
    )


class RuntimeModes(BaseModel):
    """The deployment's safety modes, so the UI can label what is real.

    Mode names only - no keys, hosts or credentials. `live_ai_enabled` is
    true for `live` and `record`, the two modes that call a model provider.
    """

    llm_mode: Literal["replay", "record", "live"]
    live_ai_enabled: bool
    submission_mode: Literal["dry_run", "enabled"]
    email_transport_mode: str


@router.get("/settings/runtime-modes", response_model=RuntimeModes)
async def get_runtime_modes(settings: AppSettings) -> RuntimeModes:
    llm = cast(Literal["replay", "record", "live"], str(settings.llm_mode))
    return RuntimeModes(
        llm_mode=llm,
        live_ai_enabled=llm != "replay",
        submission_mode=cast(Literal["dry_run", "enabled"], str(settings.submission_mode)),
        email_transport_mode=str(settings.email_transport_mode),
    )


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
