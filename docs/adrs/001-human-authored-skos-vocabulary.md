# ADR-001: A Human-Authored SKOS Vocabulary as the Entity Layer

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-002](002-ingestion-against-the-vocabulary.md), [ADR-003](003-otel-fleet-signals-to-skos.md), [ADR-004](004-vocabulary-gap-queue.md), [ADR-005](005-governance-and-downstream-orchestration.md), [ADR-006](006-vocabulary-console-and-chat.md), [ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)

## Context

h2o is a demonstrator for a manufacturer of connected, bottle-free water dispensers: purification, mineralisation, carbonation, flavour modules, field service, and an IoT-connected fleet. Its knowledge is scattered across installation manuals, service bulletins, spec sheets, support FAQs, and machine telemetry, and each of those sources names the same things differently. A manual says *carbon filter*, a technician says *carbon cartridge*, a customer says *the black thing*, and the firmware emits `component.type=carbon_filter`.

The usual response is to put a language model in front of the pile and hope it reconciles the naming. That produces answers that cannot be reproduced, cannot be audited, and change when the prompt changes.

We take the opposite approach. A **controlled vocabulary**, a SKOS thesaurus, defines the entities in a fixed language. Every source is resolved against it. The model consumes the vocabulary; it never authors it.

The governing principle, and the reason for every constraint in this ADR:

> **The vocabulary is a precondition of the system, not an output of it.**

A vocabulary induced from the corpus would inherit precisely the inconsistencies it exists to fix. Domain experts write version 1 before a single document is ingested.

## Decision

| Concern | Choice |
| --- | --- |
| Semantic layer | **SKOS**: `prefLabel`, `altLabel`, `hiddenLabel`, `definition`, `broader`, `related`, `notation`, `exactMatch`, authored as Turtle |
| Graph store | **Oxigraph embedded over S3** (RDF-native, SPARQL 1.1, zero idle cost) behind a `GraphStore` port; **Neptune Serverless** is the documented swap ([ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)) |
| Agent framework | **Strands Agents SDK** (Python) on **Bedrock AgentCore Runtime**, `POST /invocations` SSE |
| LLM | **Amazon Nova 2 Lite** via `strands.models.BedrockModel`, for chat and ingest-time extraction |
| Retrieval | **Amazon S3 Vectors** + **Titan Text Embeddings V2**, filtered by resolved concept |
| REST API | **FastAPI on AWS Lambda** behind API Gateway, IAM auth, **no VPC** ([ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md)) |
| Frontend | **Next.js 15 App Router + Vercel AI SDK v5**; server routes are the SigV4-signing BFF |
| IaC | **CloudFormation**, layered stacks, `h2o-{env}-{resource}`, eu-west-1 |
| Observability | `aws-opentelemetry-distro`; container CMD `opentelemetry-instrument python -m …` |

The model identifier is an inference profile and differs by deployment (`eu.amazon.nova-2-lite-v1:0`, `global.amazon.nova-2-lite-v1:0`). Verify the available profile at build time rather than copying one from this document.

### 1. Version 1 is written by people

The seed vocabulary is a human artefact, produced with domain experts and reviewed in git as Turtle. Two consequences follow:

- **Ingestion cannot run against an unpublished vocabulary.** The pipeline hard-fails rather than inventing concepts on the fly. An empty vocabulary is a configuration error, not an empty starting state to be filled by extraction.
- **Turtle-in-git and the curation console are not competitors.** Git is the bootstrap path and the bulk-edit path: a new scheme, a restructure, a translation pass. The console ([ADR-006](006-vocabulary-console-and-chat.md)) is the ongoing, non-technical change path: one term, one definition, one alternative label. Both write the same graph and both pass the same integrity gate ([ADR-005](005-governance-and-downstream-orchestration.md)). Neither is privileged.

### 2. Why SKOS, and not OWL or a property graph

The artefact a domain expert can actually own is a **thesaurus**, not an ontology. `broader` / `related` / `altLabel` / `definition` is the entire semantic budget, and that is a feature: every construct maps to a sentence a non-specialist can evaluate. *"Is a carbon filter a kind of filter?"* is answerable by someone who knows water treatment. *"Is `hasComponent` an inverse functional property?"* is not.

SKOS is also a W3C standard with an existing publication culture, so a client's own thesaurus loads without transformation and ours exports without lock-in.

We explicitly decline **reasoning**. Reasoner-inferred edges are the "LLM as source of truth" failure mode wearing formal clothes: facts appearing in the graph that no human asserted and no source evidences.

**Validation is a different thing, and we do want it.** SHACL is not a reasoner: the specification requires that "during validation, the data graph and the shapes graph MUST remain immutable", so it reads and reports and derives nothing. It is the integrity gate ([ADR-005](005-governance-and-downstream-orchestration.md)). What we exclude is SHACL **Advanced Features** (`sh:rule`), which do infer triples, and which sit behind an explicit opt-in flag in the validator we use.

### 3. Concept identity and naming

Concept IRIs are stable slugs under an h2o namespace, minted once and never reused:

