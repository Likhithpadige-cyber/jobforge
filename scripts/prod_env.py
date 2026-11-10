"""Private runtime helper (M20): generate-env, init, preflight.

    python -m scripts.prod_env generate-env PATH   # host: new env file, mode 600
    python -m scripts.prod_env init                # release job: marker + role passwords
    python -m scripts.prod_env preflight [--bootstrap]   # inside the api image

`generate-env` writes freshly generated secrets to a NEW file outside the
repository and prints only the path and the names it wrote. `init` runs as
the database owner in the one-shot release container. `preflight` runs as
app_api and fails closed: any failed check exits non-zero. No command prints
a secret value, and none deletes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROD_DATABASE = "jobforge_prod"
PROD_MARKER = "jobforge-prod-environment: real personal data; never reset or discard"
RUNTIME_ROLES = ("app_api", "app_agent", "app_monitor", "app_audit")
ROLE_PASSWORD_VARS = {role: f"{role.upper()}_PASSWORD" for role in RUNTIME_ROLES}
ARTIFACT_SAMPLE = 20


class ProdGuardError(RuntimeError):
    """A refusal; the message never contains a secret value."""


# ------------------------------------------------------------ generate-env
def _secret() -> str:
    # URL-safe and free of ':', '@', '/' so it is safe inside a DSN.
    return secrets.token_urlsafe(32)


def generate_env(path: Path) -> list[str]:
    path = path.expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ProdGuardError("the env file must live outside the repository")
    if path.exists():
        raise ProdGuardError("refusing to overwrite an existing env file")
    values = {
        "JOBFORGE_PROD_ENVIRONMENT": "1",
        "ENVIRONMENT": "production",
        "APP_NAME": "JobForge",
        "LOG_FORMAT": "json",
        "LLM_MODE": "replay",
        "ANTHROPIC_API_KEY": "",
        "SUBMISSION_MODE": "dry_run",
        "EMAIL_TRANSPORT_MODE": "fake",
        "POSTGRES_USER": "jobforge_owner",
        "POSTGRES_DB": PROD_DATABASE,
        "POSTGRES_PASSWORD": _secret(),
        **{var: _secret() for var in ROLE_PASSWORD_VARS.values()},
        "REDIS_PASSWORD": _secret(),
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "TOTP_ENCRYPTION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        # JSON: pydantic-settings decodes list settings from the environment as
        # JSON before any validator runs; a bare URL fails startup.
        "CORS_ORIGINS": json.dumps(["http://127.0.0.1:8480"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("# JobForge private runtime. Generated; keep an offline copy. Never commit.\n")
        fh.writelines(f"{k}={v}\n" for k, v in values.items())
    os.chmod(path, 0o600)
    return list(values)


# ----------------------------------------------------------------- checks
@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = True


def config_checks(settings: Any) -> list[Check]:
    """Pure checks on validated Settings (secrets/TOTP already enforced)."""
    from packages.shared.config import Environment, LLMMode, SubmissionMode

    origins_ok = all(
        o.startswith("https://") or o.startswith(("http://127.0.0.1", "http://localhost"))
        for o in settings.cors_origins
    )
    return [
        Check(
            "environment", settings.environment is Environment.PRODUCTION, str(settings.environment)
        ),
        Check(
            "live_ai_disabled",
            settings.llm_mode is LLMMode.REPLAY and not settings.anthropic_api_key,
            f"llm_mode={settings.llm_mode}, provider key "
            f"{'set' if settings.anthropic_api_key else 'absent'}",
        ),
        Check(
            "submission_dry_run",
            settings.submission_mode is SubmissionMode.DRY_RUN,
            str(settings.submission_mode),
        ),
        Check(
            "email_fake",
            str(settings.email_transport_mode) == "fake",
            str(settings.email_transport_mode),
        ),
        Check(
            "secure_cookies_and_totp",
            not settings.is_local and settings.totp_required,
            "Secure cookies and mandatory TOTP follow from a non-local environment",
        ),
        Check(
            "session_secret_strength", len(settings.session_secret) >= 32, "at least 32 characters"
        ),
        Check("cors_origins", origins_ok, "https:// or loopback only"),
        Check(
            "artifact_backend",
            settings.artifact_storage_backend == "filesystem",
            settings.artifact_storage_backend,
        ),
    ]


def artifact_root_checks(root: Path) -> list[Check]:
    if not root.is_dir():
        return [Check("artifact_root", False, "artifact root does not exist")]
    info = root.stat()
    private = stat.S_IMODE(info.st_mode) & 0o077 == 0
    owned = info.st_uid == os.getuid()
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".preflight-", delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
        writable = True
    except OSError:
        writable = False
    return [
        Check(
            "artifact_root_private", private and owned, "owned by this user, no group/other access"
        ),
        Check("artifact_root_writable", writable, "temporary file created and removed"),
    ]


def undecryptable_totp_secrets(blobs: list[bytes], key: str) -> int:
    """How many stored TOTP secrets the configured key cannot decrypt.
    Nothing decrypted is kept, returned or printed."""
    from packages.core.crypto import DecryptionError, decrypt_totp_secret
    from packages.core.crypto.totp_secret import MissingEncryptionKeyError

    bad = 0
    for blob in blobs:
        try:
            decrypt_totp_secret(blob, key)
        except (DecryptionError, MissingEncryptionKeyError):  # no usable key: fail closed
            bad += 1
    return bad


async def database_checks(
    engine: Any,
    *,
    expected_db: str,
    expected_role: str,
    bootstrap: bool,
    totp_key: str = "",
) -> list[Check]:
    from sqlalchemy import text

    from packages.core.db.schema_guard import code_head_revision, database_revision
    from packages.core.services.settings import SUBMISSION_ENABLED_KEY

    out: list[Check] = []
    async with engine.connect() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
        out.append(Check("database_name", name == expected_db, f"{name}"))
        marker = await conn.scalar(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        out.append(Check("database_marker", marker == PROD_MARKER, "set by `init`"))
        user = await conn.scalar(text("SELECT current_user"))
        out.append(Check("least_privilege_role", user == expected_role, f"connected as {user}"))
        rows = (
            await conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "FROM pg_roles WHERE rolname = ANY(:roles)"
                ),
                {"roles": list(RUNTIME_ROLES)},
            )
        ).all()
        found = {r[0] for r in rows}
        unsafe = sorted(r[0] for r in rows if any(r[1:]))
        out.append(
            Check(
                "runtime_roles",
                found == set(RUNTIME_ROLES) and not unsafe,
                f"missing={sorted(set(RUNTIME_ROLES) - found)} privileged={unsafe}",
            )
        )
        dispatch = await conn.scalar(
            text("SELECT value FROM settings WHERE key = :k"), {"k": SUBMISSION_ENABLED_KEY}
        )
        enabled = isinstance(dispatch, dict) and dispatch.get("enabled") is True
        out.append(Check("dispatch_switch_off", not enabled, "persisted submission switch"))
        owners = await conn.scalar(
            text(
                "SELECT count(*) FROM users WHERE role = 'owner' AND password_hash IS NOT NULL "
                "AND totp_secret_enc IS NOT NULL"
            )
        )
        # A restore with a different TOTP_ENCRYPTION_KEY would leave every
        # owner unable to sign in (found in the M21 restore drill).
        blobs = list(
            (
                await conn.execute(
                    text(
                        "SELECT totp_secret_enc FROM users "
                        "WHERE role = 'owner' AND totp_secret_enc IS NOT NULL"
                    )
                )
            ).scalars()
        )
        bad = undecryptable_totp_secrets(blobs, totp_key) if blobs else 0
        out.append(
            Check(
                "owner_totp_decryptable",
                bad == 0,
                f"{len(blobs) - bad}/{len(blobs)} owner TOTP secrets open with the configured key",
            )
        )
        out.append(
            Check(
                "owner_with_totp",
                bool(owners) or bootstrap,
                f"{owners} owner(s) with password and TOTP" + (" (bootstrap)" if bootstrap else ""),
                fatal=not bootstrap,
            )
        )
    revision = await database_revision(engine)
    head = code_head_revision()
    out.append(Check("schema_at_head", revision == head, f"database={revision} code={head}"))
    return out


def unverifiable_objects(rows: Any, root: Path) -> int:
    """How many (storage_key, sha256) rows have no matching object under root.
    A key that resolves outside the root counts as unverifiable, never read."""
    import hashlib

    base = root.resolve()
    bad = 0
    for key, digest in rows:
        path = (root / key).resolve()
        inside = path.is_relative_to(base) and path.is_file()
        if not inside or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            bad += 1
    return bad


async def artifact_integrity_checks(engine: Any, root: Path) -> list[Check]:
    """Sampled: the newest artifacts' objects exist and match their checksums."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT storage_key, sha256 FROM artifacts WHERE sha256 IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT :n"
                ),
                {"n": ARTIFACT_SAMPLE},
            )
        ).all()
    bad = unverifiable_objects(rows, root)
    return [
        Check("artifact_integrity", bad == 0, f"{len(rows)} sampled, {bad} missing or mismatched")
    ]


