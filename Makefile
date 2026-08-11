# h2o — development and deployment entrypoints.
#
# `check` is the only offline target. Everything else talks to real AWS: there
# are no local adapters and no mock layer, so `dev-api` and `dev-agent` need
# credentials (ADR-007).

export AWS_PROFILE ?= personal
ENV     ?= prod
REGION  ?= eu-west-1
STACK    = h2o-$(ENV)
CFN      = infra/cloudformation

.PHONY: help install lock lint format typecheck test check check-vocab check-corpus cfn-lint

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- development

install:  ## Sync the uv workspace (all members, dev group included)
	uv sync --all-packages

lock:  ## Re-resolve the shared lockfile
	uv lock

lint:  ## Ruff check + format check
	uv run ruff check .
	uv run ruff format --check .

format:  ## Apply Ruff formatting and autofixes
	uv run ruff check --fix .
	uv run ruff format .

typecheck:  ## Mypy over the source trees
	uv run mypy packages/h2o_core/src

test:  ## Every Python suite. Fakes only: no AWS calls, no network.
	uv run pytest

# Validates the seed vocabulary against the SHACL shapes in vocab/shapes/
# (ADR-005), plus resolver parity, the two deliberate-gap assertions, and the
# ADR-003 OTEL mapping table executed against the recorded fixture.
check-vocab:  ## SHACL gate, resolver parity, deliberate gaps, OTEL mapping table
	uv run python scripts/check_vocab.py

# Checks the document corpus against what the ADRs claim it contains: the
# registry agrees with the directory, the seeded contradictions are really
# there, the seeded gap really resolves to nothing, and the HTML document
# really exercises the lossless de-markup rule (ADR-002).
check-corpus:  ## Registry, seeded contradictions, gap counts, the HTML rule
	uv run python scripts/check_corpus.py

cfn-lint:  ## Validate every CloudFormation template
	@test -d $(CFN) && uv run cfn-lint $(CFN)/*.yaml || echo "  (no templates yet)"

check: lint typecheck test check-vocab check-corpus cfn-lint  ## Everything CI runs. Fully offline.