```
h2o:  https://vocab.h2o.example/id/       concepts
hs:   https://vocab.h2o.example/scheme/   concept schemes
tel:  https://vocab.h2o.example/telemetry/  machine-side concepts (ADR-003)
```

An IRI is not a label. Renaming a concept changes its `prefLabel`, never its IRI. Otherwise every extracted claim pointing at it breaks, which is the entire reason to have identifiers separate from names.

### 4. The agent's tools are read-only and typed

```python
agent = Agent(
    model=BedrockModel(model_id=MODEL_ID),
    system_prompt=GROUNDING_RULES,
    tools=[
        resolve_concept,
        get_concept,
        browse_scheme,
        expand_concept,
        get_facts,
        search_documents,
        get_fleet_signal,
    ],
)
```

| Tool | Backs onto | Purpose |
| --- | --- | --- |
| `resolve_concept(term)` | in-process resolver index | User wording → canonical concept, or an explicit no-match |
| `get_concept(iri)` | graph store, templated SPARQL | Labels, definition, parent, children, related, version |
| `browse_scheme(scheme, root?)` | graph store | Hierarchy navigation |
| `expand_concept(iri, depth)` | graph store | Transitive `narrower` + `altLabel` → retrieval expansion terms |
| `get_facts(concept, subject?)` | graph store, templated SPARQL | Extracted claims **with their conflict flags** ([ADR-002](002-ingestion-against-the-vocabulary.md)) |
| `search_documents(query, concepts[])` | S3 Vectors | Verbatim snippets with `source_file` + `line_range`, concept-filtered |
| `get_fleet_signal(concept, device?)` | telemetry store | Metric values for a concept, via the OTEL mapping ([ADR-003](003-otel-fleet-signals-to-skos.md)) |

**Loop shape.** Resolve the user's domain terms first, then read structure (`get_concept`, `get_facts`, `get_fleet_signal`), then fall back to retrieval (`search_documents`) for what structure lacks. Answer in the vocabulary's preferred labels while acknowledging the user's wording, as in *"the carbon filter (you said carbon cartridge)"*, because teaching the canonical term is half of what a controlled vocabulary is for.

`GROUNDING_RULES` enforces: cite `source_file` plus the verbatim snippet for every fact; surface both sides of a conflict, attributed, never resolving it; answer "not found in the sources" rather than guessing; preserve units exactly.

### What we do NOT do

- **No LLM-authored SPARQL.** Tools execute parameterized templates from `packages/h2o_core/src/h2o_core/sparql/` (inside the package, so the built wheel carries them). A free-form `execute_sparql` tool reintroduces unbounded interpretation over structure and makes answers unreproducible. Parameters bind **RDF terms only**: a different query shape is a different template file, never a placeholder in a clause position.
- **No graph writes from the chat agent, at all.** Its IAM role carries no write access to the graph store: read-only on the S3 dataset prefix today, no Neptune write actions after a swap. Writes happen in ingestion and in publish, never inside a conversation. This is enforced by IAM, not by prompt.
- **No model-authored vocabulary.** The model never writes a `prefLabel`, never writes a `definition`, never mints an IRI, never selects a parent outside the existing tree ([ADR-004](004-vocabulary-gap-queue.md)).
- **No OWL/RDFS reasoning, and no SHACL `sh:rule` inference.** SHACL *validation* is the integrity gate; what is excluded is anything that derives a triple no human authored.
- **No answers without grounding.** It needs a resolved concept, a stored claim, or a retrieved snippet. Otherwise the agent says it does not know, and the unresolved term is logged ([ADR-004](004-vocabulary-gap-queue.md)).
- **No live ERP/CRM connectors.** Instance data arrives as versioned batch imports.

#### Amendment, M5: one model call runs *before* deterministic retrieval

This ADR describes chat as a model that calls deterministic tools. On the miss path it now also calls a model before the deterministic cascade runs, and that is a real change to the shape rather than a detail. The reason is in [ADR-002 §4](002-ingestion-against-the-vocabulary.md): a dictionary cannot recognise `installtion`, embeddings cannot either, and nothing at all could recognise a question asked in French.

What keeps it inside the list above is that **the sanitiser cannot see the vocabulary**. It is shown the question and nothing else, so it can change how a term is spelled or what language it is in, and it cannot change what the term refers to — the substitution that would matter, `gas bottle` to `CO₂ Cylinder`, requires seeing `CO₂ Cylinder`. Concretely it still writes no `prefLabel`, mints no IRI, selects no parent, authors no SPARQL, and holds no write capability; and "no answers without grounding" is untouched, because it hands back an alias map rather than an answer and the cascade decides everything downstream of it.

The bound is a signature, not a prompt: `sanitise.aliases()` accepts a question and a Bedrock client, and there is no parameter an index could arrive through. A test asserts against the request body that no shipped label reaches it.

## Architecture Overview

The Next.js app on Vercel serves both the curation console and the chat. Its server routes hold the only AWS credentials and SigV4-sign two backends: the Strands agent on AgentCore Runtime (`POST /invocations`, SSE) and the FastAPI vocabulary API on Lambda + API Gateway. Both share the graph store (S3 + embedded Oxigraph), the S3 Vectors index, the DynamoDB operational tables (gap queue, curation audit, document registry), and Bedrock. A shared Python package, `packages/h2o_core`, holds the SKOS models, SPARQL templates, store ports, resolver index, integrity gate, and the OTEL mapper, so that logic is written once and imported by both deployables.

