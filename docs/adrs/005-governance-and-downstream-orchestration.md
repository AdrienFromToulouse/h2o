# ADR-005: Governance and Downstream Orchestration

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-001](001-human-authored-skos-vocabulary.md), [ADR-002](002-ingestion-against-the-vocabulary.md), [ADR-004](004-vocabulary-gap-queue.md), [ADR-006](006-vocabulary-console-and-chat.md), [ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)

## Context

A vocabulary that cannot change safely will not be changed at all, and one that changes without consequence is a form without a function. This ADR covers both halves of that problem.

**Safety.** The vocabulary is a governed asset. A change must be attributable, reversible, and validated, because a bad `broader` edge silently mis-parents a subtree, and a duplicated label silently breaks resolution for both concepts involved.

**Consequence.** This is the part that is usually missed. Editing a `prefLabel` in a form and seeing a success toast changes nothing a user will notice. The change only becomes real when the resolver index is rebuilt, held claims are re-resolved, and affected documents are re-indexed. **Publishing is the beginning of the work, not the end of it**, and the console's second job is to run that work and show what it did.

## Decision

### 1. Named graphs

One store, four roles.

| Graph | Holds |
| --- | --- |
| `h2o:graph/published` | The live vocabulary; the only graph the agent reads |
| `h2o:graph/draft` | In-progress edits, one working copy per concept under edit |
| `h2o:graph/history/{concept}/{n}` | Frozen prior versions, immutable |
| `h2o:graph/facts` | Extracted claims and flags ([ADR-002](002-ingestion-against-the-vocabulary.md)) |

