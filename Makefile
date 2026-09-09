.DEFAULT_GOAL := help
SHELL := /bin/bash

-include .env
export

PYTHON        := .venv/bin/python
CLI           := $(PYTHON) -m app.cli
DEMO_APP_URL  ?= http://localhost:8000
DEMO_OPERATOR_ID    ?= teller1
DEMO_ACCESS_CODE    ?= teller-pass
DEMO_SUPERVISOR_ID  ?= super1
DEMO_SUPERVISOR_CODE ?= super-pass
LOOKUP_ARTIFACT     := artifacts/member_savings_lookup.v1.json
SUBACCOUNT_ARTIFACT := artifacts/open_savings_subaccount.v1.json
LOOKUP_INPUTS       := --input operator_id=$(DEMO_OPERATOR_ID) --input access_code=env:DEMO_ACCESS_CODE
SUPERVISOR_INPUTS   := --input operator_id=$(DEMO_SUPERVISOR_ID) --input access_code=env:DEMO_SUPERVISOR_CODE

.PHONY: help setup lint format test test-unit test-integration test-e2e test-live demo discover discover-subaccount \
        replay replay-not-found replay-invalid replay-transient replay-fail-checkpoint replay-fail-ambiguous \
        demo-escalation demo-approval evidence evidence-check inspect clean-evidence

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv, install pinned dependencies and Chromium, create .env from the example
	uv sync --python 3.11
	$(PYTHON) -m playwright install chromium
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example -- add your LLM_API_KEY")

lint: ## Ruff (lint + format check) and mypy --strict
	.venv/bin/ruff check app demo_app tests scripts
	.venv/bin/ruff format --check app demo_app tests scripts
	.venv/bin/mypy app demo_app

format: ## Auto-format
	.venv/bin/ruff format app demo_app tests scripts
	.venv/bin/ruff check --fix app demo_app tests scripts

test: ## All tests except the live-LLM test (browser tests run headless)
	$(PYTHON) -m pytest -m "not live_llm" -q

test-unit: ## Fast unit tests only
	$(PYTHON) -m pytest tests/unit -q

test-integration: ## Browser + demo app integration tests
	$(PYTHON) -m pytest tests/integration -q

test-e2e: ## Scripted discovery -> artifact -> replay
	$(PYTHON) -m pytest tests/e2e -q -m "not live_llm"

test-live: ## Genuine LLM discovery test (needs LLM_API_KEY)
	$(PYTHON) -m pytest tests/e2e -q -m live_llm

demo: ## Start the LegacyCore demo application (foreground, port $(DEMO_APP_PORT))
	$(CLI) demo start

discover: ## Genuine LLM discovery of the savings-balance lookup (needs LLM_API_KEY and `make demo` running)
	$(CLI) discover \
	  --goal "Look up member 12345 and read their current savings balance" \
	  --url $(DEMO_APP_URL)/login --name member_savings_lookup \
	  --input member_id=12345 $(LOOKUP_INPUTS) --sensitive access_code

discover-subaccount: ## Genuine LLM discovery of the sub-account opening flow (irreversible step -> human approval via console)
	$(CLI) discover \
	  --goal "Look up member 12345, open a new Savings sub-account with nickname Emergency Fund, and reach the confirmation screen" \
	  --url $(DEMO_APP_URL)/login --name open_savings_subaccount \
	  --input member_id=12345 --input nickname="Emergency Fund" $(SUPERVISOR_INPUTS) --sensitive access_code

replay: ## Deterministic replay (no LLM): member 12345 -> SUCCESS with savings_balance
	$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=12345 $(LOOKUP_INPUTS) --escalation none

replay-not-found: ## Business outcome: member 99999 -> BUSINESS_OUTCOME / MEMBER_NOT_FOUND
	-$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=99999 $(LOOKUP_INPUTS) --escalation none

replay-invalid: ## Caller error: member_id=abc rejected before the browser is touched
	-$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=abc $(LOOKUP_INPUTS) --escalation none

replay-transient: ## Recoverable: one injected core outage is retried and the run still succeeds
	$(CLI) demo inject --flag transient_search_failures=1
	$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=12345 $(LOOKUP_INPUTS) --escalation none

replay-fail-checkpoint: ## Hard failure: injected application error -> HARD_FAILURE with screenshot evidence
	$(CLI) demo inject --flag app_error_on_search=true
	-$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=12345 $(LOOKUP_INPUTS) --escalation none --evidence-kind failure
	$(CLI) demo reset

replay-fail-ambiguous: ## Ambiguous locator (duplicate form) -> ESCALATED, no operator attached
	$(CLI) demo inject --flag duplicate_search_form=true
	-$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=12345 $(LOOKUP_INPUTS) --escalation none --evidence-kind failure
	$(CLI) demo reset

demo-escalation: ## Human handoff: ambiguous locator pauses the run; resolve it at http://127.0.0.1:8001
	$(CLI) demo inject --flag duplicate_search_form=true
	-$(CLI) replay --artifact $(LOOKUP_ARTIFACT) --input member_id=12345 $(LOOKUP_INPUTS) --escalation console
	$(CLI) demo reset

demo-approval: ## Irreversible step: sub-account confirm waits for approval at http://127.0.0.1:8001
	$(CLI) replay --artifact $(SUBACCOUNT_ARTIFACT) --input member_id=12345 --input nickname="Emergency Fund" $(SUPERVISOR_INPUTS) --escalation console

evidence: ## Genuine LLM discovery + every replay/failure/handoff scenario -> evidence/ (needs LLM_API_KEY; starts the demo app itself if needed)
	$(PYTHON) scripts/generate_evidence.py --with-subaccount

evidence-check: ## Dry run of the evidence pipeline with the scripted model stand-in (no network), into /tmp
	$(PYTHON) scripts/generate_evidence.py --planner scripted --with-subaccount --evidence-root /tmp/cua-evidence-check --artifacts-dir /tmp/cua-artifacts-check

inspect: ## Print the saved artifacts in human-readable form
	$(CLI) inspect-artifact --artifact $(LOOKUP_ARTIFACT)
	@test -f $(SUBACCOUNT_ARTIFACT) && $(CLI) inspect-artifact --artifact $(SUBACCOUNT_ARTIFACT) || true

clean-evidence: ## Remove locally generated evidence that is not committed
	rm -rf evidence/_scratch
