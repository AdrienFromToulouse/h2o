# ADR-006: The Vocabulary Console, and Chat as the Second Surface

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-001](001-human-authored-skos-vocabulary.md), [ADR-002](002-ingestion-against-the-vocabulary.md), [ADR-003](003-otel-fleet-signals-to-skos.md), [ADR-004](004-vocabulary-gap-queue.md), [ADR-005](005-governance-and-downstream-orchestration.md)

## Context

The person who owns the vocabulary knows water treatment. They do not know RDF, they will not write SPARQL, and if they are shown an IRI they will reasonably conclude the tool is not for them.

This matters more than it sounds. [ADR-001](001-human-authored-skos-vocabulary.md) puts a human at the centre of the design, and the vocabulary is only as good as the expert's willingness to maintain it. Every piece of technical vocabulary in the interface is a reason for that person to defer the task, and a deferred queue is the failure mode [ADR-004](004-vocabulary-gap-queue.md) warns about. **The UI's non-technical surface is a functional requirement, not a polish item.**

There is a second surface, the chat agent. It is not the product here, but it is not decorative either: it is the only place where the effect of vocabulary work becomes directly observable. An expert who adds *"gas bottle"* as an alternative term and then watches a previously failing question succeed has seen the system's whole argument in ten seconds.

## Decision

### 1. One Next.js application, two surfaces

A single Next.js 15 App Router app, Tailwind v4, no component library. Both surfaces share the session, the vocabulary data, and the BFF's AWS credentials, and splitting them would duplicate all three for no benefit.

| Route | Purpose |
| --- | --- |
| `/vocabulary` | Scheme tree and search, **the landing page** |
| `/vocabulary/[id]` | The review card: edit, impact preview, change note, Save new version |
| `/gaps` | The evidenced gap queue ([ADR-004](004-vocabulary-gap-queue.md)), evidence inline |
| `/runs` | Publish fan-out status and counts ([ADR-005](005-governance-and-downstream-orchestration.md)) |
| `/conflicts` | Contradicting claims ([ADR-002](002-ingestion-against-the-vocabulary.md)), side by side with sources |
| `/` | Chat, the proof the vocabulary works |

The landing page is `/vocabulary`, not `/`. That choice states what the application is for: the vocabulary is the subject, and the chat is a consequence of it.

### 2. The non-technical contract

**No IRI, no Turtle, no SPARQL, no SKOS jargon, and no OTEL attribute name appears in the default interface.** This is a hard constraint, not a guideline, and it is testable.

| Model | UI label |
| --- | --- |
| `skos:Concept` | Term |
| `skos:ConceptScheme` | Vocabulary |
| `skos:prefLabel` | Name |
| `skos:definition` | Definition |
| `skos:altLabel` | Alternative terms |
| `skos:broader` | Parent |
| `skos:narrower` | Sub-terms |
| `skos:related` | Related concepts |
| `skos:exactMatch` → `hs:telemetry` | Machine signals (read-only chips) |
| `owl:versionInfo` + `dct:modified` + `dct:contributor` | "Version 3 · updated by Marie, 12 Mar" |
| `skos:changeNote` | "Why are you making this change?" |

Integrity failures ([ADR-005](005-governance-and-downstream-orchestration.md)) are rendered the same way, as in *"Filter Cartridge is already an alternative term for Sediment Filter"*, never a validation code and never the query that produced it.

A **Show technical detail** toggle reveals the IRI, the Turtle, and the OTEL bindings for whoever wants them. The toggle exists so that hiding this material costs nothing; it defaults off and its state is not remembered across concepts, so the technical view is something you opt into per concept rather than a mode you get stuck in.

**One carve-out, because "no OTEL attribute name" is not quite the rule.** OTEL **attribute keys and values** — `component.type`, `fault.code=E42`, `water.output=sparkling` — never appear outside the toggle. Those are the firmware's private naming, and letting them leak is exactly what a separate `hs:telemetry` scheme exists to prevent ([ADR-003](003-otel-fleet-signals-to-skos.md)). **Instrument names** — `dispenser.co2.pressure` — do appear, read-only, in the two places named below: the *Machine signals* row of the review card, and the fleet variant of the chat concept chip. An instrument name is what the machine measures rather than what it calls a thing, it is the only honest way to show that a business term is wired to real telemetry, and both surfaces present it as a fact about the term rather than as something to edit.

### 3. The review card

```
┌──────────────────────────────────────────────────┐
│ Carbon Filter                    Version 3        │
│                                  Marie, 12 Mar    │
│ Definition                                        │
│ A filter using activated carbon to reduce         │
│ chlorine, taste and odour compounds…              │
│                                                   │
│ Alternative terms                                 │
│ • Carbon Cartridge          ×                     │
│ • Filter Cartridge          ×            + Add    │
│                                                   │
│ Parent                                            │
│ Filter                                    change  │
│                                                   │
│ Related concepts                                  │
│ Water Purification                        change  │
│                                                   │
│ Machine signals                                   │
│ dispenser.filter.life_remaining      (read-only)  │
│                                                   │
│ Why are you making this change?                   │
│ [                                              ]  │
│                                                   │
│ ℹ Adding this alternative term will resolve       │
│   12 mentions across 3 documents.                 │
│                                                   │
│ [Show technical detail]      [Save new version]   │
└──────────────────────────────────────────────────┘
```

Two things matter most here. The **impact preview** ([ADR-005](005-governance-and-downstream-orchestration.md)) sits directly above the save button, because it is the information the expert needs at the moment of deciding. And a **before/after diff** is shown on publish, in the same plain language, so the change note is written against something concrete.

