# ADR-004: The Vocabulary Gap Queue

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-001](001-human-authored-skos-vocabulary.md), [ADR-002](002-ingestion-against-the-vocabulary.md), [ADR-003](003-otel-fleet-signals-to-skos.md), [ADR-005](005-governance-and-downstream-orchestration.md), [ADR-006](006-vocabulary-console-and-chat.md)

## Context

[ADR-001](001-human-authored-skos-vocabulary.md) states that humans author the vocabulary and that a language model never writes one. But a vocabulary authored once and never revisited goes stale: terminology shifts, product lines change, firmware ships new fault codes, and customers use words nobody anticipated.

The question this ADR answers is narrow and important: **how does a domain expert find out what is missing?**

Three unattractive answers exist. *Wait for someone to complain*, which is slow, and most gaps never generate a complaint. *Have the expert periodically re-read everything*, which does not scale past the first review. *Let a model propose new concepts*, which reintroduces exactly the model-authored vocabulary [ADR-001](001-human-authored-skos-vocabulary.md) forbids, with the added hazard that a plausible generated concept is harder to reject than an obvious gap.

The answer here is a **queue of evidenced gaps**. Three parts of the system already know when they failed to resolve something. Rather than each swallowing that failure, all three record it, and the records aggregate into one work list with the evidence attached.

The distinction that governs the whole design:

> A gap entry is a **report with a suggested attachment point**, not a drafted concept.

## Decision

### 1. Three sources, one queue

Gaps land in a DynamoDB table, `h2o-{env}-vocabulary-gaps`, keyed by normalised surface form so the three sources merge into one entry.

| Source | Produced when | Evidence recorded |
| --- | --- | --- |
| **Ingestion** ([ADR-002](002-ingestion-against-the-vocabulary.md)) | A mention resolves to nothing; its claim is held | `source_file:line_range`, the verbatim sentence, `doc_version` |
| **Chat** ([ADR-001](001-human-authored-skos-vocabulary.md)) | `resolve_concept` returns no-match | The verbatim user turn, session id, timestamp |
| **Telemetry** ([ADR-003](003-otel-fleet-signals-to-skos.md)) | An OTEL attribute value or instrument name maps to nothing | The OTLP record, occurrence count, affected devices |

The chat path deserves a note: the miss is recorded by **deterministic resolver code**, not by the agent calling a tool. The agent has no write capability of any kind ([ADR-001](001-human-authored-skos-vocabulary.md)), and a `report_gap` tool would be one the model could be prompted into misusing. Recording a miss is a side effect of resolution failing, which is not a decision the model gets to make.

### 2. Aggregation is deterministic

Per entry, computed in code with no model involvement:

- **Normalise** the surface form and merge across sources. The merge key is *not* the resolver's normalisation. The resolver's `normalise()` defines label identity for the published index ([ADR-005](005-governance-and-downstream-orchestration.md)), and it deliberately does no stemming, so `gas bottle` and `gas bottles` are two distinct labels to it. The queue wants them to be one entry. Teaching the resolver to stem in order to get that would change what "two labels collide" means and break the parity guarantee the integrity gate depends on, so the gap key is a separate function: the resolver's normalisation plus a plural fold, used for merging queue entries and never for resolving a mention.
- **Count** occurrences per source. Three chat turns and nine document mentions are one entry with two counts, not two entries.
- **Collect** verbatim evidence, capped and deduplicated, always with its locator.
- **Compute a suggested attachment point**: the nearest existing concepts by lexical distance and label embedding, returned as a ranked shortlist with scores.

The shortlist is the useful part. *"`gas bottle`, closest existing terms: CO₂ Cylinder (0.81), Mineral Cartridge (0.44)"* turns an unbounded authoring task into a yes/no judgement.

### 3. What the model may and may not do

At most **one constrained call**, which ranks the supplied shortlist and may **abstain**. That is the entire model involvement in vocabulary evolution.

It never writes a `prefLabel`. It never writes a `definition`. It never mints an IRI. It never proposes a parent outside the existing tree. It never selects a concept that was not in the shortlist it was given. Abstention is always a permitted outcome and is the default when nothing scores well.

