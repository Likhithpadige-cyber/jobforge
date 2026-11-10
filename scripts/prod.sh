#!/usr/bin/env bash
# JobForge private runtime wrapper (M20). The only supported way to drive
# docker-compose.prod.yml. It pins the project, the compose file and an env
# file kept outside the repository, and refuses anything that could destroy
# data or turn on live AI.
#
#   JOBFORGE_PROD_ENV_FILE=~/Library/Application\ Support/JobForge/prod.env \
#     scripts/prod.sh <config|build|up|start|stop|ps|logs [service]|migrate>
#
# Optional: JOBFORGE_PROD_PROJECT=jobforge-prodtest (synthetic rehearsal only,
# needs a different JOBFORGE_PROD_PORT), JOBFORGE_RELEASE (image tag; defaults
# to the current commit, "-dirty" if the tree has changes).
#
# There is deliberately no `down` command: `stop` keeps containers and every
# volume. Removing a production project is a manual, separately approved act.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
COMPOSE_FILE="$ROOT/docker-compose.prod.yml"

die() { echo "prod.sh: refusing: $*" >&2; exit 2; }

command=${1:-}
case "$command" in
  config|build|up|start|stop|ps|logs|migrate) ;;
  *) die "unknown command '${command}' (config|build|up|start|stop|ps|logs|migrate)" ;;
esac
shift || true
for arg in "$@"; do
  case "$arg" in
    -v|--volumes|--rmi|-f|--force|--remove-orphans|down|rm) die "argument '$arg' is not allowed" ;;
  esac
done

project=${JOBFORGE_PROD_PROJECT:-jobforge-prod}
[[ "$project" =~ ^jobforge-prod(test)?$ ]] || die "project must be jobforge-prod or jobforge-prodtest"
port=${JOBFORGE_PROD_PORT:-8480}
[[ "$port" =~ ^[0-9]+$ ]] || die "JOBFORGE_PROD_PORT must be a number"
case "$port" in 3000|3100|3383|8000|8100|8383) die "port $port belongs to the dev/demo stacks" ;; esac
if [[ "$project" == "jobforge-prodtest" && "$port" == "8480" ]]; then
  die "the rehearsal project needs its own JOBFORGE_PROD_PORT"
fi

env_file=${JOBFORGE_PROD_ENV_FILE:-}
[[ -n "$env_file" ]] || die "JOBFORGE_PROD_ENV_FILE is not set"
[[ -f "$env_file" ]] || die "env file not found"
env_file=$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")
case "$env_file" in "$ROOT"/*) die "the env file must live outside the repository" ;; esac
perm=$(stat -f '%Lp' "$env_file" 2>/dev/null || stat -c '%a' "$env_file")
[[ "$perm" == "600" ]] || die "env file permissions must be 600 (are $perm)"

value() { grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true; }
[[ "$(value JOBFORGE_PROD_ENVIRONMENT)" == "1" ]] || die "env file lacks JOBFORGE_PROD_ENVIRONMENT=1"
[[ "$(value ENVIRONMENT)" == "production" ]] || die "env file must set ENVIRONMENT=production"
# Live AI stays disabled: no provider key, replay only (compose also pins it).
[[ -z "$(value ANTHROPIC_API_KEY)" ]] || die "ANTHROPIC_API_KEY must be empty (live AI is disabled)"
llm=$(value LLM_MODE); [[ -z "$llm" || "$llm" == "replay" ]] || die "LLM_MODE must be replay"
sub=$(value SUBMISSION_MODE); [[ -z "$sub" || "$sub" == "dry_run" ]] || die "SUBMISSION_MODE must be dry_run"
# Connection strings are composed per role in docker-compose.prod.yml; an
# override here could silently connect the API as the owner role.
for forbidden in DATABASE_URL AUDIT_DATABASE_URL WORKER_DATABASE_URL REDIS_URL; do
  [[ -z "$(value $forbidden)" ]] || die "$forbidden must not be set in the env file"
done
for required in POSTGRES_PASSWORD APP_API_PASSWORD APP_AGENT_PASSWORD APP_MONITOR_PASSWORD \
                APP_AUDIT_PASSWORD REDIS_PASSWORD SESSION_SECRET TOTP_ENCRYPTION_KEY; do
  v=$(value "$required")
  [[ -n "$v" ]] || die "$required is not set"
  case "$v" in localdev|minioadmin|changeme|password|jobforge) die "$required is a known placeholder" ;; esac
  [[ "$v" =~ ^[A-Za-z0-9_=+/-]+$ ]] || die "$required contains characters unsafe in a connection URL"
done

release=${JOBFORGE_RELEASE:-$(git -C "$ROOT" rev-parse --short=12 HEAD)$(git -C "$ROOT" diff --quiet HEAD -- || echo -dirty)}
export JOBFORGE_PROD_ENV_FILE="$env_file" JOBFORGE_RELEASE="$release" JOBFORGE_PROD_PORT="$port"

compose=(docker compose -p "$project" -f "$COMPOSE_FILE" --env-file "$env_file")
case "$command" in
  config)  exec "${compose[@]}" config --quiet ;;
  build)   exec "${compose[@]}" build "$@" ;;
  up)      exec "${compose[@]}" up -d "$@" ;;
  start)   exec "${compose[@]}" start "$@" ;;
  stop)    exec "${compose[@]}" stop "$@" ;;
  ps)      exec "${compose[@]}" ps "$@" ;;
  logs)    exec "${compose[@]}" logs --tail=200 "$@" ;;
  migrate) exec "${compose[@]}" --profile release run --rm migrate ;;
esac
