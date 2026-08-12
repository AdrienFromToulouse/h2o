# h2o — working notes for agents

The product is **AquaKnow**; `h2o` is the repository and the resource prefix.

A knowledge platform for connected water dispensers, built on one claim: **a
human-authored SKOS vocabulary defines the entities; the model uses it and
reports what it is missing; a person decides what gets added.** The vocabulary is
a precondition, not an output — a vocabulary induced from a corpus inherits
exactly the inconsistencies it exists to fix.

## Read these first

- `docs/adrs/` — seven ADRs. They are the specification and they predate the
  code, deliberately. **If the code and an ADR disagree, that is a finding.**
  Amend the ADR in the same commit and say why; do not quietly diverge.
- `README.md` — the loop the whole system exists to demonstrate.
- `git log` — decisions live in commit messages, including several the code
  cannot express. Read them before assuming something is arbitrary.

## Layout

```
vocab/            the hand-written SKOS vocabulary + SHACL shapes (the gate)
data/docs/        six documents with two seeded contradictions and a seeded gap
packages/h2o_core the library: graph, resolve, ingest, publish, fan-out
apps/api          FastAPI on Lambda — HTTP, ingest worker, fan-out steps
apps/frontend     the console (Next.js, App Router)
infra/cloudformation  seven stacks, 00–60
scripts/          check_vocab, check_corpus, seed_graph, demo_reset, api
```

## Commands

```bash
make check        # everything CI runs, fully offline (~2 min; pySHACL is most of it)
make dev-web      # the console on :3000, against real AWS
make deploy-api   # build + deploy the Lambda
make ingest       # ingest the corpus through Bedrock, poll to completion
make demo-reset   # put the demonstrator back to its starting state
uv run python scripts/api.py GET /gaps    # the API is IAM-signed; this signs
```

AWS is **profile `personal`, eu-west-1, `H2O_ENV=prod`**. There are no local
adapters and no mock layer: `make check` is the only offline target, and fakes
exist only in tests.

## Conventions that are load-bearing

**Comments explain *why*, and especially why-not.** Several in this tree record
a bug that cost hours; they are not decoration and should not be summarised
away. If you remove one, you are removing the reason.

**A fake that is more permissive than the real service is worse than no fake.**
`packages/h2o_core/tests/fakes.py` reproduces the specific refusals AWS makes —
DynamoDB rejecting a nested `ADD`, S3 refusing a stale `If-Match`. Tests assert
the refusal happens.

**Placeholders in SPARQL bind RDF terms only.** A different query shape is a
different file in `src/h2o_core/sparql/`. `render()` refuses a plain `str`. Never
generate SPARQL from user or model input.

**One dispatcher.** `apps/api/h2o_api/dispatch.py` is the only place that reads
`IS_LAMBDA`, and a test greps the tree to keep it so. It decides *who calls* the
work, never *what the work does*.

**The API is IAM-authorised.** Nothing reaches it unsigned. The console signs
server-side; credentials never reach a browser and are read from `H2O_AWS_*`,
never `AWS_*` (on Vercel those hold Vercel's own).

**Plain language reaches people.** No `skos:`, no IRI, no validation code in
anything a curator sees. `apps/frontend/lib/labels.ts` holds the rule as data and
`tests/no-jargon.test.tsx` enforces it.

## Verification discipline

Demo invariants are asserted **twice**: in `scripts/check_*.py` against the raw
artefacts, and through the library in the test suite. If they disagree, the
library is wrong.

A third source now exists — the deployed system — and it has repeatedly
disagreed with both. When it does, **record what actually happened rather than
tuning until the number matches.** Several such findings are in the commit log
and are among the most valuable things in this repository.

## Known open questions

These are measured, not speculative. Do not "fix" them without deciding first.

- **The shortlist ranks poorly.** Titan embeds short labels, so it returns
  lexical similarity. `CO₂ Cylinder` ranks 2nd at 0.28 for "gas bottle", behind
  `Single-Use Bottles Avoided` — which shares the word. `limescale` scores 0.170
  while the verb `replace` scores 0.393. The score is not weak evidence of
  aboutness; on this vocabulary it points the wrong way. Filtering is therefore
  structural (`retrieval._worth_reporting`), not a threshold. It is also no
  longer shown in chat: a curator choosing an attachment point can use five
  candidates, and the same list beside an answer reads as "did you mean".
- **Mentions are not claims.** `check_corpus.py` counts regex mentions; the
  pipeline counts claims the extractor chose to emit and could evidence. The
  README's "12 mentions" is the former. The counts also move between runs.
- **Chat gap granularity.** A question about "the gas bottle pressure" records
  `gas bottle pressure`, which does not merge with ingestion's `gas bottle`.
  Honest, but it splits the demo's headline entry.

## Milestones

M0–M6 and the console are done and deployed; `demo-reset` makes the loop
repeatable. Chat answers from the documents with citations and two-sided
conflicts. **Not done:** the agent on AgentCore (chat currently runs in the API
Lambda and returns its events as an array, because API Gateway buffers), the
OTEL telemetry loop (M7), and `docs/operations/`.
