# ADR-002: Ingestion Against the Vocabulary

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-001](001-human-authored-skos-vocabulary.md), [ADR-004](004-vocabulary-gap-queue.md), [ADR-005](005-governance-and-downstream-orchestration.md), [ADR-006](006-vocabulary-console-and-chat.md)

## Context

[ADR-001](001-human-authored-skos-vocabulary.md) establishes a human-authored SKOS vocabulary as a precondition. This ADR covers what happens next: documents arrive (installation manuals, service bulletins, spec sheets, support FAQs) and must become queryable knowledge without becoming unreliable knowledge.

Two properties of the source material drive every decision here.

**The sources disagree.** A manual revised in 2023 says the carbon filter is replaced every six months; a 2024 service bulletin says four. A spec sheet gives a flow rate the support FAQ contradicts. This is not data corruption to be cleaned up. It is the actual state of the documentation, and the people who need to fix it are exactly the people who need to see it. **Reconciling contradiction is this pipeline's job**, and reconciling does not mean choosing.

**The sources name things inconsistently.** That is what the vocabulary is for. Every mention is resolved against it, and a mention that resolves to nothing is a signal, not a failure to suppress.

The principle inherited from the platform's design: **the system records what is evidenced, not what is true.** Determining truth is a curator's job; the pipeline's job is to make the evidence, and the disagreement, impossible to miss.

## Decision

Ingestion is a **deterministic pipeline that calls a language model inside exactly one step**. This is the mirror image of the chat agent, which is a language model that calls deterministic tools. Never one prompt over a corpus; never a model free-writing the graph.

A run is triggered by `POST /ingest` and proceeds in six steps.

### 1. Register

A document registry row in DynamoDB records `filename`, `doc_type`, `doc_version`, validity window, and format. Unknown documents are **registered and then ingested**, never guessed at and never silently skipped. The registry is the successor to a hardcoded corpus manifest: same explicitness, now data.

### 2. Normalize → chunk → embed → `PutVectors`

Normalisation exists for embedding quality only. **The stored `snippet` is the original text**, so citations remain verbatim. Two rules carry forward because both were learned the hard way:

- **HTML is cited against its de-marked-up text.** A reader of `<div class="price">&pound;7.99</div>` sees `£7.99`, and that is what any extractor quotes, so a byte comparison against the raw markup rejects a *correct* quotation as a fabrication and silently drops the fact. HTML documents are extracted from, chunked from, and cited against the flattened text, with `line_range` mapped back to the original file. This is safe precisely because the transform is **lossless**: it introduces no character the document did not contain.

  **An HTML comment is not the document.** De-markup drops comments *before* it drops tags, because a comment is authoring scaffolding — a note to the next editor, a stale price, a block someone commented out rather than deleted — and it is not text the document says. Leaving comments in the citable text would let a model quote one and have the verbatim gate certify it as genuine source material: the gate would be doing exactly its job and still passing a fabrication, which is the worst shape a check can take. Comments are replaced by their own newlines rather than removed, so `line_range` still points where a human would look.
- **OCR repairs are never cited.** Rewriting `ug`→`µg` or `Vitam1n`→`Vitamin` improves embedding quality, but citing repaired text would claim a datasheet printed a unit it never printed. Repairs stay embedding-only; those snippets remain byte-exact.

Chunks are section-aware (~300–500 tokens, split on headings). Vector metadata carries `source_file`, `doc_type`, `doc_version`, and the resolved `concept` as filterable keys, with `snippet` and `line_range` as payload.

### 3. Extract

Nova 2 Lite, forced tool call against a strict schema, one row per fact:

```
{ subject, predicate, object, unit, source_file, doc_version,
  line_range, snippet, confidence }
```

The **verbatim-snippet gate** is mandatory: `snippet` must be an exact substring of the source text (per the HTML rule above). **Reject, do not repair.** A row that fails the check is persisted as a `Rejection` record with the offending text, not silently dropped and not "fixed" into passing. A fact the model knows but cannot evidence is a rejection, by design.

**The unit of extraction is a passage, never a line.** The model is given a whole chunk, and a mention that a hard wrap splits across two lines — `the gas\nbottle yourself` in the support FAQ — is one mention, because that is what a reader sees. This sounds like an implementation detail and is not: a line-oriented reader finds eleven mentions of `gas bottle` in the corpus where a passage-oriented one finds twelve, and the missing twelfth is a held claim that never gets recovered when the vocabulary later gains the term. The verbatim gate does not protect against this, because the eleven it does see all quote correctly. Only the reading unit does.

### 4. Resolve mentions against the published vocabulary

A deterministic cascade, per mention:

1. Normalise (case, whitespace, punctuation, diacritics).
2. Exact match against `prefLabel`, `altLabel`, `hiddenLabel` in the published graph. This is expected to catch the large majority, costs nothing, and is fully explainable.
3. Embedding similarity over concept labels, producing a ranked shortlist.
4. **Abstain.** If nothing clears the threshold, the mention is unresolved.

