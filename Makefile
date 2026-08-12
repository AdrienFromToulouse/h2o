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

.PHONY: help install lock lint format typecheck test check check-vocab check-corpus cfn-lint \
        deploy-data-plane deploy-graph deploy-data deploy-telemetry deploy-orchestration \
        deploy-frontend outputs seed-graph seed-docs ingest gaps demo-reset dev-api dev-web \
        web-install web-check api-reqs sam-build \
        deploy-api clean

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
	uv run mypy packages/h2o_core/src apps/api/h2o_api

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

seed-graph: check-vocab  ## Load vocab/*.ttl into h2o:graph/published (gated first)
	H2O_ENV=$(ENV) AWS_REGION=$(REGION) uv run python scripts/seed_graph.py

# The manifest ships with the corpus rather than with the code, so the bucket
# is the source of truth for what has been made available to ingest -- and the
# worker reads registry.json from the same place it reads the documents.
seed-docs: check-corpus  ## Upload data/docs/ (documents + registry.json) to the raw-docs bucket
	aws s3 sync data/docs/ s3://h2o-$(ENV)-raw-docs/ --delete --region $(REGION)

# --------------------------------------------------------------- local serving
# These call real AWS. `check` is the only offline target.

dev-web:  ## Serve the console on :3000 (needs apps/frontend/.env.local)
	cd apps/frontend && pnpm install --silent && pnpm dev

web-install:  ## Install the console's dependencies
	cd apps/frontend && pnpm install

# Part of `check`, and offline: it renders the real components against fixtures
# and asserts ADR-006's no-jargon rule, which is the one claim that ADR makes
# about testability.
web-check:  ## Typecheck and test the console
	@test -d apps/frontend/node_modules || (cd apps/frontend && pnpm install --silent)
	cd apps/frontend && pnpm exec tsc --noEmit && pnpm exec vitest run

dev-api:  ## Serve the vocabulary API on :8085 (real S3, DynamoDB, Bedrock)
	cd apps/api && H2O_ENV=$(ENV) AWS_REGION=$(REGION) \
		uv run uvicorn h2o_api.app:app --reload --port 8085

# ------------------------------------------------------------------ packaging

api-reqs:  ## Stage the Lambda's third-party deps and the h2o_core wheel
	# The gate's shapes travel inside the wheel. Copied rather than checked in,
	# so vocab/shapes/ stays the one reviewed original (ADR-005).
	mkdir -p packages/h2o_core/src/h2o_core/shapes
	cp vocab/shapes/*.ttl packages/h2o_core/src/h2o_core/shapes/
	uv export --frozen --no-dev --no-emit-workspace --package api -o apps/api/requirements.txt
	@grep -q '^h2o-core' apps/api/requirements.txt \
		&& { echo "error: h2o-core must not be in requirements.txt (it ships as a wheel)"; exit 1; } \
		|| true
	rm -rf apps/api/vendor
	uv build --wheel packages/h2o_core -o apps/api/vendor

# SAM resolves samconfig.toml next to the template, and sam deploy reads
# .aws-sam/build/ relative to cwd, so build and deploy share a directory.
sam-build: api-reqs  ## Build the Lambda bundle (arm64 manylinux_2_28 wheels, no Docker)
	cd $(CFN) && sam build -t 40-api.yaml

deploy-api: sam-build  ## 40: build and deploy the vocabulary API
	cd $(CFN) && sam deploy --config-env $(ENV)

# ------------------------------------------------------------------- exercising
# The API is IAM-authorised, so these sign their requests. scripts/api.py is the
# same tool the README's end-to-end walkthrough is run with.

ingest:  ## Start an ingest run against the deployed API and poll it to completion
	H2O_ENV=$(ENV) AWS_REGION=$(REGION) uv run python scripts/api.py POST /ingest --wait

demo-reset:  ## Put the demo back to its starting state (the loop is one-shot otherwise)
	H2O_ENV=$(ENV) AWS_REGION=$(REGION) uv run python scripts/demo_reset.py

gaps:  ## Show the open gap queue, ordered by occurrences
	H2O_ENV=$(ENV) AWS_REGION=$(REGION) uv run python scripts/api.py GET /gaps

clean:  ## Remove build artefacts
	rm -rf $(CFN)/.aws-sam apps/api/vendor apps/api/requirements.txt .pytest_cache

check: lint typecheck test check-vocab check-corpus cfn-lint web-check  ## Everything CI runs. Fully offline.

# ----------------------------------------------------------------- deployment
#
# Stacks 00-30 hold no code and deploy on their own. 40-api and 50-agent deploy
# once there is something to ship (a Lambda bundle and a container image), and
# 60-frontend deploys last because it scopes its policy to the API's id.

deploy-data-plane: deploy-graph deploy-data deploy-telemetry deploy-orchestration  ## Stacks 00-30

deploy-graph:  ## 00: versioned N-Quads dataset bucket + advisory publish lock
	aws cloudformation deploy --template-file $(CFN)/00-graph.yaml \
		--stack-name $(STACK)-graph --parameter-overrides Environment=$(ENV) \
		--region $(REGION) --no-fail-on-empty-changeset

deploy-data:  ## 10: S3 Vectors bucket + both indexes, raw docs, four tables
	aws cloudformation deploy --template-file $(CFN)/10-data.yaml \
		--stack-name $(STACK)-data --parameter-overrides Environment=$(ENV) \
		--region $(REGION) --no-fail-on-empty-changeset

deploy-telemetry:  ## 20: telemetry bucket + concept-keyed fleet-signal store
	aws cloudformation deploy --template-file $(CFN)/20-telemetry.yaml \
		--stack-name $(STACK)-telemetry --parameter-overrides Environment=$(ENV) \
		--region $(REGION) --no-fail-on-empty-changeset

deploy-orchestration:  ## 30: EventBridge bus + publish fan-out state machine
	aws cloudformation deploy --template-file $(CFN)/30-orchestration.yaml \
		--stack-name $(STACK)-orchestration --parameter-overrides Environment=$(ENV) \
		--capabilities CAPABILITY_NAMED_IAM --region $(REGION) --no-fail-on-empty-changeset

deploy-frontend:  ## 60: scoped IAM user for the Vercel BFF (needs API_ID=...)
	@test -n "$(API_ID)" || { echo "usage: make deploy-frontend API_ID=<rest-api-id>"; exit 1; }
	aws cloudformation deploy --template-file $(CFN)/60-frontend.yaml \
		--stack-name $(STACK)-frontend \
		--parameter-overrides Environment=$(ENV) ApiId=$(API_ID) \
		--capabilities CAPABILITY_NAMED_IAM --region $(REGION) --no-fail-on-empty-changeset

# Stacks that have not been deployed yet are skipped rather than failing the
# target: this is how you find out what exists, so "not there" is an answer.
outputs:  ## Print every stack output, for filling .env.local
	@for s in graph data telemetry orchestration api agent frontend; do \
		aws cloudformation describe-stacks --stack-name $(STACK)-$$s --region $(REGION) \
			--query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output text 2>/dev/null \
			|| echo "  ($(STACK)-$$s not deployed)"; done
