# Architecture Decision Records

h2o is a knowledge platform for a manufacturer of connected, bottle-free water dispensers. A human-authored SKOS vocabulary defines the entities; documents and machine telemetry are resolved against it; a language model consumes the vocabulary but never authors it.

These ADRs record architecture decisions only. Procedural material belongs in `docs/operations/`.

## Vocabulary

- **Concept**: a named entity in the controlled vocabulary (`skos:Concept`). Surfaced to users as a *term*.
- **Concept scheme**: a grouping of related concepts (`skos:ConceptScheme`). Surfaced as a *vocabulary*.
- **Published graph**: the live vocabulary; the only graph the chat agent reads.
- **Held claim**: an extracted fact whose subject or object resolved to no concept. Retained, not discarded, and recovered when the vocabulary catches up.
- **Gap**: an evidenced record that some surface form resolved to nothing. The unit of the curation work list.
- **Attachment point**: the existing concept a gap most plausibly belongs to. A suggestion, never a decision.
- **Conflict**: two claims on the same subject and predicate with disagreeing values. Recorded and flagged, never silently resolved.
- **Shapes graph**: the SHACL constraints every publish is validated against, versioned in git beside the vocabulary.
- **Impact preview**: the computed downstream effect of a vocabulary change, shown before it is published.
- **Publish fan-out**: the orchestrated work a publish triggers: index rebuild, backlog re-resolution, targeted re-indexing.

## Foundation

| ADR | Decision | Status |
| --- | --- | --- |
| [001](001-human-authored-skos-vocabulary.md) | A Human-Authored SKOS Vocabulary as the Entity Layer | Proposed |
| [007](007-zero-idle-graph-runtime-and-cloudformation.md) | A Zero-Idle Graph Runtime, the GraphStore Port, and CloudFormation Stack Decomposition | Proposed |

## Knowledge Ingestion

| ADR | Decision | Status |
| --- | --- | --- |
| [002](002-ingestion-against-the-vocabulary.md) | Ingestion Against the Vocabulary | Proposed |
| [003](003-otel-fleet-signals-to-skos.md) | OpenTelemetry Fleet Signals Mapped to SKOS | Proposed |

## Vocabulary Evolution

| ADR | Decision | Status |
| --- | --- | --- |
| [004](004-vocabulary-gap-queue.md) | The Vocabulary Gap Queue | Proposed |
| [005](005-governance-and-downstream-orchestration.md) | Governance and Downstream Orchestration | Proposed |

## Interface

| ADR | Decision | Status |
| --- | --- | --- |
| [006](006-vocabulary-console-and-chat.md) | The Vocabulary Console, and Chat as the Second Surface | Proposed |

## Reading order

[001](001-human-authored-skos-vocabulary.md) establishes what the vocabulary is and why a human writes it. [002](002-ingestion-against-the-vocabulary.md) and [003](003-otel-fleet-signals-to-skos.md) cover what runs against it: documents and fleet telemetry. [004](004-vocabulary-gap-queue.md) is the hinge: how those two, plus chat, report what the vocabulary is missing. [005](005-governance-and-downstream-orchestration.md) covers how a change publishes and what it triggers. [006](006-vocabulary-console-and-chat.md) is how a non-technical expert sees all of it, and [007](007-zero-idle-graph-runtime-and-cloudformation.md) is where it runs.