**An unresolved mention holds its claim rather than dropping it.** The claim persists with `status = held`, and a gap record is written ([ADR-004](004-vocabulary-gap-queue.md)) carrying the surface form, the verbatim sentence, and `source_file:line_range`. When the vocabulary later gains the term, the held claim is re-resolved and goes live ([ADR-005](005-governance-and-downstream-orchestration.md)), and the document does not need re-reading.

Every resolution records which cascade stage matched and at what score, so any concept link can be explained after the fact.

Instance identifiers such as machine serials and site IDs resolve by **exact match only**. No fuzzy instance linking: a wrong concept link mislabels one claim, whereas a wrong instance link tells the wrong customer their filter is due.

### 5. Detect contradictions, deterministically and with no model

Group claims by `(subject, predicate)`, normalise units before comparing, and flag any group whose values disagree.

Unit normalisation is dimension-safe by construction: µg, mg, and IU are not interconvertible without a substance-specific factor, and per-unit is not per-serving. Comparing across dimensions is an error, not a conversion.

### 6. Persist

Claims with full provenance, conflict flags, gap records, rejections, and the ingest-run row.

## Contradictions: record both, flag, never resolve

When two sources disagree, **both claims persist**, each carrying its own `source_file`, `doc_version`, `line_range`, and verbatim snippet. A `conflict` flag attaches to the group. Three consumers see it:

- **The agent** is required by system prompt to surface every side, attributed: *"The installation manual (v3, lines 412–415) says six months; the 2024 service bulletin says four."* It may note which source is more authoritative; it may not silently pick one.
- **The console** lists conflicts as a filterable queue ([ADR-006](006-vocabulary-console-and-chat.md)), which is how a documentation owner finds out their manuals disagree.
- **`get_facts`** returns the flag alongside the values, so no consumer can accidentally read one value as settled.

**Why not source precedence.** Ranking sources (official manual > spec sheet > support article) is a few lines of code and lets the platform state one confident answer. It also permanently hides a real disagreement behind a plausible response. The disagreement is frequently the most valuable thing in the corpus: it means two documents in circulation tell technicians different things.

**Why not recency.** "Newest version wins" is right for a revised manual and wrong for two peer sources published the same quarter. Distinguishing the two cases requires validity windows we do not have for every document, and guessing wrong produces silent, confident error.

`doc_version` travels on every claim, so answers can always be attributed to a version, as in *"per manual v3"*. What we decline is **inferring supersession automatically**. A curator can mark a claim superseded; the pipeline will not.

## Consequences

### Benefits

- **Disagreement becomes visible and actionable** instead of being resolved into invisibility. The conflict queue is a documentation-quality report the client did not previously have.
- **Every fact carries its evidence.** Source file, version, line range, and a verbatim snippet, enforced by a substring check rather than by prompt instruction.
- **Unresolved mentions cost nothing permanently.** The claim is held, not lost, and is recovered automatically when the vocabulary catches up. This is what makes vocabulary work pay off visibly ([ADR-005](005-governance-and-downstream-orchestration.md)).
- **Conflict detection is code**, so it is testable, explainable, and identical on every run.
- **Re-ingestion is idempotent** on `(source_file, doc_version)`.

### Trade-offs

- **The agent's answers are longer and less decisive** when conflicts exist. We accept this: a two-sided answer with sources is more useful than a confident wrong one, and the alternative is a system that hides its own uncertainty.
- **Conflict volume may be high on a real corpus.** A demo with a seeded contradiction proves the mechanism; a client corpus may surface hundreds and need triage tooling we have not built.
- **Held claims are invisible until the vocabulary grows.** A user can ask a question whose answer exists in a held claim and be told it is not found. This is honest but unsatisfying, and it is the strongest argument for keeping the gap queue short.
- **The verbatim-snippet gate rejects correct facts** whose wording the model paraphrased. Recall is traded for the guarantee that every stored fact is quotable. Rejection records make the loss inspectable rather than silent.
- **Unit normalisation is subtle code** and easy to get wrong at the edges (undated documents, mixed dimensions, ranges). It needs its own test suite.

### Out of scope

No live ERP/CRM/IoT connectors; instance data arrives as versioned batch imports. No generic web ingestion; documents enter through the registry. No automatic supersession. No curator adjudication workflow for conflicts in this ADR; the console surfaces them, and resolving them is a documentation change at the source.

## References

- [ADR-001](001-human-authored-skos-vocabulary.md): the vocabulary this pipeline resolves against
- [ADR-004](004-vocabulary-gap-queue.md): where unresolved mentions go
- [ADR-005](005-governance-and-downstream-orchestration.md): how held claims are recovered on publish
- [ADR-006](006-vocabulary-console-and-chat.md): the conflict queue surface
- [Amazon S3 Vectors](https://aws.amazon.com/s3/features/vectors/)
- [Amazon Titan Text Embeddings V2](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)
