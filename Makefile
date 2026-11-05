# Jobforge - developer commands
# Run `make help` for the list.

SHELL := /bin/bash
COMPOSE := docker compose
CORE := --profile core
FULL := --profile full

.DEFAULT_GOAL := help
.PHONY: help bootstrap dev dev-full up down stop clean logs ps shell-api shell-db \
        migrate migration downgrade seed seed-resume-studio setup-auth auth-key \
        test test-unit \
        test-integration test-e2e test-e2e-browser e2e-install test-cov \
        lint fmt typecheck check web-install web-test web-lint web-build \
        verify secrets-scan secrets-baseline audit db-roles db-grants db-reset \
        verify-schema check-shadowing test-security diagnose-roles db-verify-roles \
        demo-env demo-up demo-migrate demo-init demo-seed demo-workers demo-status \
        demo-logs demo-down demo-web-host

## ----------------------------------------------------------------- meta ---
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

## ------------------------------------------------------------ lifecycle ---
bootstrap:  ## First-time setup: .env, images, migrations
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")
	$(COMPOSE) $(CORE) build
	$(COMPOSE) $(CORE) up -d postgres redis
	@echo "waiting for postgres and redis..."
	@$(COMPOSE) $(CORE) exec -T postgres sh -c 'until pg_isready -U jobforge; do sleep 1; done'
	$(MAKE) migrate
	$(MAKE) db-roles
	@echo ""
	@echo "bootstrap complete. run 'make dev' to start the stack."

dev:  ## Start the core stack (postgres, redis, api, web)
	$(COMPOSE) $(CORE) up -d
	@echo "api  -> http://localhost:8000/docs"
	@echo "web  -> http://localhost:3000"

dev-full:  ## Start everything including minio and mailhog
	$(COMPOSE) $(FULL) up -d

up: dev  ## Alias for dev

down:  ## Stop and remove containers (keeps volumes)
	$(COMPOSE) $(FULL) down

stop:  ## Stop containers without removing them
	$(COMPOSE) $(FULL) stop

clean:  ## Stop and DELETE all volumes (destroys local data)
	$(COMPOSE) $(FULL) down -v

logs:  ## Tail logs. Usage: make logs SVC=api
	$(COMPOSE) logs -f $(SVC)

ps:  ## Show container status
	$(COMPOSE) $(FULL) ps

shell-api:  ## Shell inside the api container
	$(COMPOSE) exec api bash

shell-db:  ## psql inside the postgres container
	$(COMPOSE) exec postgres psql -U jobforge -d jobforge

## ------------------------------------------------------------ database ---
migrate:  ## Apply all migrations
	$(COMPOSE) run --rm api alembic upgrade head

migration:  ## Create a migration. Usage: make migration m="add jobs table"
	$(COMPOSE) run --rm api alembic revision --autogenerate -m "$(m)"

downgrade:  ## Roll back one migration
	$(COMPOSE) run --rm api alembic downgrade -1

seed:  ## Load synthetic development fixtures (never real PII)
	$(COMPOSE) run --rm api python scripts/seed.py

seed-resume-studio:  ## Load one demo application + tailored resume for Resume Studio
	$(COMPOSE) run --rm api python scripts/seed_resume_studio_demo.py

setup-auth:  ## Set the demo user's password + TOTP (interactive, no echo)
	$(COMPOSE) run --rm api python -m scripts.setup_demo_auth

auth-key:  ## Print a fresh TOTP_ENCRYPTION_KEY for .env
	$(COMPOSE) run --rm api python -m scripts.setup_demo_auth --generate-key

db-roles:  ## Set app_* role passwords from .env and verify they can connect
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(COMPOSE) exec -T postgres psql -U jobforge -d jobforge -v ON_ERROR_STOP=1 \
	  -c "ALTER ROLE app_api    LOGIN PASSWORD '$${APP_API_PASSWORD:-app_api}';" \
	  -c "ALTER ROLE app_agent  LOGIN PASSWORD '$${APP_AGENT_PASSWORD:-app_agent}';" \
	  -c "ALTER ROLE app_monitor LOGIN PASSWORD '$${APP_MONITOR_PASSWORD:-app_monitor}';" \
	  -c "ALTER ROLE app_migrate LOGIN PASSWORD '$${APP_MIGRATE_PASSWORD:-app_migrate}';"
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if $(COMPOSE) exec -T postgres psql -U jobforge -d jobforge -tAc \
	     "SELECT 1 FROM pg_roles WHERE rolname='app_audit'" | grep -q 1; then \
	  $(COMPOSE) exec -T postgres psql -U jobforge -d jobforge -v ON_ERROR_STOP=1 \
	    -c "ALTER ROLE app_audit LOGIN PASSWORD '$${APP_AUDIT_PASSWORD:-app_audit}';"; \
	else \
	  echo "app_audit absent (0005 not applied, or JOBFORGE_SKIP_ROLE_GRANTS=1);"; \
	  echo "  the audit-containment tests will SKIP, which is not a pass."; \
	fi
	@$(MAKE) --no-print-directory db-verify-roles

