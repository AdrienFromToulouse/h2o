# ADR-003: OpenTelemetry Fleet Signals Mapped to SKOS

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-001](001-human-authored-skos-vocabulary.md), [ADR-002](002-ingestion-against-the-vocabulary.md), [ADR-004](004-vocabulary-gap-queue.md), [ADR-006](006-vocabulary-console-and-chat.md)

## Context

The dispensers are connected. Firmware exports OpenTelemetry over OTLP through IoT Core into a collector, and every signal arrives carrying **machine-side names for things the vocabulary already has words for**. `component.type=carbon_filter` and *Carbon Filter* are the same entity in two dialects.

Three groups name that entity, and none of them should have to adopt another's naming:

- **Firmware engineers** own attribute keys and values. They version on their own release cycle and answer to wire-format stability, not to business readability.
- **Domain experts** own the business vocabulary. They should never see an attribute key.
- **Users** say whatever they say, and get resolved by [ADR-001](001-human-authored-skos-vocabulary.md)'s cascade.

SKOS is the bridge. The mapping is also the third source of vocabulary gaps: when firmware ships an attribute value that maps to nothing, that is a hole in the business vocabulary discovered by a machine.

## Decision

> **These payloads are invented fixture data.** `service.name`, `service.namespace`, `deployment.environment`, `device.id`, and `device.model.identifier` are real OpenTelemetry semantic conventions and are used as specified. Everything else is illustrative and was authored for this demonstrator: `site.id`, `component.type`, `component.serial`, `water.output`, `fault.code`, `fault.type`, the `E17`/`E42` codes, and all `dispenser.*` instrument names and units. There is no OTEL semantic convention for water dispensers, so a manufacturer defines these in its own namespace; what follows shows the *shape* such a namespace takes. Replace it with the real firmware contract before any of this meets a device.

### 1. The signal

Resource attributes on every dispenser:

```
service.name            = "water-dispenser"
service.namespace       = "fleet"
deployment.environment  = "production"
device.id               = "WD-4412"
device.model.identifier = "FS-500-SPK"
site.id                 = "customer-1183/floor-2"
```

Instruments and events, trimmed:

```jsonc
{ "name": "dispenser.filter.life_remaining", "unit": "%",
  "gauge": { "dataPoints": [{ "asDouble": 12.5, "attributes": [
    {"key":"component.type","value":{"stringValue":"carbon_filter"}},
    {"key":"component.serial","value":{"stringValue":"CF-88213"}} ]}]}}

{ "name": "dispenser.water.dispensed", "unit": "L",
  "sum": { "dataPoints": [{ "asDouble": 1842.0, "attributes": [
    {"key":"water.output","value":{"stringValue":"sparkling"}} ]}]}}

{ "name": "dispenser.co2.pressure",    "unit": "bar" }      // component.type=co2_cylinder
{ "name": "dispenser.uv.lamp_hours",   "unit": "h" }        // component.type=uv_lamp
{ "name": "dispenser.bottles_avoided", "unit": "{bottle}" }

// log record
{ "body": "flow below threshold", "attributes": [
  {"key":"event.name","value":{"stringValue":"dispenser.fault"}},
  {"key":"fault.code","value":{"stringValue":"E17"}},
  {"key":"fault.type","value":{"stringValue":"low_flow"}} ]}
```

### 2. The mapping

| OTEL signal · attribute | Machine token | SKOS concept |
| --- | --- | --- |
| `dispenser.filter.life_remaining` · `component.type` | `carbon_filter` | `h2o:carbon-filter` |
| `dispenser.co2.pressure` · `component.type` | `co2_cylinder` | `h2o:co2-cylinder` |
| `dispenser.uv.lamp_hours` · `component.type` | `uv_lamp` | `h2o:uv-lamp` |
| `dispenser.water.dispensed` · `water.output` | `sparkling` | `h2o:sparkling` |
| `dispenser.fault` · `fault.type` / `fault.code` | `low_flow` / `E17` | `h2o:low-flow` |
| `dispenser.service` · `service.type` | `filter_replacement` | `h2o:filter-replacement` |
| `dispenser.bottles_avoided` (instrument name) | (none) | `h2o:single-use-bottles-avoided` |

### 3. A separate concept scheme, joined by SKOS mapping properties

The machine side becomes its own scheme, `hs:telemetry`. Each concept carries the exact wire string as `skos:notation` and joins the business vocabulary with `skos:exactMatch`, or with `skos:closeMatch` / `skos:broadMatch` where the fit is approximate, which is worth saying out loud rather than flattening.

```turtle
tel:component.type.carbon_filter a skos:Concept ;
    skos:inScheme   hs:telemetry ;
    skos:prefLabel  "component.type=carbon_filter"@en ;
    skos:notation   "carbon_filter" ;
    h2o:otelSignal  "dispenser.filter.life_remaining" ;
    h2o:otelUnit    "%" ;
    skos:exactMatch h2o:carbon-filter .

tel:fault.code.E17 a skos:Concept ;
    skos:inScheme   hs:telemetry ;
    skos:prefLabel  "fault.code=E17"@en ;
    skos:notation   "E17" ;
    h2o:otelSignal  "dispenser.fault" ;
    skos:closeMatch h2o:low-flow ;
    skos:scopeNote  "Firmware raises E17 below 0.8 L/min; the business term also covers user-reported slow flow."@en .
```