## Seed vocabulary

Seven concept schemes, 80 concepts, in `vocab/*.ttl`: 57 business concepts across six schemes plus 23 machine-side concepts in `telemetry`. Every business concept carries `prefLabel` in English and Dutch, a `definition`, a `notation`, and its place in the hierarchy. Multilingual labelling is not decoration. It is half of why SKOS earns its place over a flat term list, and it makes the resolver's job visible.

| Scheme | Sample concepts |
| --- | --- |
| `equipment` | Dispenser → Countertop / Freestanding; Filter → Carbon Filter, Sediment Filter, RO Membrane; UV Lamp, CO₂ Cylinder, Mineral Cartridge, Flavour Pod, Chiller Block, Tap |
| `treatment` | Purification → Filtration, Reverse Osmosis, UV Disinfection; Mineralisation, Carbonation, Chilling |
| `water-output` | Still, Chilled, Sparkling, Ambient, Hot, Flavoured |
| `service` | Installation, Preventive Maintenance, Filter Replacement, Sanitisation, Repair, Decommissioning |
| `fault` | Leak, Low Flow, No Cooling, No Carbonation, Filter Expired, Taste Complaint |
| `sustainability` | Single-Use Bottles Avoided, CO₂e Avoided, Water Dispensed |
| `telemetry` | machine-side scheme, see [ADR-003](003-otel-fleet-signals-to-skos.md) |

The canonical concept:

```turtle
h2o:carbon-filter a skos:Concept ;
    skos:inScheme    hs:equipment ;
    skos:prefLabel   "Carbon Filter"@en, "Koolstoffilter"@nl ;
    skos:altLabel    "Carbon Cartridge"@en, "Filter Cartridge"@en ;
    skos:definition  "A filter using activated carbon to reduce chlorine, taste and odour compounds in the feed water."@en ;
    skos:broader     h2o:filter ;
    skos:related     h2o:purification ;
    skos:notation    "EQ-FLT-CARB" ;
    owl:versionInfo  "1" .
```

**Two gaps are deliberate and must survive.** `h2o:co2-cylinder` has no *"gas bottle"* alternative label, and there is no concept for limescale anywhere in the vocabulary. They are the starting conditions for the two demonstration loops ([ADR-004](004-vocabulary-gap-queue.md)), and a test fails if either is quietly filled in.

## Consequences

### Benefits

- **Reproducible grounding.** The same question resolves to the same concept and the same evidence on every run, because resolution is deterministic code against a versioned artefact.
- **The vocabulary is a governable asset.** It has an owner, a review path, versions, and an audit trail, none of which a prompt has.
- **Naming is decoupled from sources.** Documents, technicians, customers, and firmware may each use their own words; the vocabulary is where they meet, and no source has to change.
- **Standards interop.** SKOS thesauri load as-is and export without transformation.
- **The model is confined.** It extracts and converses. It does not decide what exists.

### Trade-offs

- **Someone must own the vocabulary.** This is the real operating cost, and it is organisational rather than technical. The gap queue makes the backlog visible ([ADR-004](004-vocabulary-gap-queue.md)); it does not staff it.
- **Bootstrapping requires domain access.** Version 1 needs experts in a room. A system that induced its vocabulary from documents would start faster and be wrong in ways nobody could see.
- **A missing concept degrades answers immediately and visibly.** We consider this correct, since a visible gap that enters a review queue is better than a confident answer built on an unnamed thing, but it does mean early quality tracks vocabulary coverage.
- **Nova 2 Lite is unproven on long service manuals.** Mitigated by strict extraction schemas, the verbatim-snippet gate, and keeping conflict detection out of the model ([ADR-002](002-ingestion-against-the-vocabulary.md)). Changing `model_id` is a one-line change if evaluation shows it is needed.
- **Two AWS deployables** (AgentCore container, Lambda API) share `h2o_core`, so more moving parts than one service, in exchange for clean REST semantics alongside a managed chat runtime.

### Out of scope

Authentication (the demo confines AWS credentials to server routes and has no end-user identity), multi-tenancy, live ERP/CRM/IoT connectors, and any write path from a conversation.

## References

- [SKOS Simple Knowledge Organization System Reference (W3C)](https://www.w3.org/TR/skos-reference/)
- [RDF 1.1 Turtle (W3C)](https://www.w3.org/TR/turtle/)
- [Strands Agents SDK](https://strandsagents.com/)
- [Amazon Bedrock AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)
- [Amazon S3 Vectors](https://aws.amazon.com/s3/features/vectors/)
- [Vercel AI SDK](https://sdk.vercel.ai/)
- [ADR-002](002-ingestion-against-the-vocabulary.md): ingestion resolves against this vocabulary
- [ADR-005](005-governance-and-downstream-orchestration.md): how the vocabulary changes
- [ADR-007](007-zero-idle-graph-runtime-and-cloudformation.md): where the graph runs
