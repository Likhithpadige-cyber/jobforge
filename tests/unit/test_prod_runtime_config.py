"""M20 step 2: the private runtime's compose file, edge and wrapper.

Static checks of docker-compose.prod.yml and infra/caddy/Caddyfile, plus the
refusals of scripts/prod.sh. Nothing here starts a container: the only
docker invocation is `docker compose config`, which validates and resolves
the file locally, and it is skipped when docker is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.safety]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
CADDYFILE = (ROOT / "infra" / "caddy" / "Caddyfile").read_text()
WRAPPER = ROOT / "scripts" / "prod.sh"
BASH = shutil.which("bash") or "/bin/bash"
DOCKER = shutil.which("docker") or "docker"
WORKERS = {name for name in SERVICES if name.startswith("worker-")}
PYTHON_APPS = {"api", "migrate", *WORKERS}


def test_only_the_edge_publishes_a_port_and_only_on_loopback() -> None:
    published = {name: svc["ports"] for name, svc in SERVICES.items() if svc.get("ports")}
    assert set(published) == {"edge"}
    assert all(str(port).startswith("127.0.0.1:") for port in published["edge"])
    assert all("expose" not in svc for svc in SERVICES.values())


def test_networks_isolate_state_and_block_egress() -> None:
    networks = COMPOSE["networks"]
    assert networks["app"].get("internal") is True
    assert networks["data"].get("internal") is True
    assert set(SERVICES["edge"]["networks"]) == {"public", "app"}
    assert SERVICES["web"]["networks"] == ["app"]  # never on `data`
    assert set(SERVICES["api"]["networks"]) == {"app", "data"}
    for name in ("postgres", "redis", "migrate", *WORKERS):
        assert SERVICES[name]["networks"] == ["data"], name
    # Only the edge touches the non-internal network.
    assert [n for n, s in SERVICES.items() if "public" in s.get("networks", [])] == ["edge"]


def test_no_repository_bind_mounts_except_the_readonly_caddyfile() -> None:
    named = set(COMPOSE["volumes"])
    for name, svc in SERVICES.items():
        for volume in svc.get("volumes", []):
            source = str(volume).split(":")[0]
            if source in named:
                continue
            assert volume == "./infra/caddy/Caddyfile:/etc/caddy/Caddyfile:ro", (name, volume)


def test_images_are_production_builds() -> None:
    for name in ("api", "web", *WORKERS):
        svc = SERVICES[name]
        assert svc["build"]["target"] == "production", name
        assert "${JOBFORGE_RELEASE" in svc["image"], name
    assert SERVICES["web"]["build"]["args"]["NEXT_PUBLIC_API_BASE_URL"] == "same-origin"


def test_no_model_calling_or_submission_worker_runs() -> None:
    commands = " ".join(" ".join(SERVICES[w]["command"]) for w in WORKERS)
    for forbidden in ("first_draft", "optimizer", "cover_letter", "submission"):
        assert forbidden not in commands
    assert {" ".join(SERVICES[w]["command"]) for w in WORKERS} == {
        f"python -m workers.{m}"
        for m in ("outbox_relay", "application", "render", "monitor", "scheduler")
    }


def test_runtime_modes_are_pinned_safe() -> None:
    env = COMPOSE["x-app-env"]
    assert env["ENVIRONMENT"] == "production"
    assert env["LLM_MODE"] == "replay"
    assert env["SUBMISSION_MODE"] == "dry_run"
    assert env["EMAIL_TRANSPORT_MODE"] == "fake"
    assert env["ARTIFACT_STORAGE_BACKEND"] == "filesystem"
    for name in PYTHON_APPS:
        assert SERVICES[name]["environment"]["LLM_MODE"] == "replay", name


def test_least_privilege_database_roles() -> None:
    assert "//app_api:" in SERVICES["api"]["environment"]["DATABASE_URL"]
    assert "//app_audit:" in SERVICES["api"]["environment"]["AUDIT_DATABASE_URL"]
    agent = {"worker-outbox", "worker-application", "worker-render"}
    for name in WORKERS:
        url = SERVICES[name]["environment"]["WORKER_DATABASE_URL"]
        role = "app_agent" if name in agent else "app_monitor"
        assert f"//{role}:" in url, name
    # Only the one-shot migration job uses the owner role (via POSTGRES_*).
    assert "DATABASE_URL" not in SERVICES["migrate"]["environment"]
    assert SERVICES["migrate"]["profiles"] == ["release"]
    assert SERVICES["migrate"]["restart"] == "no"


def test_web_holds_no_secrets() -> None:
    web = SERVICES["web"]
    assert "env_file" not in web
    assert set(web["environment"]) == {"NODE_ENV", "HOSTNAME", "PORT", "NEXT_TELEMETRY_DISABLED"}


def test_hardening_on_every_service() -> None:
    for name, svc in SERVICES.items():
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["logging"]["options"]["max-size"], name
        assert svc["restart"] == ("no" if name == "migrate" else "unless-stopped"), name
        if name != "migrate":
            assert svc.get("mem_limit") and svc.get("cpus"), name
    for name in ("edge", "web", *PYTHON_APPS):
        assert SERVICES[name]["cap_drop"] == ["ALL"], name
        assert SERVICES[name]["read_only"] is True, name
    for name in ("edge", "web", "api", "postgres", "redis"):
        assert "healthcheck" in SERVICES[name], name


def test_persistent_named_volumes_are_labelled() -> None:
    volumes = COMPOSE["volumes"]
    assert set(volumes) == {"pg_data", "redis_data", "artifact_data"}
    assert all(v["labels"]["com.jobforge.env"] == "prod" for v in volumes.values())
    mounts = {
        n
        for n, s in SERVICES.items()
        if any("artifact_data" in str(v) for v in s.get("volumes", []))
    }
    assert mounts == {"api", "worker-application", "worker-render"}


def test_edge_routes_one_origin_and_hides_internals() -> None:
    assert "admin off" in CADDYFILE and "auto_https off" in CADDYFILE
    assert "reverse_proxy api:8000" in CADDYFILE and "reverse_proxy web:3000" in CADDYFILE
    assert "handle /api/v1/*" in CADDYFILE
    assert "@internal path /health /docs" in CADDYFILE and "respond @internal 404" in CADDYFILE
    for header in ("Cookie", "Authorization", "X-Csrf-Token", "Set-Cookie"):
        assert f">{header} delete" in CADDYFILE
    assert ":443" not in CADDYFILE and ":80 " not in CADDYFILE


def test_dev_and_demo_compose_files_are_untouched_by_m20() -> None:
    """The private runtime is additive; the other stacks keep their names."""
    dev = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    demo = yaml.safe_load((ROOT / "docker-compose.demo.yml").read_text())
    assert (dev["name"], demo["name"], COMPOSE["name"]) == (
        "jobforge",
        "jobforge-demo",
        "jobforge-prod",
    )


# ------------------------------------------------------------ the wrapper
SYNTHETIC = {
    "JOBFORGE_PROD_ENVIRONMENT": "1",
    "ENVIRONMENT": "production",
    "POSTGRES_PASSWORD": "synthPg0001",
    "APP_API_PASSWORD": "synthApi0001",
    "APP_AGENT_PASSWORD": "synthAgent0001",
    "APP_MONITOR_PASSWORD": "synthMonitor0001",
    "APP_AUDIT_PASSWORD": "synthAudit0001",
    "REDIS_PASSWORD": "synthRedis0001",
    "SESSION_SECRET": "synthSession0001",
    "TOTP_ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
}


def _env_file(tmp_path: Path, mode: int = 0o600, **changes: str | None) -> Path:
    values: dict[str, str | None] = {**SYNTHETIC, **changes}
    path = tmp_path / "prod.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None))
    path.chmod(mode)
    return path


def _wrapper(*args: str, env_file: Path | None, **env: str) -> subprocess.CompletedProcess[str]:
    run_env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    if env_file is not None:
        run_env["JOBFORGE_PROD_ENV_FILE"] = str(env_file)
    run_env["JOBFORGE_RELEASE"] = "test"
    run_env.update(env)
    return subprocess.run(  # noqa: S603 - our own script, fixed argv
        [BASH, str(WRAPPER), *args],
        cwd=ROOT,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    ("args", "changes", "env", "message"),
    [
        (("down",), {}, {}, "unknown command"),
        (("rm",), {}, {}, "unknown command"),
        (("stop", "-v"), {}, {}, "not allowed"),
        (("up", "--remove-orphans"), {}, {}, "not allowed"),
        (("ps",), {"JOBFORGE_PROD_ENVIRONMENT": None}, {}, "JOBFORGE_PROD_ENVIRONMENT=1"),
        (("ps",), {"ENVIRONMENT": "local"}, {}, "ENVIRONMENT=production"),
        (("ps",), {"ANTHROPIC_API_KEY": "sk-synthetic"}, {}, "live AI is disabled"),
        (("ps",), {"LLM_MODE": "live"}, {}, "LLM_MODE must be replay"),
        (("ps",), {"SUBMISSION_MODE": "enabled"}, {}, "dry_run"),
        (("ps",), {"DATABASE_URL": "postgresql://x"}, {}, "DATABASE_URL must not be set"),
        (("ps",), {"POSTGRES_PASSWORD": "localdev"}, {}, "known placeholder"),
        (("ps",), {"REDIS_PASSWORD": None}, {}, "REDIS_PASSWORD is not set"),
        (("ps",), {"APP_API_PASSWORD": "a@b:c"}, {}, "unsafe in a connection URL"),
        (("ps",), {}, {"JOBFORGE_PROD_PROJECT": "jobforge"}, "project must be"),
        (("ps",), {}, {"JOBFORGE_PROD_PROJECT": "jobforge-prodtest"}, "own JOBFORGE_PROD_PORT"),
        (("ps",), {}, {"JOBFORGE_PROD_PORT": "3100"}, "dev/demo stacks"),
    ],
)
def test_wrapper_refusals(
    tmp_path: Path,
    args: tuple[str, ...],
    changes: dict[str, str | None],
    env: dict[str, str],
    message: str,
) -> None:
    result = _wrapper(*args, env_file=_env_file(tmp_path, **changes), **env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr
    secrets = [
        v for k, v in SYNTHETIC.items() if any(t in k for t in ("PASSWORD", "SECRET", "KEY"))
    ]
    for secret in secrets:
        assert secret not in result.stderr + result.stdout


def test_wrapper_refuses_loose_permissions_and_missing_env(tmp_path: Path) -> None:
    assert (
        "permissions must be 600" in _wrapper("ps", env_file=_env_file(tmp_path, mode=0o644)).stderr
    )
    assert "not set" in _wrapper("ps", env_file=None).stderr
    assert "not found" in _wrapper("ps", env_file=tmp_path / "missing.env").stderr


def test_wrapper_refuses_an_env_file_inside_the_repository(tmp_path: Path) -> None:
    inside = ROOT / ".prod-test.env"
    try:
        inside.write_text(_env_file(tmp_path).read_text())
        inside.chmod(0o600)
        result = _wrapper("ps", env_file=inside)
        assert result.returncode == 2 and "outside the repository" in result.stderr
    finally:
        inside.unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_compose_file_resolves_with_a_valid_env(tmp_path: Path) -> None:
    """`docker compose config` validates the file and resolves the published
    port to 127.0.0.1 only. Local and read-only: nothing is started."""
    ok = _wrapper("config", env_file=_env_file(tmp_path))
    assert ok.returncode == 0, ok.stderr[-1500:]
    resolved = subprocess.run(  # noqa: S603
        [
            DOCKER,
            "compose",
            "-f",
            str(ROOT / "docker-compose.prod.yml"),
            "--env-file",
            str(_env_file(tmp_path)),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "JOBFORGE_PROD_ENV_FILE": str(_env_file(tmp_path)),
            "JOBFORGE_RELEASE": "test",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    import json

    services = json.loads(resolved.stdout)["services"]
    ports = [p for s in services.values() for p in s.get("ports", [])]
    assert len(ports) == 1
    assert ports[0]["host_ip"] == "127.0.0.1" and ports[0]["target"] == 8080
