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

- **HTML is cited against its de-marked-up text.** A reader of `<div class="price">&pound;7.99</div>` sees `£7.99`, and that is what any extractor quotes, so a byte comparison against the raw markup rejects a *correct* quotation as a fabrication and silently drops the fact. HTML documents are extracted from, chunked from, and cited against the flattened text, with `line_range` mapped back to the original file. This is safe precisely because the transform **invents no content**: every non-whitespace character in it came from the document or from an entity the document wrote, in order, with nothing added between them.

  **Amendment, M5: the rule is invent-no-content, not byte-preservation.** It read "introduces no character the document did not contain", and the deployed console showed what holding it to the letter costs: `<th>Supply pressure</th><td>1.5 &ndash; 6.0 bar</td>` flattened to `Supply pressure1.5 – 6.0 bar`. No reader of that table has ever seen `pressure1.5`, so the rule was failing the standard set in the sentence directly above it. It was not only a display fault either — chunks are built from this text and handed to the extractor, so the model was deciding `subject` and `value` from the glue, and the corpus-coverage check matches `" carbon filter "`, which `carbon filter6 months` does not contain.

  Byte-preservation was never actually the standard. `chunking.read_source` has always stripped each line's indentation and dropped blank lines outright, which deletes characters the document *did* contain. What the rule exists for is the line against OCR repair below: rewriting `ug`→`µg` claims a datasheet printed a character it never printed, and the verbatim gate would certify that fabrication. A space at a box boundary claims nothing — it renders a boundary the document really had, where it really had it.

  So a **block-level** tag flattens to a space and an **inline** one still flattens to nothing: `<b>£7</b>.99` is `£7.99`, because a separator there would break a word the reader sees whole. Two adjacent block tags emit one space, never two — `chunking.locate`'s fallback searches a whitespace-collapsed copy of the text, and a run would shift every offset it returns.

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

#### Amendment, M5: one model call before the cascade, at query time only

*The cascade above is unchanged for ingestion. This amends what happens when the caller is a person typing a question.*

Stages 1 and 2 are a dictionary, so they are exact by construction: `installtion` is not `installation` and never will be. Stage 3 cannot rescue it either — a character-level typo shreds subword tokenisation, and the measurements in `retrieval._worth_reporting` already record that similarity on this vocabulary points the wrong way. The deployed console therefore answered a one-keystroke typo with an honest refusal about a term the documents use on every page, and had no route at all for a question asked in another language. Edit distance would fix the first and is structurally incapable of the second: `bouteille de gaz` is not a misspelling of anything.

So the read path may call a model **before** resolution, subject to four constraints. Each is enforced by code or by a test, not by intention.

**It is blind to the vocabulary.** Its prompt contains the question and nothing else — no labels, no shortlist, no concept ids. This is the whole safety argument and it is not a matter of degree. `installtion` is a *misspelling* of `Installation`; `gas bottle` is a *synonym* of `CO₂ Cylinder`, semantically correct, and precisely the altLabel [ADR-004](004-vocabulary-gap-queue.md) requires a human to author and the model only to report. A model shown both will map the second and be right to, and the gap entry this platform's central claim rests on disappears. A model shown only the question cannot: `gas bottle` is correctly-spelled English and passes through untouched. `sanitise.aliases()` accordingly takes a question and a client, and there is no parameter through which an index could arrive — the same unrepresentable-otherwise move as `resolve_instance` above. A test asserts no label from the shipped vocabulary appears anywhere in the request, and it first failed on the prompt's own worked example.

**It returns an alias map, never a rewritten question.** Only the lookup key changes; the surface form is carried through untouched. It is load-bearing three ways: it is the left-hand side of the chip, and "installation → Installation" tells a reader nothing; it is what a gap entry quotes back to a curator as evidence; and it is what the queue merges on.

**It may change how a term is spelled or what language it is in. It may not change what it refers to.** No expanded abbreviations, no colloquial-to-technical substitution, no singular/plural, nothing requiring knowledge of the subject matter.

**It runs only on the miss path.** A question whose every phrase resolved never pays for it. A question in another language resolves nothing and always does, which is the case it exists for.

**What this actually does, measured against Nova 2 Lite rather than hoped for.** Typo correction is reliable. A single foreign word becoming an English term is reliable, including one word becoming two (`koolstoffilter` → `carbon filter`). A **multi-word foreign term is not**: across five prompt formulations it came back word by word (`bouteille` → `bottle`, `gaz` → `gas`, which never compose into a phrase the resolver looks up), or as the whole question in one entry, or with a substituted English term rather than a translated one — `bouteille de gaz` → `gas cylinder`, which is the synonym move this design exists to refuse. One formulation held, by naming that exact pair in the prompt, which is a model echoing an example rather than following an instruction and which put a subject-matter term into a prompt that must hold none.

So the honest scope is **typos and single-word foreign terms**. A multi-word foreign term files an imprecise queue entry, or several; it cannot resolve wrongly, because `retrieval._prune_aliases` refuses any multi-word alias that would make a term resolve. That asymmetry is the second of two vocabulary-side guards and it is bought with the `gas cylinder` observation: a single-word original leaves no room for phrase-level reinterpretation, and the multilingual case never needed a resolution in the first place — what it needs is the *miss* filed under the English form. `scripts/check_sanitiser.py` asks a live model all of this and gates on the seeded gaps.

Ingestion passes no alias map and keeps the four stages above, deliberately. The stage a resolution records lands in the facts graph as `h2o:resolvedBy`, and a claim asserting it matched exactly when a model corrected the spelling first would be false in the graph. It is also the rule stated in step 3: **reject, do not repair**. A document that spells a term wrong is a curator's `skos:hiddenLabel` decision — the seed vocabulary now carries some — not something the pipeline patches on the way past.

One consequence for [ADR-004](004-vocabulary-gap-queue.md)'s merge key, taken deliberately: when an alias translated a term, the queue entry merges and displays on the English form, and the words actually typed become its variant. `bouteille de gaz` and `gas bottle` are one gap in an English vocabulary, and two entries would split the count the console orders by. The evidence text stays the verbatim question either way.

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
- **Re-ingestion is idempotent** on `(source_file, doc_version)`, and it is worth saying how. For an unchanged reading it falls out of the data model: the claim IRI is a content hash and RDF is a set. That covers less than it sounds, because the hash has six fields and `snippet` is not one of them — a document read *differently* comes back as the same claim wearing a second, contradictory snippet, and every consumer does a single-valued read. The de-markup amendment above moved exactly that. So a document is now retracted before it is re-read (`facts.retract_document`), which is the move [ADR-005](005-governance-and-downstream-orchestration.md)'s fan-out already makes when it restates a claim: clearing beats patching, because the interesting case is always the triple that should no longer be there. Idempotent now means the graph describes the document, rather than the union of every way the document has been read.

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
