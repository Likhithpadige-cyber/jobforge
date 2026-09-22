#!/usr/bin/env python3
"""Offline scaffold verification.

Checks everything that can be checked without PostgreSQL, Redis, Docker, or
an installed dependency set: file layout, Python syntax, import-graph
direction, config/YAML/JSON validity, and a handful of safety invariants
from SECURITY.md that are cheap to assert mechanically.

    python3 scripts/verify_scaffold.py

Exit code 0 means the scaffold is structurally sound. It does NOT mean the
services start - for that, run `make bootstrap && make dev`.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))


# --------------------------------------------------------------- structure
REQUIRED_FILES = [
    "README.md",
    "Makefile",
    ".gitignore",
    ".env.example",
    ".dockerignore",
    ".pre-commit-config.yaml",
    "docker-compose.yml",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "alembic.ini",
    "apps/api/main.py",
    "apps/api/deps.py",
    "apps/api/routers/health.py",
    "apps/api/schemas/health.py",
    "apps/web/package.json",
    "apps/web/tsconfig.json",
    "apps/web/next.config.mjs",
    "apps/web/tailwind.config.ts",
    "apps/web/vitest.config.ts",
    "apps/web/app/layout.tsx",
    "apps/web/app/page.tsx",
    "apps/web/lib/api.ts",
    "apps/web/components/HealthBadge.tsx",
    "packages/shared/config.py",
    "packages/shared/logging.py",
    "packages/shared/ids.py",
    "packages/shared/errors.py",
    "packages/core/db/session.py",
    "packages/core/db/base.py",
    "packages/core/db/health.py",
    "packages/core/cache/redis.py",
    "packages/core/cache/health.py",
    "migrations/env.py",
    "migrations/versions/0001_baseline_extensions.py",
    "migrations/versions/0002_core_schema.py",
    "migrations/versions/0003_roles_and_grants.py",
    "migrations/sql/0002_core_schema.up.sql",
    "migrations/sql/0002_core_schema.down.sql",
    "migrations/sql/0003_roles_grants.up.sql",
    "migrations/sql/0003_roles_grants.down.sql",
    "packages/core/models/enums.py",
    "packages/core/models/identity.py",
    "packages/core/models/facts.py",
    "packages/core/models/jobs.py",
    "packages/core/models/applications.py",
    "packages/core/models/operations.py",
    "packages/core/repositories/base.py",
    "packages/core/repositories/profile.py",
    "packages/core/repositories/facts.py",
    "packages/core/services/profile_service.py",
    "apps/api/routers/profile.py",
    "apps/api/routers/facts.py",
    "apps/api/schemas/profile.py",
    "apps/api/schemas/facts.py",
    "docs/DATABASE_IMPLEMENTATION.md",
    "tests/conftest.py",
    "infra/docker/Dockerfile.api",
    "infra/docker/Dockerfile.web",
    ".github/workflows/ci.yml",
    "docs/ARCHITECTURE.md",
    "docs/AGENT_DESIGN.md",
    "docs/DATABASE_DESIGN.md",
    "docs/EVENTS.md",
    "docs/SECURITY.md",
]


def check_structure() -> None:
    missing = [f for f in REQUIRED_FILES if not (ROOT / f).is_file()]
    if missing:
        record(FAIL, "required files present", f"missing: {', '.join(missing)}")
    else:
        record(PASS, "required files present", f"{len(REQUIRED_FILES)} files")


# ------------------------------------------------------------ python syntax
def python_files() -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*.py")
        if not any(part in {".venv", "node_modules", "__pycache__", ".next"} for part in p.parts)
    ]


def check_python_syntax() -> None:
    errors: list[str] = []
    for path in python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
    if errors:
        record(FAIL, "python syntax", "; ".join(errors))
    else:
        record(PASS, "python syntax", f"{len(python_files())} files parsed")


# --------------------------------------------------------------- config files
def check_configs() -> None:
    try:
        tomllib.loads((ROOT / "pyproject.toml").read_text())
        record(PASS, "pyproject.toml parses", "")
    except Exception as exc:
        record(FAIL, "pyproject.toml parses", str(exc))

    for rel in ("apps/web/package.json", "apps/web/tsconfig.json", ".secrets.baseline"):
        try:
            json.loads((ROOT / rel).read_text())
            record(PASS, f"{rel} parses", "")
        except Exception as exc:
            record(FAIL, f"{rel} parses", str(exc))

    try:
        import yaml

        for rel in ("docker-compose.yml", ".pre-commit-config.yaml", ".github/workflows/ci.yml"):
            yaml.safe_load((ROOT / rel).read_text())
            record(PASS, f"{rel} parses", "")
    except ImportError:
        record(WARN, "yaml files parse", "pyyaml not installed; skipped")
    except Exception as exc:
        record(FAIL, "yaml files parse", str(exc))


# ------------------------------------------------------------- import graph
LAYERS = {
    "packages/shared": 0,
    "packages/core": 1,
    "packages/agents": 2,
    "packages/services": 2,
    "apps/api": 3,
}


def check_import_direction() -> None:
    """ARCHITECTURE.md §12: apps -> agents/services -> core -> shared."""
    violations: list[str] = []
    for path in python_files():
        rel = path.relative_to(ROOT).as_posix()
        layer = next((v for k, v in LAYERS.items() if rel.startswith(k)), None)
        if layer is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if not module:
                continue
            target = module.replace(".", "/")
            target_layer = next((v for k, v in LAYERS.items() if target.startswith(k)), None)
            if target_layer is not None and target_layer > layer:
                violations.append(f"{rel} imports {module}")
    if violations:
        record(FAIL, "import direction", "; ".join(violations))
    else:
        record(PASS, "import direction", "no upward imports")


# ------------------------------------------------------ internal imports
def _exported_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def check_internal_imports() -> None:
    """Every `from packages.x import Y` must resolve to a real definition.

    Catches typos and missing re-exports without importing anything, so it
    works before dependencies are installed.
    """
    problems: list[str] = []
    resolved = 0
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(("packages.", "apps.")):
                continue
            base = ROOT / node.module.replace(".", "/")
            module_file = base.with_suffix(".py")
            init_file = base / "__init__.py"
            if module_file.is_file():
                target = module_file
            elif init_file.is_file():
                target = init_file
            else:
                problems.append(f"{path.relative_to(ROOT)}: no module {node.module}")
                continue
            available = _exported_names(target)
            for alias in node.names:
                resolved += 1
                if alias.name == "*" or alias.name in available:
                    continue
                if (base / f"{alias.name}.py").is_file():
                    continue
                problems.append(f"{path.relative_to(ROOT)}: `{alias.name}` not in {node.module}")
    if problems:
        record(FAIL, "internal imports resolve", "; ".join(problems[:5]))
    else:
        record(PASS, "internal imports resolve", f"{resolved} imports")


# ---------------------------------------------------------------- safety
def check_no_committed_secrets() -> None:
    """No real-looking key material anywhere in the tree."""
    patterns = [
        (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "Anthropic API key"),
        (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    ]
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(p in {"node_modules", ".git", ".next", "__pycache__"} for p in path.parts):
            continue
        if path.name == "verify_scaffold.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            # Respect detect-secrets' inline allowlist pragma so that test
            # fixtures containing deliberately fake key-shaped strings pass.
            if "pragma: allowlist secret" in line:
                continue
            for pattern, label in patterns:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{line_no} ({label})")
    if hits:
        record(FAIL, "no committed secrets", "; ".join(hits))
    else:
        record(PASS, "no committed secrets", "")


def check_env_example_has_no_values_for_secrets() -> None:
    secret_keys = ("ANTHROPIC_API_KEY", "SESSION_SECRET")
    text = (ROOT / ".env.example").read_text()
    bad = [k for k in secret_keys if re.search(rf"^{k}=.+$", text, re.MULTILINE)]
    if bad:
        record(FAIL, ".env.example has empty secrets", f"populated: {', '.join(bad)}")
    else:
        record(PASS, ".env.example has empty secrets", "")


def check_env_is_gitignored() -> None:
    ignore = (ROOT / ".gitignore").read_text()
    if re.search(r"^\.env$", ignore, re.MULTILINE):
        record(PASS, ".env is gitignored", "")
    else:
        record(FAIL, ".env is gitignored", ".env not found in .gitignore")


def check_no_prohibited_dependencies() -> None:
    """SECURITY.md §7.1: CAPTCHA-solving and stealth packages must not exist."""
    banned = re.compile(
        r"2captcha|anticaptcha|capmonster|deathbycaptcha|"
        r"puppeteer-extra-plugin-stealth|undetected.chromedriver",
        re.IGNORECASE,
    )
    hits: list[str] = []
    for rel in ("requirements.txt", "requirements-dev.txt", "apps/web/package.json"):
        if banned.search((ROOT / rel).read_text()):
            hits.append(rel)
    if hits:
        record(FAIL, "no CAPTCHA/stealth dependencies", "; ".join(hits))
    else:
        record(PASS, "no CAPTCHA/stealth dependencies", "")


def check_safe_defaults() -> None:
    """Defaults must not permit live submission or live LLM calls."""
    config = (ROOT / "packages/shared/config.py").read_text()
    checks = [
        (
            "submission_mode: SubmissionMode = SubmissionMode.DRY_RUN",
            "submission defaults to dry_run",
        ),
        ("llm_mode: LLMMode = LLMMode.REPLAY", "llm defaults to replay"),
        ('anthropic_api_key: str = ""', "no default API key"),
    ]
    missing = [label for needle, label in checks if needle not in config]
    if missing:
        record(FAIL, "safe defaults", f"missing: {', '.join(missing)}")
    else:
        record(PASS, "safe defaults", "dry_run + replay + empty key")


def check_health_routes_registered() -> None:
    router = (ROOT / "apps/api/routers/health.py").read_text()
    main = (ROOT / "apps/api/main.py").read_text()
    problems: list[str] = []
    if '@router.get("/health"' not in router:
        problems.append("GET /health missing")
    if '@api_router.get("/health"' not in router:
        problems.append("GET {prefix}/health missing")
    if "include_router(health.router)" not in main:
        problems.append("liveness router not registered")
    if "include_router(health.api_router, prefix=settings.api_prefix)" not in main:
        problems.append("readiness router not registered under prefix")
    if problems:
        record(FAIL, "health endpoints wired", "; ".join(problems))
    else:
        record(PASS, "health endpoints wired", "/health and /api/v1/health")


def check_liveness_has_no_dependencies() -> None:
    """Liveness must not call the database or Redis."""
    router = (ROOT / "apps/api/routers/health.py").read_text()
    body_start = router.index("async def liveness(")
    body_end = router.index("async def liveness_alias(")
    body = router[body_start:body_end]
    if "check_database" in body or "check_redis" in body:
        record(FAIL, "liveness has no dependency calls", "liveness touches a service")
    else:
        record(PASS, "liveness has no dependency calls", "")


def check_compose_service_wiring() -> None:
    try:
        import yaml
    except ImportError:
        record(WARN, "compose wiring", "pyyaml not installed; skipped")
        return
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose.get("services", {})
    problems: list[str] = []
    for name in ("postgres", "redis", "api", "web"):
        if name not in services:
            problems.append(f"missing service {name}")
    for name in ("postgres", "redis", "api", "web"):
        if name in services and "healthcheck" not in services[name]:
            problems.append(f"{name} has no healthcheck")
    api = services.get("api", {})
    depends = api.get("depends_on", {})
    for dep in ("postgres", "redis"):
        if depends.get(dep, {}).get("condition") != "service_healthy":
            problems.append(f"api does not wait for healthy {dep}")
    if services.get("postgres", {}).get("image", "").find("pgvector") == -1:
        problems.append("postgres image is not pgvector (needed for Phase 2)")
    if problems:
        record(FAIL, "compose wiring", "; ".join(problems))
    else:
        record(PASS, "compose wiring", "4 services, healthchecks, ordered startup")


def check_frontend_tests_exist() -> None:
    tests = list((ROOT / "apps/web/__tests__").glob("*.test.*"))
    if len(tests) >= 2:
        record(PASS, "frontend tests present", f"{len(tests)} files")
    else:
        record(FAIL, "frontend tests present", f"only {len(tests)}")


def check_migration_chain() -> None:
    versions = sorted((ROOT / "migrations/versions").glob("*.py"))
    if not versions:
        record(FAIL, "migration chain", "no migrations")
        return
    heads = 0
    for path in versions:
        text = path.read_text()
        if re.search(r"^down_revision: str \| None = None$", text, re.MULTILINE):
            heads += 1
    if heads != 1:
        record(FAIL, "migration chain", f"{heads} base revisions, expected exactly 1")
    else:
        record(PASS, "migration chain", f"{len(versions)} revision(s), 1 base")


def check_phase2_delegated() -> None:
    """Delegate the schema and shadowing checks to their own scripts."""
    import subprocess

    for script, label in (
        ("verify_schema.py", "schema conformance"),
        ("check_model_shadowing.py", "no model name shadowing"),
    ):
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(ROOT / "scripts" / script)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        if result.returncode == 0:
            record(PASS, label, f"scripts/{script}")
        else:
            record(
                FAIL, label, result.stdout.strip().splitlines()[-1] if result.stdout else "failed"
            )


def main() -> int:
    check_structure()
    check_python_syntax()
    check_configs()
    check_import_direction()
    check_internal_imports()
    check_health_routes_registered()
    check_liveness_has_no_dependencies()
    check_compose_service_wiring()
    check_migration_chain()
    check_phase2_delegated()
    check_frontend_tests_exist()
    check_no_committed_secrets()
    check_env_example_has_no_values_for_secrets()
    check_env_is_gitignored()
    check_no_prohibited_dependencies()
    check_safe_defaults()

    width = max(len(name) for _, name, _ in results) + 2
    print("\nJobforge scaffold verification")
    print("=" * (width + 40))
    for status, name, detail in results:
        marker = {PASS: "  ok  ", FAIL: " FAIL ", WARN: " warn "}[status]
        print(f"[{marker}] {name:<{width}} {detail}")
    print("=" * (width + 40))

    failed = sum(1 for s, _, _ in results if s == FAIL)
    warned = sum(1 for s, _, _ in results if s == WARN)
    passed = sum(1 for s, _, _ in results if s == PASS)
    print(f"{passed} passed, {failed} failed, {warned} warnings\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
