"""The real-submission safety gate (M11 Phase 1, locked design).

Real submission requires every one of:

  1. configuration `SUBMISSION_MODE=enabled`   (`Settings.submission_enabled`)
  2. persisted `settings.submission_enabled = true`
                                    (`packages.core.services.settings`)
  3. (M11 Phase 2) an explicit, per-application human `POST /submit`

Phase 1 implements the conjunction of (1) and (2) as the one function below,
specifically so Phase 2 cannot accidentally bypass it: Phase 2 adds
condition 3 by calling `real_submission_enabled` and refusing to proceed
unless it is also true, never by re-deriving (1) or (2) itself. There is
exactly one place either switch is read for this purpose.

Neither switch alone is sufficient by construction: this function is a
plain `and`, not an early return on either half, so there is no order of
operations or short-circuit that could be mistaken for "either is enough".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.services.settings import submission_dispatch_enabled

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.shared.config import Settings


async def real_submission_enabled(session: AsyncSession, settings: Settings) -> bool:
    """True only if both the environment switch and the persisted switch are on.

    `settings.submission_enabled` (`packages.shared.config.Settings`,
    already existed before M11) is the first half, referenced rather than
    re-derived here. This says nothing about whether a *specific
    application* may be submitted - readiness, policy, and (from Phase 2) an
    explicit human Submit action are separate, per-application questions.
    This function answers one question only: is real dispatch even possible
    at all, system-wide, right now.
    """
    db_enabled = await submission_dispatch_enabled(session)
    return settings.submission_enabled and db_enabled
