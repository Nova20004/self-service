# ephemeral-idp - developer entry points.
# Compatible with GNU Make 3.81 (macOS /usr/bin/make) and newer: no .ONESHELL,
# no $(file), no grouped targets. On macOS, `gmake` and `make` both work.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE     ?= docker compose
PYTHON      ?= python3
VENV        ?= .venv
BASE_FILES  := -f docker-compose.yml
DEMO_FILES  := -f docker-compose.yml -f docker-compose.demo.yml
FILES       := $(if $(filter 1,$(DEMO)),$(DEMO_FILES),$(BASE_FILES))
LOAD_ENV    := set -a; . ./.env; set +a
SH_SCRIPTS  := scripts/demo-ephemeral-lifecycle.sh scripts/cleanup-labelled.sh scripts/doctor.sh scripts/gen-env.sh

.PHONY: help doctor env images up down validate lint test demo list-envs logs stats clean check-env

help: ## Show this help
	@echo "ephemeral-idp - self-service ephemeral environments"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Quickstart: gmake doctor && gmake env && gmake demo"

doctor: ## Check Docker, Compose v2, free ports and Python
	@bash scripts/doctor.sh

env: ## Generate .env with fresh random secrets (never overwrites)
	@bash scripts/gen-env.sh .env

check-env:
	@test -f .env || { echo "missing .env - run: gmake env"; exit 1; }

images: check-env ## Build platform, workload and Postgres images; pull socket-proxy
	$(COMPOSE) $(BASE_FILES) --profile images build
	$(COMPOSE) $(BASE_FILES) --profile images pull docker-proxy-ro docker-proxy-rw

up: images ## Start the platform (DEMO=1 adds the short-TTL demo overlay)
	$(COMPOSE) $(FILES) up -d --wait --wait-timeout 120
	@$(LOAD_ENV); echo "control plane: http://127.0.0.1:$${API_PORT:-8000}  edge: http://127.0.0.1:$${EDGE_PORT:-8080}"

down: ## Stop the stack and remove every managed-by=ephemeral-idp resource
	-@docker stop eidp-control-plane >/dev/null 2>&1 || true
	@bash scripts/cleanup-labelled.sh
	$(COMPOSE) $(DEMO_FILES) down --volumes --remove-orphans
	@echo "remaining project containers: $$(docker ps -aq --filter label=com.docker.compose.project=ephemeral-idp | wc -l | tr -d ' ')"

demo: ## Run the full lifecycle demo (starts the stack with the demo overlay)
	$(MAKE) up DEMO=1
	bash scripts/demo-ephemeral-lifecycle.sh

validate: ## Static checks: compose config, yamllint, ruff, mypy, hadolint, shellcheck
	$(COMPOSE) --env-file .env.example $(BASE_FILES) config --quiet && echo "PASS docker compose config (base)"
	$(COMPOSE) --env-file .env.example $(DEMO_FILES) config --quiet && echo "PASS docker compose config (demo overlay)"
	@$(MAKE) --no-print-directory lint

lint: ## yamllint --strict, ruff, mypy, hadolint, shellcheck -x
	@if command -v yamllint >/dev/null 2>&1; then yamllint --strict . && echo "PASS yamllint --strict"; \
	  elif [ -x $(VENV)/bin/yamllint ]; then $(VENV)/bin/yamllint --strict . && echo "PASS yamllint --strict"; \
	  else echo "SKIPPED yamllint (not installed)"; fi
	@if [ -x $(VENV)/bin/ruff ]; then \
	  cd control-plane && ../$(VENV)/bin/ruff check . && ../$(VENV)/bin/ruff format --check . && cd .. && \
	  $(VENV)/bin/ruff check --config cli/ruff.toml cli/envctl && \
	  $(VENV)/bin/ruff check --config cli/ruff.toml scripts/sign-slack-request.py workload-template/main.py && \
	  echo "PASS ruff"; \
	  else echo "SKIPPED ruff (run gmake test once to create $(VENV))"; fi
	@if [ -x $(VENV)/bin/mypy ]; then cd control-plane && ../$(VENV)/bin/mypy && echo "PASS mypy --strict"; \
	  else echo "SKIPPED mypy (run gmake test once to create $(VENV))"; fi
	@if command -v hadolint >/dev/null 2>&1; then \
	  hadolint control-plane/Dockerfile workload-template/Dockerfile traefik/Dockerfile postgres/Dockerfile && echo "PASS hadolint"; \
	  else echo "SKIPPED hadolint (not installed)"; fi
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck -x $(SH_SCRIPTS) && echo "PASS shellcheck -x"; \
	  else echo "SKIPPED shellcheck (not installed)"; fi

$(VENV)/.installed: control-plane/requirements.txt control-plane/requirements-dev.txt
	$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else "control-plane tests need Python >= 3.13 (set PYTHON=python3.13)")'
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --disable-pip-version-check -r control-plane/requirements-dev.txt
	@touch $@

test: $(VENV)/.installed ## Unit tests with coverage gate (>= 85%)
	cd control-plane && ../$(VENV)/bin/python -m pytest --cov --cov-report=term-missing

list-envs: check-env ## List all environments (admin view)
	@$(LOAD_ENV); ENVCTL_TOKEN="$$ADMIN_TOKEN" $(PYTHON) cli/envctl --url "http://127.0.0.1:$${API_PORT:-8000}" list --all

logs: ## Follow platform logs
	$(COMPOSE) $(FILES) logs -f --tail=100

stats: ## One-shot memory/CPU snapshot of platform + environments
	@docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}\t{{.PIDs}}' $$(docker ps -q --filter name=eidp-)

clean: down ## down + remove built images, venv and caches (keeps .env)
	-docker image rm ephemeral-idp/control-plane:0.1.0 ephemeral-idp/traefik:0.1.0 ephemeral-idp/workload:0.1.0 ephemeral-idp/postgres:17.11-alpine-p1 2>/dev/null
	rm -rf $(VENV) control-plane/.pytest_cache control-plane/.mypy_cache control-plane/.ruff_cache control-plane/.coverage