db-verify-roles:  ## Prove app_agent can authenticate AND cannot write to facts
	@set -a; [ -f .env ] && . ./.env; set +a; \
	echo "verifying app_agent can authenticate..."; \
	$(COMPOSE) exec -T -e PGPASSWORD="$${APP_AGENT_PASSWORD:-app_agent}" postgres \
	  psql -h 127.0.0.1 -U app_agent -d jobforge -v ON_ERROR_STOP=1 -tAc "SELECT current_user" \
	  || { echo ""; \
	       echo "FAILED: app_agent cannot authenticate."; \
	       echo "The 16 agent-isolation tests will SKIP, which is not a pass."; \
	       echo "Run 'make diagnose-roles' for the specific cause."; \
	       exit 1; }
	@echo "verifying app_agent CANNOT write to profile_facts..."
	@$(COMPOSE) exec -T postgres psql -U jobforge -d jobforge -tAc \
	  "SELECT has_table_privilege('app_agent','profile_facts','SELECT')::text || ' ' || \
	          has_table_privilege('app_agent','profile_facts','INSERT')::text || ' ' || \
	          has_table_privilege('app_agent','profile_facts','UPDATE')::text || ' ' || \
	          has_table_privilege('app_agent','profile_facts','DELETE')::text" \
	  | grep -qx "true false false false" \
	  || { echo "FAILED: app_agent privileges on profile_facts are wrong."; \
	       echo "Expected read-only. Run 'make db-grants' to inspect."; exit 1; }
	@echo "app_agent: can authenticate, can read, cannot write. Security fixture will run."

db-grants:  ## Show what each app_* role can do to profile_facts
	$(COMPOSE) exec -T postgres psql -U jobforge -d jobforge -c "\
	  SELECT r.rolname, \
	    has_table_privilege(r.rolname,'profile_facts','SELECT') AS can_read, \
	    has_table_privilege(r.rolname,'profile_facts','INSERT') AS can_insert, \
	    has_table_privilege(r.rolname,'profile_facts','UPDATE') AS can_update, \
	    has_table_privilege(r.rolname,'profile_facts','DELETE') AS can_delete \
	  FROM pg_roles r WHERE r.rolname LIKE 'app\\_%' ORDER BY 1;"

db-reset:  ## Drop and recreate the schema from scratch (destroys local data)
	$(COMPOSE) run --rm api alembic downgrade base
	$(COMPOSE) run --rm api alembic upgrade head

verify-schema:  ## Offline: models vs migration vs DATABASE_DESIGN.md
	python3 scripts/verify_schema.py

check-shadowing:  ## Offline: SQLAlchemy declarative name-shadowing audit
	python3 scripts/check_model_shadowing.py

test-security:  ## Run only the security-invariant tests (skips are failures)
	$(COMPOSE) run --rm -e REQUIRE_SECURITY_TESTS=1 api pytest -m safety -rs

diagnose-roles:  ## Explain why the security tests are skipping
	$(COMPOSE) exec api python scripts/diagnose_roles.py

## --------------------------------------------------------------- tests ---
test:  ## Run all backend tests
	$(COMPOSE) run --rm api pytest

.PHONY: test-evals
test-evals:  ## Offline grounding corpus and per-category report (no provider calls)
	python scripts/run_offline_evals.py --report .eval-results/offline.json

test-unit:  ## Unit tests only (no services required)
	pytest -m unit

test-integration:  ## Integration tests (requires the stack running)
	$(COMPOSE) run --rm api pytest -m integration

test-e2e:  ## Opt-in E2E API journeys (real login, CSRF, PostgreSQL; needs the stack)
	$(COMPOSE) run --rm api pytest -m e2e tests/e2e/test_m13_api_journeys.py

e2e-install:  ## Install the browser E2E toolchain into .venv (pytest-playwright + Chromium)
	.venv/bin/pip install -r requirements-dev.txt
	.venv/bin/playwright install chromium

