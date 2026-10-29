# Day-to-day commands. Everything runs through uv (https://docs.astral.sh/uv/) with the optional
# extras installed, exactly like CI; set UV= to run the tools from an activated virtualenv
# instead (make UV= test).
UV ?= uv run --all-extras
UVX ?= uvx

.PHONY: help sync test coverage lint format typecheck schemas schemas-check build check start stop clean

help:            ## show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

sync:            ## create .venv with the receiver, its extras and the dev tools
	uv sync --all-extras

test:            ## run the test suite
	$(UV) pytest

coverage:        ## run the test suite with a line and branch coverage report
	$(UV) pytest --cov --cov-report=term-missing

lint:            ## ruff check, formatting check and shellcheck on tools/
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UVX) --from shellcheck-py shellcheck tools/*.sh

format:          ## fix lint and formatting in place
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck:       ## mypy (strict) on src/
	$(UV) mypy

schemas:         ## recompile the hand-written protobuf schemas
	$(UV) python -m stadiacast.schema_compiler

schemas-check:   ## fail when a compiled schema is stale
	$(UV) python -m stadiacast.schema_compiler --check

build:           ## build the sdist and the wheel into dist/
	uv build

check: lint typecheck schemas-check test   ## everything CI runs

start:           ## start the receiver with the proven preset (tools/start_receiver.sh)
	tools/start_receiver.sh

stop:            ## stop the receiver and its helpers
	tools/stop_receiver.sh

clean:           ## remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml htmlcov dist build src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
