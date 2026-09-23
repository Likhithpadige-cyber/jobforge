"""The persisted submission-dispatch switch (M11 Phase 1, locked decision 5).

Read: owner + reviewer. Write: owner only. Authorization is entirely the
API layer's job (`apps.api.routers.settings`, gated by `require_owner`) -
this module has no notion of a caller's role and never checks one; it only
implements the fail-closed default and the one write path, so there is
exactly one place either can drift.

Absence of the `submission_enabled` row means disabled. This is the same
"absence is the strictest answer" discipline `site_policies` already uses
for an unknown domain (`packages.services.ingestion.policy.blocked_policy`)
- a missing switch must never accidentally read as "on", and a fresh
deployment that has never touched this setting is safe by construction
rather than by remembering to seed a default row.

Named `submission_dispatch_enabled`, not `submission_enabled`, to keep it
unambiguous next to `Settings.submission_enabled`
(`packages.shared.config`) - that property answers a different question
(is `SUBMISSION_MODE=enabled` in the environment?) using the same English
words. `packages.core.services.submission_gate` is the one place that
combines both; nothing else should need to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.models import Setting
from packages.core.repositories import SettingsRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.models import User

#: The one key this module manages. A second persisted switch would get its
#: own constant and its own pair of functions, not a parameter threaded
#: through these two - each switch's fail-closed default is worth stating
#: explicitly at its own call site.
SUBMISSION_ENABLED_KEY = "submission_enabled"


async def submission_dispatch_enabled(session: AsyncSession) -> bool:
    """The persisted half of the two-switch gate. False if the row is absent."""
    row = await SettingsRepository(session).get_by_key(SUBMISSION_ENABLED_KEY)
    if row is None:
        return False
    return bool(row.value.get("enabled", False))


async def set_submission_dispatch_enabled(
    session: AsyncSession, enabled: bool, *, actor: User
) -> bool:
    """Create or update the row. Does not commit - the caller's transaction.

    The caller (the API route) is responsible for having already confirmed
    `actor` is an owner; this function trusts its caller the same way every
    other service function in this codebase trusts a validated `actor`
    rather than re-deriving authorization from first principles.
    """
    repo = SettingsRepository(session)
    row = await repo.get_by_key(SUBMISSION_ENABLED_KEY)
    if row is None:
        row = Setting(
            key=SUBMISSION_ENABLED_KEY,
            value={"enabled": enabled},
            description=(
                "Master switch for real (non-dry-run) application submission. "
                "Both this and SUBMISSION_MODE=enabled are required before any "
                "real external send is possible; neither one alone is enough."
            ),
            updated_by=actor.id,
        )
        session.add(row)
    else:
        row.value = {"enabled": enabled}
        row.updated_by = actor.id
    await session.flush()
    return enabled