async def redis_checks(settings: Any) -> list[Check]:
    from redis.asyncio import Redis
    from redis.exceptions import AuthenticationError, ResponseError

    authed = Redis.from_url(str(settings.redis_url), socket_timeout=3)
    anonymous = Redis(host=settings.redis_host, port=settings.redis_port, socket_timeout=3)
    try:
        ok = bool(await authed.ping())
        try:
            await anonymous.ping()
            requires_auth = False
        except (AuthenticationError, ResponseError):
            requires_auth = True
    finally:
        await authed.aclose()
        await anonymous.aclose()
    return [
        Check("redis_reachable", ok, "authenticated PING"),
        Check("redis_requires_password", requires_auth, "anonymous PING refused"),
    ]


BACKUP_MAX_AGE_HOURS = 26


def backup_check(now: Any = None) -> Check:
    """Warning-level: the newest complete backup bundle (its name is passed in
    by `prod.sh preflight` from JOBFORGE_BACKUP_DIR) is at most 26 hours old.
    Real data should not be loaded until a verified backup exists (M20 §9)."""
    from datetime import UTC, datetime

    name = os.environ.get("JOBFORGE_LAST_VERIFIED_BACKUP", "")
    if not name:
        return Check(
            "backup_ready", False, "no backup found (set JOBFORGE_BACKUP_DIR)", fatal=False
        )
    try:
        taken = datetime.strptime(
            name.removeprefix("jobforge-backup-") + "+0000", "%Y%m%dT%H%M%SZ%z"
        )
    except ValueError:
        return Check("backup_ready", False, "unrecognised backup name", fatal=False)
    age = ((now or datetime.now(UTC)) - taken).total_seconds() / 3600
    return Check(
        "backup_ready", age <= BACKUP_MAX_AGE_HOURS, f"newest backup {age:.1f} h old", fatal=False
    )


