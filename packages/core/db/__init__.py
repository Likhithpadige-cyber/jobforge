# NOTE: `audit_boundary` is deliberately NOT re-exported here. It imports
# `packages.core.models`, and every model module imports `packages.core.db.base`
# - which executes THIS file first. Re-exporting it would mean that importing
# `packages.core.models` runs `db/__init__` runs `audit_boundary` runs
# `from packages.core.models import Event` against a `models/__init__` that is
# still on its first line, and fails. Import it by its own path:
#
#     from packages.core.db.audit_boundary import AuditBoundary, AuditContext
#
# `audit_session` below is safe to re-export: it imports only config and
# logging, so it closes no loop.
from packages.core.db.audit_session import (
    audit_engine_ready,
    audit_session_scope,
    dispose_audit_engine,
    get_audit_sessionmaker,
    init_audit_engine,
)
from packages.core.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from packages.core.db.health import check_database, get_migration_revision
from packages.core.db.session import (
    dispose_engine,
    get_engine,
    get_session,
    get_sessionmaker,
    init_engine,
    session_scope,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "audit_engine_ready",
    "audit_session_scope",
    "check_database",
    "dispose_audit_engine",
    "dispose_engine",
    "get_audit_sessionmaker",
    "get_engine",
    "get_migration_revision",
    "get_session",
    "get_sessionmaker",
    "init_audit_engine",
    "init_engine",
    "session_scope",
]