test-e2e-browser:  ## Opt-in real-browser E2E against the running demo (make demo-up demo-seed first)
	$(DEMO_GUARD)
	.venv/bin/pytest -m e2e tests/e2e/browser

test-cov:  ## Backend tests with coverage
	$(COMPOSE) run --rm api pytest --cov --cov-report=term-missing

## ---------------------------------------------------------------- lint ---
lint:  ## Ruff lint
	ruff check .

fmt:  ## Ruff format
	ruff format .
	ruff check --fix .

typecheck:  ## Mypy
	mypy apps packages workers

check: lint typecheck test-unit  ## Everything a PR must pass locally

secrets-scan:  ## Read-only gate: fail if any tracked file has an unbaselined secret
	# `detect-secrets scan --baseline FILE` (the command this target ran
	# before) is the *baseline-maintenance* command: it always rewrites the
	# baseline and always exits 0, so it can never fail a build and silently
	# mutates the working tree on every run - not a gate at all. This is the
	# read-only counterpart the project's own .pre-commit-config.yaml already
	# uses under the hood (the detect-secrets pre-commit hook wraps exactly
	# this entry point): it compares tracked files against .secrets.baseline
	# and exits non-zero on anything not already baselined, without writing
	# to .secrets.baseline. To update the baseline itself, use
	# `make secrets-baseline` and review the diff before committing it.
	detect-secrets-hook --baseline .secrets.baseline $$(git ls-files)

secrets-baseline:  ## Developer action: regenerate .secrets.baseline (review the diff before committing)
	detect-secrets scan --baseline .secrets.baseline

audit:  ## Dependency vulnerability audit
	pip-audit -r requirements.txt

## ----------------------------------------------------------------- demo ---
# Isolated demo stack (docs/DEMO_ENVIRONMENT.md): Compose project
# `jobforge-demo`, its own containers, ports, volumes and `.env.demo`.
# Nothing here touches the development stack, and no target removes volumes.
DEMO_COMPOSE := docker compose -p jobforge-demo -f docker-compose.demo.yml --env-file .env.demo
DEMO_GUARD := @grep -qx 'JOBFORGE_DEMO_ENVIRONMENT=1' .env.demo 2>/dev/null \
	|| { echo "refusing: .env.demo missing or not a demo env (run 'make demo-env')"; exit 2; }

demo-env:  ## Demo: create .env.demo with fresh local-only secrets (never overwrites)
	python3 scripts/demo_env.py write-env

demo-up:  ## Demo: build and start postgres, redis, api and web
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) up -d --build postgres redis api web

demo-migrate:  ## Demo: verify identity, then apply migrations to jobforge_demo
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) exec -T api python -m scripts.demo_env check
	$(DEMO_COMPOSE) exec -T api alembic upgrade head

demo-init:  ## Demo: verify identity, mark the database, set demo role passwords
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) exec -T api python -m scripts.demo_env init

demo-seed:  ## Demo: verify identity and marker, then load fictional demo data
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) exec -T api python -m scripts.demo_env seed

demo-workers:  ## Demo: start the outbox, scheduler and monitor workers only
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) up -d --build worker-outbox worker-scheduler worker-monitor

demo-status:  ## Demo: show the demo containers
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) ps

demo-logs:  ## Demo: tail logs. Usage: make demo-logs SVC=api
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) logs -f --tail=100 $(SVC)

demo-down:  ## Demo: stop and remove demo containers; volumes are kept
	$(DEMO_GUARD)
	$(DEMO_COMPOSE) down

demo-web-host:  ## Demo: run the web dev server on the host instead of the container
	$(DEMO_GUARD)
	-$(DEMO_COMPOSE) stop web
	cd apps/web && API_PROXY_TARGET=http://127.0.0.1:8100 \
	  NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3100 NEXT_TELEMETRY_DISABLED=1 \
	  npx next dev -H 127.0.0.1 -p 3100

## ------------------------------------------------------------- frontend ---
web-install:  ## Install frontend dependencies
	cd apps/web && npm install

web-test:  ## Frontend tests
	cd apps/web && npm run test

web-lint:  ## Frontend lint + typecheck
	cd apps/web && npm run lint && npm run typecheck

web-build:  ## Production frontend build
	cd apps/web && npm run build

## ------------------------------------------------------------- verify ---
verify:  ## Offline scaffold verification (no services needed)
	python3 scripts/verify_scaffold.py