When the card is opened from a gap entry, the **evidence is pinned beside it**, showing the actual sentences and user turns that produced the gap, rather than being left behind on the previous screen.

Parent and related pickers search by name and show the definition of each candidate inline. Choosing a parent is the highest-consequence edit on this card and the easiest to get wrong from a label alone.

### 4. The gap queue must offer "none of these"

[ADR-004](004-vocabulary-gap-queue.md) notes that a suggested attachment point can anchor the expert. The console counters this concretely: **"None of these, create a new term"** is a primary action on every gap entry, styled equally with the suggestion, never a fallback link at the bottom. The suggestion is a shortcut, not a recommendation, and the interface must not imply otherwise.

### 5. Chat

`useChat` with a `DefaultChatTransport` configured to carry a thread id, posting to `/api/chat`, which SigV4-signs the request to AgentCore's `/invocations` and bridges the response stream.

Streaming uses `createUIMessageStream` rather than `streamText`: the agent is a remote Python process with its own SSE event protocol, not a model the frontend calls. The route translates that protocol (`text_delta`, `tool_use`, `tool_result`, `done`, `error`) into AI SDK v5 UI message parts. The wire contract is mirrored in TypeScript and Python, and the two change together.

Two h2o-specific data parts carry the argument of the whole system into the interface:

- **`data-concept`**: a chip under the answer reading *"gas bottle → **CO₂ Cylinder**"*, or for fleet answers *"CO₂ Cylinder ← `dispenser.co2.pressure`"*. This makes the resolution step **visible**. Without it the vocabulary is invisible infrastructure and the demonstrator has no argument.
- **`data-conflict`**: a badge expanding to both sides of a contradiction with their sources ([ADR-002](002-ingestion-against-the-vocabulary.md)), so disagreement is presented rather than buried in prose.

Tool calls render as labelled chips such as *"Looking up the vocabulary"* and *"Searching the manuals"*, not as JSON. A domain expert watching the agent work should learn what it consults, not what its function signatures are.

#### Amendment, M5: what a `data-concept` chip may and may not say

**A miss says only that it is a miss.** It carried the nearest existing terms as a suffix, and the console showed why that was wrong: `process → not in the vocabulary · closest: Fault, Dispenser, Component`. The shortlist is a curator's artefact ([ADR-004 §2](004-vocabulary-gap-queue.md)); rendered beside an answer it reads as *"did you mean"*, and the measured ranking cannot support that reading. The queue entry still carries it.

**A correction is invisible.** A term the read path's sanitiser corrected renders as an ordinary resolution — *"installtion → **Installation**"* — with no badge and no third chip state. The left-hand side is always the words that were typed, which is the only thing that makes the chip worth reading, and it is why the sanitiser returns an alias map rather than a rewritten question ([ADR-002 §4](002-ingestion-against-the-vocabulary.md)).

Neither changes the rule this section exists for: a chip is derived from the retrieval, never written by the model, so it cannot be conjured and — the half that matters more — cannot be suppressed.

### 6. Credentials and identity

AWS credentials live only in server routes; the browser never reaches AgentCore or API Gateway directly. There is **no end-user authentication in the demonstrator**; the only identity is a client-generated session id in `localStorage`.

This is a demonstrator decision and it is wrong for a real deployment. Publishing is a write, the audit trail already records a contributor, and that field is currently unverified. A real deployment needs Cognito in front of the console, and the audit schema is designed so that adding it changes who fills the field rather than the shape of the record.

## Consequences

### Benefits

- **The vocabulary owner can actually use it.** No RDF literacy required to add a term, retire one, or fix a definition.
- **Vocabulary work becomes observably worthwhile.** The impact preview before the change and the run counts after it turn maintenance from a chore into a measurable contribution.
- **The concept chips make the architecture legible.** A viewer sees that the system resolved their words to a governed term, which is the entire thesis rendered in one line of UI.
- **Disagreement is presented rather than hidden**, in both surfaces, consistently with [ADR-002](002-ingestion-against-the-vocabulary.md).
- **One deployment, one credential path, one session** for both surfaces.

### Trade-offs

- **The translation layer is real code.** Every SKOS construct needs a label, an editor, and an error message in plain language, and that mapping has to be maintained as the model grows.
- **Hiding structure hides capability.** An expert cannot express something the card has no field for, such as a second parent, a mapping relation, or a scope note, and must fall back to git ([ADR-001](001-human-authored-skos-vocabulary.md)). This is a deliberate ceiling, not an oversight.
- **The stream bridge is ours to maintain**, along with the SigV4 signing in the Next.js routes, and the TypeScript and Python event definitions must be changed together or the chat silently drops parts.
- **No authentication means no real attribution.** The audit trail records a contributor field that nobody verifies. Acceptable for a demonstrator, unacceptable the moment a second person uses it.
- **Six routes is a lot of surface for a demonstrator.** `/conflicts` and `/runs` in particular are thin, and could be panels rather than pages if the build proves them sparse.

### Out of scope

Authentication and authorisation. Multi-user editing and edit-conflict resolution in the UI (the store-level conditional write is the backstop, see [ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)). Bulk editing, import, and export through the UI; that is git's job. Rollback UI. Any write path from the chat surface.

## References

- [Vercel AI SDK](https://sdk.vercel.ai/)
- [Next.js App Router](https://nextjs.org/docs/app)
- [ADR-001](001-human-authored-skos-vocabulary.md): git as the complementary edit path
- [ADR-004](004-vocabulary-gap-queue.md): the queue this console presents
- [ADR-005](005-governance-and-downstream-orchestration.md): impact preview and run status