**Why a separate scheme and not `skos:altLabel` on the business concept.** Three reasons, in order of weight:

1. **It would leak into the curation UI.** `carbon_filter` would print in the *Alternative terms* box of the expert's review card, exactly the technical leakage [ADR-006](006-vocabulary-console-and-chat.md) forbids.
2. **A firmware rename would look like a vocabulary change.** The two sides version independently; conflating them means every OTLP contract revision produces a spurious business-vocabulary version.
3. **Cross-scheme alignment is what these properties are for.** `skos:exactMatch` and its siblings exist precisely to relate concepts across schemes maintained by different parties.

`skos:hiddenLabel` was the cheap alternative: it keeps the resolver working with no new scheme. It was rejected because it loses the ability to record *which* signal a token came from, its unit, and its own version history, and because it puts firmware strings inside the artefact a domain expert owns.

### 4. What it buys the agent

`get_fleet_signal(concept, device?)` turns *"is my sparkling water machine low on gas?"* into: resolve → `h2o:co2-cylinder` → inverse `skos:exactMatch` → `component.type=co2_cylinder` → the `dispenser.co2.pressure` series for that device, answered in business language with the value and its unit.

The user never learns an attribute name. The firmware never learns a business term. The mapping is the only place the two meet, and it is reviewable.

### 5. The third gap source

Firmware ships `fault.type=scale_buildup` with `fault.code=E42`. The mapping step finds no `hs:telemetry` concept and no match into the business scheme, so an **unmapped-signal gap** enters the same queue as document and chat gaps ([ADR-004](004-vocabulary-gap-queue.md)):

> *"Your machines reported `scale_buildup` 214 times last week and it maps to nothing."*

This closes the limescale hole seeded in [ADR-001](001-human-authored-skos-vocabulary.md): a vocabulary gap detected in machine data, at fleet scale, resolved by a person. It is the counterpart to the chat-driven loop and demonstrates that gap detection is not a chat feature.

### 6. Agent traces share the pipeline

The Strands agent already runs under `opentelemetry-instrument`. We stamp `h2o.concept.resolved`, `h2o.concept.miss`, and `h2o.vocabulary.version` on its spans, so fleet telemetry and agent telemetry are one observability surface and a resolution failure is traceable to the turn that caused it.

The durable gap **queue** nonetheless stays a DynamoDB table. Traces are sampled and expire; a curation backlog must do neither. Traces are for debugging a specific request; the table is the work list.

## Consequences

### Benefits

- **Three naming worlds coexist** (firmware, business, and user) with exactly one reviewable mapping between them and no party forced to rename anything.
- **Fleet questions are answerable in business language.** Concept-grounded telemetry access without exposing attribute keys to anyone.
- **Vocabulary gaps are detected at machine scale.** 214 occurrences of an unmapped fault code is a stronger signal than three chat turns, and neither requires a person to notice.
- **Approximate mappings stay honest.** `closeMatch` plus a `scopeNote` records that `E17` and *Low Flow* are near-equivalents rather than pretending they are identical.
- **The two sides version independently.** A firmware release changes `hs:telemetry`; the business vocabulary is untouched.

### Trade-offs

- **The mapping is a second artefact to maintain.** It needs an owner, and that owner sits between the firmware and domain teams: an organisational seam, not a technical one.
- **It drifts silently on the firmware side.** A renamed attribute produces unmapped-signal gaps rather than errors, which is the correct failure mode but is only useful if someone works the queue.
- **`closeMatch` is a judgement call.** Whether `E17` is close enough to *Low Flow* is a domain decision the model cannot make and the ADR cannot settle.
- **Every mapped token needs a concept**, so `hs:telemetry` grows roughly with the firmware's attribute surface. For a demonstrator this is small; for a real fleet with many models it is its own curation workload.

### Out of scope

**No control plane.** h2o reads telemetry and never commands a device. **No alerting and no maintenance scheduling.** Deciding what to do about a filter at 12.5% is a consumer's system, and an agent that can act is an agent that can be prompted into acting. **No live OTLP ingest in the demonstrator**: a recorded fixture in `data/telemetry/` replays through the same mapping code, so the mapping is exercised without a device.

## References

- [OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/)
- [OTLP Specification](https://opentelemetry.io/docs/specs/otlp/)
- [SKOS Reference, mapping properties](https://www.w3.org/TR/skos-reference/#mapping)
- [ADR-001](001-human-authored-skos-vocabulary.md): the business vocabulary this maps onto
- [ADR-004](004-vocabulary-gap-queue.md): where unmapped signals go
- [ADR-006](006-vocabulary-console-and-chat.md): why attribute names must not reach the UI