The reason for confining it this far is not caution for its own sake. A generated concept, with its plausible label, fluent definition, and sensible-looking parent, is *harder to reject* than a raw gap. It arrives pre-justified, and reviewing it means arguing with a draft rather than exercising judgement. Presenting evidence and letting the expert author the term keeps the human decision genuinely human, and keeps authorship attributable.

### 4. Typed entries, rendered as plain questions

A closed set of types, so the console can render each as one specific question instead of a generic form.

| Type | Rendered as |
| --- | --- |
| `AddAltLabel` | "**gas bottle**: 3 chat turns, 12 document mentions. Closest term: **CO₂ Cylinder**." |
| `NewConcept` | "**scale_buildup**: 214 fleet events, matches nothing. Closest scheme: **Fault**." |
| `AddMapping` | "Signal **component.type=scale_inhibitor** looks like **Mineral Cartridge**." |
| `MergeDuplicate` | "**Filter Cartridge** and **Carbon Cartridge** resolve to different concepts but co-occur in 11 sentences." |
| `EditDefinition` | "**Sanitisation** was asked about 4 times and the answer was rated unhelpful." |

Every entry carries the verbatim evidence, per-source occurrence counts, the cascade stage and score behind the suggestion, the run that produced it, and a status: `open`, `actioned`, or `dismissed`.

### 5. Dismissal must suppress

A dismissal records a reason and **suppresses that surface form from re-entering the queue**. Without this the queue never converges: a term the expert has already judged irrelevant reappears every ingest run, and the queue becomes noise that nobody reads.

Suppression is per surface form, not permanent amnesty. A suppressed term whose occurrence count later jumps by an order of magnitude resurfaces once, because volume that changes by 100× is new information.

### 6. Actioning is not publishing

Actioning an entry opens the review card in the console with the evidence pinned beside it. **Nothing is written to the vocabulary.** The expert authors the term, and publishing runs the integrity gate and the downstream fan-out ([ADR-005](005-governance-and-downstream-orchestration.md)). One human decision, always, and it is an authoring decision rather than an approval click.

## Consequences

### Benefits

- **Gaps surface without anyone monitoring for them**, from three independent directions, at whatever scale each operates.
- **Evidence makes the judgement cheap.** An expert deciding about *"gas bottle"* sees the actual sentences and the actual user turns, not an abstract term.
- **The queue is a coverage metric.** Its length and age measure how well the vocabulary matches reality, a number that did not previously exist.
- **Authorship stays human and attributable.** Every concept has a person behind it, which matters when the vocabulary is a governed asset.
- **Cross-source aggregation reveals importance.** A term appearing in documents, chat, *and* telemetry is more clearly real than one appearing once.

### Trade-offs

- **The queue needs a worker.** An unworked queue is worse than none: it accumulates, its signal decays, and its existence implies a diligence that is not happening. This is the platform's real operating cost and it is organisational.
- **Suppression can hide a real gap.** A dismissal made in haste buries a term until its volume jumps. The escape hatch is deliberate but coarse.
- **The merge key is deliberately looser than the resolver's.** Surface forms differing only in punctuation or plurality become one queue entry, which is usually right and occasionally conflates two distinct terms. It also means the queue's notion of "the same term" and the resolver's are not identical by design, and anyone reading either has to know which one they are looking at.
- **The shortlist can anchor the expert.** Showing *"closest term: CO₂ Cylinder"* makes accepting that attachment easier than considering a new concept. Mitigated by always offering "none of these" as a first-class option in the console rather than a fallback.
- **Chat gaps are noisier than the other two.** Users type typos, jokes, and unrelated questions. Frequency thresholds filter most of it, at the cost of delaying genuinely new terms until they recur.

### Out of scope

No automatic publication under any confidence threshold. No model-drafted labels or definitions. No prioritisation or scoring model over the queue beyond raw counts; ordering by occurrence is sufficient at this scale and is explainable, which a learned ranking would not be.

## References

- [ADR-001](001-human-authored-skos-vocabulary.md): why the model never authors vocabulary
- [ADR-002](002-ingestion-against-the-vocabulary.md): held claims and the document gap source
- [ADR-003](003-otel-fleet-signals-to-skos.md): the telemetry gap source
- [ADR-005](005-governance-and-downstream-orchestration.md): what happens when a gap is closed
- [ADR-006](006-vocabulary-console-and-chat.md): how the queue is presented