Per-concept provenance lives on the concept: `owl:versionInfo`, `dct:modified`, `dct:contributor`, `skos:changeNote` (the expert's own reason, in their words), and `prov:wasRevisionOf` pointing at the history graph it replaced. Every publish additionally writes an audit row: who, when, which concept, the before/after diff, and the originating gap id if there was one.

### 2. Publish is one atomic update

Copy the concept's current triples into a fresh `history/{concept}/{n}` graph, delete them from `published`, insert the reviewed version, bump `versionInfo`.

On the default runtime ([ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)) the SPARQL update runs in-process against the loaded store and persists as **one conditional S3 write**, so the publish either lands as a new object version or does not land at all. On Neptune it is a single transactional update request. A test asserts on both backends that a mid-publish failure leaves `published` byte-identical.

**Concepts are never hard-deleted.** Deprecation marks the concept and carries a pointer to its replacement, so existing claims keep resolving and the agent can say *"that term was retired in favour of X"* rather than failing to find it. A vocabulary with dangling references is worse than one with tombstones.

### 3. The integrity gate: SHACL shapes, plus one thing SHACL cannot do

The gate runs before **any** publish, from either the console or a git bulk load. No model involvement.

**SHACL is the mechanism**, not hand-written queries. The shapes live in `vocab/shapes/skos-integrity.ttl` and are reviewed in git exactly like the vocabulary they constrain, which is the point: **the rules become as governable as the data.** Conformance level is SHACL Core plus SHACL-SPARQL. SHACL Advanced Features (`sh:rule`) are not used and must not be enabled, because rules derive triples ([ADR-001](001-human-authored-skos-vocabulary.md)). Validation itself is side-effect free by specification: the data graph must remain immutable throughout.

| # | Check | Expressed as |
| --- | --- | --- |
| 1 | Exactly one `skos:prefLabel` per language | `sh:uniqueLang` + `sh:minCount` (Core) |
| 2 | No label collision within a scheme | `sh:sparql` (see caveat below) |
| 3 | `skos:broader` forms no cycle | `sh:sparql` with a `skos:broader+` path |
| 4 | Every `broader` / `related` / `*Match` target exists | `sh:class` + `sh:nodeKind` (Core) |
| 5 | A string is not both `prefLabel` and `altLabel` of one concept | `sh:disjoint` (Core) |
| 6 | Orphan: no `broader`, not a `topConceptOf` | `sh:sparql`, `sh:severity sh:Warning` |

Checks 1–5 exist because each corresponds to a **silent** failure. Two concepts sharing a label means resolution picks one arbitrarily and the other becomes unreachable. A `broader` cycle makes `expand_concept` non-terminating. A dangling `related` produces a link the console renders as a dead end. None of these throw; they just quietly make answers worse.

Checks 3 and 6 are SHACL-SPARQL rather than Core because **SHACL Core cannot express recursion** (the specification leaves validation with recursive shapes undefined) and a cycle is inherently transitive. A SPARQL-based constraint with a `+` property path is the standard way to say it.

**The caveat on check 2, which is why Python code remains in the gate.** SPARQL can fold case and nothing more. The resolver normalises further: NFKD decomposition, accent stripping, punctuation removal. So `"CO₂ Cylinder"` and `"CO2 Cylinder"` collide *for the resolver* and look distinct to a `LCASE` comparison. The SHACL shape catches the common case; a **resolver-parity check in Python**, running the real normalisation function over every label, catches the rest. Anything that decides whether two labels are "the same" must use the function the published index is actually built with, not an approximation of it. This is not a gap in SHACL so much as a reminder that the resolver's notion of identity is code, and only that code can be authoritative about it.

**`sh:message` carries the user-facing text.** Each constraint declares its own plain sentence, such as *"Filter Cartridge is already an alternative term for Sediment Filter"*, so the wording the expert sees lives beside the rule rather than in a lookup table in the UI. The console renders `sh:resultMessage` verbatim. Never SPARQL, never SKOS jargon, never a validation code ([ADR-006](006-vocabulary-console-and-chat.md)).

Violations block a publish; warnings are reported and do not.

### 4. Impact preview, computed before Save rather than after

A deterministic query across the gap queue, the fact graph, and the document registry, answering:

- **How many held mentions would now resolve**, and across how many documents.
- **Which documents would be re-indexed.**
- **Which concepts gain or lose a child**, if `broader` changed.
- **Whether any label collides**: the gate, run early and non-fatally so the expert learns before typing a change note rather than after.

Rendered as one sentence in the review card:

> *Adding this alternative term will resolve 12 mentions across 3 documents.*

This is the single feature that makes the console more than a form. It converts an abstract edit into a visible consequence, and it is what a domain expert needs in order to judge whether a change is worth making.

### 5. Publish fan-out

Publishing emits a `VocabularyPublished` event (concept IRI, new version, the diff) onto EventBridge. A **Step Functions** state machine runs the consequences in order:

1. **Rebuild the resolver index.** Recompile normalised labels → concept IRIs, publish to S3, bump the watermark. The agent picks it up within seconds. *Without this step nothing else matters*, which is why it is first and why its failure aborts the run.
2. **Re-resolve the gap backlog.** Replay held mentions against the new vocabulary; newly matching ones attach to their pending claims, which go live. Gap records close with a pointer to the publish that closed them.
3. **Targeted re-ingest and re-index.** Only documents that contained the affected surface forms, identified from the gap records. Re-extract and re-embed so retrieval filters reflect the new concept. **Not** a full corpus re-run.
4. **Record the run.** Counts per step, so the console can show *"published 12:04 · 43 mentions resolved · 6 documents reindexed"*.

The API exposes the run for polling; the console reuses the existing ingest-run polling hook pattern (2.5s interval, resumes an in-flight run after reload) rather than inventing a second one.

**Why Step Functions and not a Lambda chain.** This is a genuine multi-step workflow with retries, partial failure, ordering constraints, and a status a human is actively watching. A chain would reimplement all of that, badly, in application code. The cost is one more service in the CloudFormation surface, accepted deliberately.

**Why the resolver index is a build artefact and not a live query.** Resolution happens on every user turn and every ingested mention; it must be an in-process dictionary lookup, not a graph round trip. The index is compiled from the published graph and versioned with a watermark, so staleness is bounded and observable rather than possible and invisible.

## Consequences

### Benefits

- **A vocabulary change has visible effect.** The loop closes: an expert adds a term, and a previously unanswerable question becomes answerable, with counts proving it.
- **Every change is attributable and reversible.** Author, reason, timestamp, prior version, and originating evidence, all recorded without the expert filling in a form beyond the change note.
- **The integrity gate catches the silent failures**, which are the only ones that matter. A broken vocabulary does not throw, it just quietly answers worse.
- **Git and the console are equally safe.** Both paths run the same gate, so a bulk Turtle edit cannot bypass validation that a UI edit enforces.
- **Held claims are recovered automatically.** Vocabulary work pays off across the whole existing corpus, not just future ingestion.

### Trade-offs

- **The gate is now two mechanisms, not one.** SHACL owns the structural constraints and Python owns resolver parity, so a reviewer must know which file to read for which rule. We accept the split because the alternative is worse: approximating the resolver's normalisation in SPARQL would produce a gate that passes while the index it protects collides.
- **SHACL validation pulls a validator into the runtime.** The publish path needs pySHACL, and therefore rdflib, in the API Lambda ([ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)). That is real package weight for a check that runs on a human action, not on every request.
- **History graphs grow unbounded.** Every publish adds one. Acceptable at vocabulary scale, and pruning would need its own retention decision.
- **The resolver index has a staleness window** of seconds between publish and rebuild. Exposed as a watermark rather than hidden, so a stale answer is diagnosable.
- **Concept-level versioning is not scheme-level.** A multi-concept restructure, such as moving a subtree or splitting a concept, is not one atomic unit. The escape hatch is a batch publish sharing one transaction, documented but not the default path.
- **Targeted re-ingest can miss documents** whose relevance to a term is semantic rather than lexical: a document discussing gas cylinders without ever writing *"gas bottle"* is not re-indexed. A full re-ingest remains available as a manual action, and the run record states which documents were touched so the omission is inspectable.
- **The fan-out is a distributed process with partial-failure states.** A run that rebuilds the index and then fails to re-index leaves the system correct but incomplete. Step Functions makes this visible and retryable; it does not make it impossible.

### Out of scope

No approval workflow with multiple reviewers; one expert authors and publishes, and the audit trail records who. No scheduled or automatic publishing. No rollback UI in this ADR; history graphs and S3 object versions make rollback possible, and exposing it is a later decision. No retention policy for history graphs.

## References

- [SKOS Reference](https://www.w3.org/TR/skos-reference/)
- [AWS Step Functions](https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html)
- [Amazon EventBridge](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-what-is.html)
- [SHACL (W3C Recommendation)](https://www.w3.org/TR/shacl/): Core and SPARQL-based constraints; validation is side-effect free
- [pySHACL](https://github.com/RDFLib/pySHACL): the validator, with Advanced Features behind an explicit flag
- [ADR-001](001-human-authored-skos-vocabulary.md): the vocabulary being governed
- [ADR-004](004-vocabulary-gap-queue.md): what a publish typically closes
- [ADR-006](006-vocabulary-console-and-chat.md): the review card and run status
- [ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md): publish atomicity per backend