async def preflight(*, bootstrap: bool) -> list[Check]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.config import get_settings

    settings = get_settings()  # raises (without echoing values) on bad secrets
    checks = config_checks(settings)
    root = Path(settings.artifact_storage_root)
    checks += artifact_root_checks(root)
    engine = create_async_engine(str(settings.database_url), connect_args={"timeout": 5})
    try:
        checks += await database_checks(
            engine,
            expected_db=PROD_DATABASE,
            expected_role="app_api",
            bootstrap=bootstrap,
            totp_key=settings.totp_encryption_key,
        )
        if root.is_dir():
            checks += await artifact_integrity_checks(engine, root)
    except Exception as exc:  # unreachable database is a failed check, not a traceback
        checks.append(Check("database_reachable", False, type(exc).__name__))
    finally:
        await engine.dispose()
    try:
        checks += await redis_checks(settings)
    except Exception as exc:
        checks.append(Check("redis_reachable", False, type(exc).__name__))
    checks.append(backup_check())
    return checks


# ------------------------------------------------------------------- init
async def init() -> None:
    """Owner-only, in the release container: mark the database and set the
    runtime role passwords from the environment."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.config import get_settings

    settings = get_settings()
    engine = create_async_engine(str(settings.database_url))
    try:
        async with engine.begin() as conn:
            name = await conn.scalar(text("SELECT current_database()"))
            if name != PROD_DATABASE:
                raise ProdGuardError(f"connected to {name!r}, not {PROD_DATABASE!r}")
            marker = await conn.scalar(
                text(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                    "WHERE datname = current_database()"
                )
            )
            if marker not in (None, PROD_MARKER):
                raise ProdGuardError("database carries another environment's marker")
            await conn.execute(text(f"COMMENT ON DATABASE {PROD_DATABASE} IS '{PROD_MARKER}'"))
            for role, var in ROLE_PASSWORD_VARS.items():
                password = os.environ.get(var, "")
                if len(password) < 24:
                    raise ProdGuardError(f"{var} must be set (24+ characters)")
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                )
                if not exists:
                    raise ProdGuardError(f"role {role} missing; run the migrate job first")
                literal = password.replace("'", "''")
                await conn.execute(text(f"ALTER ROLE {role} LOGIN PASSWORD '{literal}'"))
    finally:
        await engine.dispose()


# -------------------------------------------------------- bootstrap-owner
MIN_OWNER_PASSWORD = 14


async def bootstrap_owner(
    engine: Any,
    *,
    email: str,
    encryption_key: str,
    read_password: Any,
    read_code: Any,
    show: Any,
    expected_db: str = PROD_DATABASE,
) -> str:
    """Create the single owner with a password and verified TOTP enrolment.

    Interactive by design: the password comes from `read_password` (a no-echo
    prompt), the authenticator URI is passed to `show` exactly once, and a
    code from the authenticator must verify before anything is written - a
    mistyped enrolment can never lock the owner out. Refuses when any owner
    already exists. Returns the new user's id.
    """
    # The same validator the login endpoint uses (pydantic EmailStr), so the
    # bootstrap can never create an owner who cannot sign in (found in the
    # M21 drill: reserved domains such as .invalid are refused at login).
    from pydantic import EmailStr, TypeAdapter, ValidationError
    from sqlalchemy import text

    from packages.core.auth import generate_secret, hash_password, provisioning_uri, verify_code
    from packages.core.crypto import encrypt_totp_secret
    from packages.shared.ids import uuid7

    try:
        email = str(TypeAdapter(EmailStr).validate_python(email.strip())).lower()
    except ValidationError as exc:
        raise ProdGuardError("a valid email address is required (as accepted at sign-in)") from exc
    async with engine.connect() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
        if name != expected_db:
            raise ProdGuardError(f"connected to {name!r}, not {expected_db!r}")
        if await conn.scalar(text("SELECT count(*) FROM users WHERE role = 'owner'")):
            raise ProdGuardError("an owner already exists; nothing was changed")

    password = read_password()
    if not password or len(password) < MIN_OWNER_PASSWORD:
        raise ProdGuardError(f"the password must be at least {MIN_OWNER_PASSWORD} characters")
    secret = generate_secret()
    show(provisioning_uri(secret, email))
    if not verify_code(secret, read_code().strip()):
        raise ProdGuardError("the authenticator code did not verify; nothing was written")

    user_id = uuid7()
    async with engine.begin() as conn:
        if await conn.scalar(text("SELECT count(*) FROM users WHERE role = 'owner'")):
            raise ProdGuardError("an owner was created concurrently; nothing was changed")
        await conn.execute(
            text(
                "INSERT INTO users (id, email, role, password_hash, totp_secret_enc, "
                "created_at, updated_at) VALUES (:id, :email, 'owner', :hash, :totp, now(), now())"
            ),
            {
                "id": user_id,
                "email": email,
                "hash": hash_password(password),
                "totp": encrypt_totp_secret(secret, encryption_key),
            },
        )
    return str(user_id)


async def reset_owner_totp(
    engine: Any,
    *,
    email: str,
    encryption_key: str,
    read_password: Any,
    read_code: Any,
    show: Any,
    expected_db: str = PROD_DATABASE,
) -> str:
    """Re-enrol TOTP for an existing owner (lost or replaced authenticator).

    Requires the owner's current password; the new authenticator URI is shown
    once and a code from it must verify before the stored secret is replaced.
    Returns the user id; the caller revokes that user's sessions.
    """
    from sqlalchemy import text

    from packages.core.auth import generate_secret, provisioning_uri, verify_code, verify_password
    from packages.core.crypto import encrypt_totp_secret

    async with engine.connect() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
        if name != expected_db:
            raise ProdGuardError(f"connected to {name!r}, not {expected_db!r}")
        row = (
            await conn.execute(
                text("SELECT id, password_hash FROM users WHERE role = 'owner' AND email = :e"),
                {"e": email.strip().lower()},
            )
        ).first()
    # One message for "no such owner" and "wrong password": no account probing.
    refused = "email or password not accepted; nothing was changed"
    if row is None or not row.password_hash:
        read_password()
        raise ProdGuardError(refused)
    if not verify_password(read_password(), row.password_hash):
        raise ProdGuardError(refused)
    secret = generate_secret()
    show(provisioning_uri(secret, email.strip().lower()))
    if not verify_code(secret, read_code().strip()):
        raise ProdGuardError("the authenticator code did not verify; nothing was written")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET totp_secret_enc = :t, updated_at = now() WHERE id = :id"),
            {"t": encrypt_totp_secret(secret, encryption_key), "id": row.id},
        )
    return str(row.id)


# -------------------------------------------------------------------- cli
async def _bootstrap_cli(email: str) -> None:
    import getpass

    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.shared.config import get_settings

    settings = get_settings()

    def read_password() -> str:
        first = getpass.getpass("Owner password (not echoed): ")
        if first != getpass.getpass("Repeat password: "):
            raise ProdGuardError("the passwords do not match; nothing was written")
        return first

    def show(uri: str) -> None:
        # Shown once on this terminal only; never logged or stored.
        print("\nAdd this to your authenticator app now (shown once):\n\n  " + uri + "\n")

    engine = create_async_engine(str(settings.database_url))
    try:
        await bootstrap_owner(
            engine,
            email=email,
            encryption_key=settings.totp_encryption_key,
            read_password=read_password,
            read_code=lambda: input("Code from the authenticator: "),
            show=show,
        )
    finally:
        await engine.dispose()
    print("owner created; sign in with the password and an authenticator code")


async def _reset_totp_cli(email: str) -> None:
    import getpass

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.core.auth import revoke_all_sessions
    from packages.shared.config import get_settings

    settings = get_settings()

    def show(uri: str) -> None:
        print("\nAdd this to the NEW authenticator now (shown once):\n\n  " + uri + "\n")

    engine = create_async_engine(str(settings.database_url))
    try:
        user_id = await reset_owner_totp(
            engine,
            email=email,
            encryption_key=settings.totp_encryption_key,
            read_password=lambda: getpass.getpass("Current password (not echoed): "),
            read_code=lambda: input("Code from the new authenticator: "),
            show=show,
        )
    finally:
        await engine.dispose()
    redis = Redis.from_url(str(settings.redis_url))
    try:
        from uuid import UUID

        await revoke_all_sessions(
            redis, UUID(user_id), absolute_hours=settings.session_absolute_timeout_hours
        )
    finally:
        await redis.aclose()
    print("TOTP re-enrolled; every existing session for this owner was revoked")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.prod_env")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-env")
    gen.add_argument("path", type=Path)
    sub.add_parser("init")
    boot = sub.add_parser("bootstrap-owner")
    boot.add_argument("--email", required=True)
    reset = sub.add_parser("reset-totp")
    reset.add_argument("--email", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--bootstrap", action="store_true", help="allow no owner account yet")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-env":
            names = generate_env(args.path)
            print(f"wrote {len(names)} settings (values not shown) to {args.path.expanduser()}")
            return 0
        if args.command == "bootstrap-owner":
            asyncio.run(_bootstrap_cli(args.email))
            return 0
        if args.command == "reset-totp":
            asyncio.run(_reset_totp_cli(args.email))
            return 0
        if args.command == "init":
            asyncio.run(init())
            print("initialised: marker set, runtime role passwords applied")
            return 0
        checks = asyncio.run(preflight(bootstrap=args.bootstrap))
    except ProdGuardError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # never a traceback, never a value
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            # Settings hide their input, so the messages name settings only.
            reasons = "; ".join(e["msg"] for e in exc.errors(include_input=False))
            print(f"{args.command} failed: invalid configuration: {reasons}", file=sys.stderr)
        else:
            print(f"{args.command} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    failed = [c for c in checks if not c.ok and c.fatal]
    print(json.dumps({"ok": not failed, "checks": [asdict(c) for c in checks]}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
