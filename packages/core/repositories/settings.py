"""`SettingsRepository` (M11 Phase 1). Generic key/value operational config.

`settings` has no per-feature columns - every row is `{key, value JSONB}`
(DATABASE_DESIGN.md §6, migration 0002) - so a new switch like
`submission_enabled` needs no migration, only a new key. Absence of a row is
not an error; callers decide the fail-closed default per key
(`packages.core.services.settings`).
"""

from __future__ import annotations

from packages.core.models import Setting
from packages.core.repositories.base import BaseRepository


class SettingsRepository(BaseRepository[Setting]):
    """`Setting.key` is a TEXT primary key, not a UUID `id` column like every
    other model `BaseRepository.get()` assumes - so this defines its own
    lookup, `get_by_key`, rather than overriding `get` with an incompatible
    signature."""

    model = Setting

    async def get_by_key(self, key: str) -> Setting | None:
        return await self.session.get(Setting, key)
