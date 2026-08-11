#!/usr/bin/env python3
"""Validate the seed vocabulary against the decisions the ADRs actually make.

Three layers, in order of what owns what:

1. SHACL (`vocab/shapes/skos-integrity.ttl`) is the integrity gate described in
   docs/adrs/005-governance-and-downstream-orchestration.md. Violations block.
2. Resolver parity: the one check SHACL cannot perform, because it needs the
   real normalisation function rather than SPARQL's LCASE.
3. Demonstrator invariants: the two deliberate gaps, and the ADR-003 mapping
   table executed against the recorded OTLP fixture.

`normalise` is imported from h2o_core rather than defined here, because the
whole point of check 2 is that it runs the function the published index is
actually built with. A local copy would make this a check of a copy.

    uv run python scripts/check_vocab.py

Exit code 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from h2o_core import config, graph, integrity
from h2o_core.normalize import normalise
from rdflib import Namespace, URIRef
from rdflib.namespace import RDF, SKOS

ROOT = Path(__file__).resolve().parent.parent
VOCAB = ROOT / "vocab"
SHAPES = VOCAB / "shapes" / "skos-integrity.ttl"
FIXTURE = ROOT / "data" / "telemetry" / "fleet-sample.otlp.json"

H2O = Namespace("https://vocab.h2o.example/id/")
HS = Namespace("https://vocab.h2o.example/scheme/")

failures: list[str] = []
notes: list[str] = []


def fail(check: str, detail: str) -> None:
    failures.append(f"{check}: {detail}")


def short(iri: URIRef | str) -> str:
    return str(iri).rsplit("/", 1)[-1]


# ---------------------------------------------------------------- load

ttl_files = sorted(VOCAB.glob("*.ttl"))
if not ttl_files:
    print("no Turtle files found in vocab/", file=sys.stderr)
    sys.exit(1)

# Loaded into the same store the publish path loads, then converted through the
# same function the gate uses. One parse, one copy: a second reading of the same
# Turtle is a second chance for this script and the API to disagree about what
# the vocabulary says.
try:
    store = graph.store_from_turtle(
        {path.name: path.read_bytes() for path in ttl_files}, config.PUBLISHED_GRAPH
    )
except Exception as exc:  # noqa: BLE001 - the file it names is the whole message
    print(f"parse: {exc}", file=sys.stderr)
    sys.exit(1)

data = integrity.as_rdflib(store)

concepts = set(data.subjects(predicate=RDF.type, object=SKOS.Concept))
notes.append(f"parsed {len(ttl_files)} Turtle files, {len(data)} triples, {len(concepts)} concepts")

scheme_of: dict[URIRef, URIRef] = {}
for c in concepts:
    schemes = list(data.objects(c, SKOS.inScheme))
    if schemes:
        scheme_of[c] = schemes[0]


# --------------------------- 1 and 2. the gate, exactly as a publish runs it

# Imported, never reimplemented. ADR-005: "Git and the console are equally safe.
# Both paths run the same gate, so a bulk Turtle edit cannot bypass validation
# that a UI edit enforces." That sentence is only true if this line calls the
# same function the publish route calls -- a second copy here would be a second
# gate, and two gates drift in exactly one direction.
findings = integrity.validate(store)
blocking = integrity.blocking(findings)

for finding in blocking:
    fail(
        "gate",
        f"{finding.concept_id}: {finding.message}" if finding.concept_id else finding.message,
    )
if not blocking:
    notes.append(f"OK  integrity gate: no violations across {len(concepts)} concepts")

warned: dict[str, int] = defaultdict(int)
for finding in findings:
    if not finding.blocks:
        warned[finding.message] += 1
for message, count in sorted(warned.items()):
    notes.append(f"WARN {count}x {message}")

# Guard the guard: prove the normaliser still folds the case SPARQL misses, so
# a future "simplification" of normalise() cannot silently weaken parity.
if normalise("CO₂ Cylinder") != normalise("CO2 Cylinder"):
    fail("resolver-parity", "normalise() no longer folds 'CO₂' to 'CO2'; SHACL cannot cover this")


# ----------------------------------- 3a. deliberate gap: no 'gas bottle' label

all_labels = {
    normalise(str(lit))
    for c in concepts
    for pred in (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel)
    for lit in data.objects(c, pred)
}
if "gas bottle" in all_labels:
    fail("seeded-gap", "'gas bottle' is a label somewhere; ADR-004's chat loop needs it absent")
else:
    notes.append("OK  seeded gap intact: no 'gas bottle' label")


# ------------------------------- 3b. deliberate gap: no limescale/scale concept

SCALE_WORDS = ("limescale", "scale", "scaling", "descal")
hits = sorted(
    f"{short(c)}='{lit}'"
    for c in concepts
    for pred in (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel)
    for lit in data.objects(c, pred)
    if any(w in str(lit).casefold() for w in SCALE_WORDS)
)
if hits:
    fail(
        "seeded-gap", f"limescale-related label(s) present: {hits}; ADR-003's fleet loop needs none"
    )
else:
    notes.append("OK  seeded gap intact: no limescale concept")


# ------------------- 3c. ADR-003 mapping table, executed against the fixture

MAPPING_KEYS = {"component.type", "water.output", "fault.type", "fault.code", "service.type"}
EXPECTED_UNMAPPED = {"scale_buildup", "E42"}

notation_to_concept = {
    str(n): c
    for c in concepts
    if scheme_of.get(c) == HS.telemetry
    for n in data.objects(c, SKOS.notation)
}


def business_target(tel_concept: URIRef) -> URIRef | None:
    for pred in (SKOS.exactMatch, SKOS.closeMatch, SKOS.broadMatch):
        for t in data.objects(tel_concept, pred):
            return t
    return None


def walk_attributes(node: object) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        value = node.get("value")
        if "key" in node and isinstance(value, dict) and "stringValue" in value:
            found.append((str(node["key"]), str(value["stringValue"])))
        for v in node.values():
            found.extend(walk_attributes(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(walk_attributes(v))
    return found


if not FIXTURE.exists():
    fail("fixture", f"{FIXTURE.relative_to(ROOT)} not found")
else:
    payload = json.loads(FIXTURE.read_text())
    tokens = {v for k, v in walk_attributes(payload) if k in MAPPING_KEYS}

    unmapped = {
        token
        for token in tokens
        if (tel := notation_to_concept.get(token)) is None or business_target(tel) is None
    }
    unexpected = unmapped - EXPECTED_UNMAPPED
    missing_gap = EXPECTED_UNMAPPED - unmapped
    if unexpected:
        fail("otel-mapping", f"fixture tokens map to nothing: {sorted(unexpected)}")
    if missing_gap:
        fail(
            "otel-mapping",
            f"expected these to be UNMAPPED but they resolved: {sorted(missing_gap)}",
        )
    if not unexpected and not missing_gap:
        notes.append(
            f"OK  {len(tokens) - len(unmapped)}/{len(tokens)} fixture tokens map; "
            f"unmapped exactly {sorted(unmapped)}"
        )


# ------------------- 3d. the review card in the brief renders from real data

card = H2O["carbon-filter"]
expected_alts = {"Carbon Cartridge", "Filter Cartridge"}
actual_alts = {str(x) for x in data.objects(card, SKOS.altLabel)}
if not expected_alts <= actual_alts:
    fail(
        "review-card",
        f"carbon-filter altLabels {sorted(actual_alts)} miss {sorted(expected_alts - actual_alts)}",
    )
if (card, SKOS.broader, H2O["filter"]) not in data:
    fail("review-card", "carbon-filter is not skos:broader h2o:filter")
if (card, SKOS.related, H2O["purification"]) not in data:
    fail("review-card", "carbon-filter is not skos:related h2o:purification")
if not list(data.objects(card, SKOS.definition)):
    fail("review-card", "carbon-filter has no skos:definition")


# ---------------------------------------------------------------- report

per_scheme: dict[str, int] = defaultdict(int)
for c in concepts:
    per_scheme[short(scheme_of.get(c, "none"))] += 1

print("h2o vocabulary check")
print("-" * 60)
for line in notes:
    print(f"  {line}")
print(f"  concepts per scheme: {dict(sorted(per_scheme.items()))}")
print("-" * 60)

if failures:
    print(f"FAILED: {len(failures)} problem(s)")
    for f in failures:
        print(f"  x {f}")
    sys.exit(1)

print("PASSED: SHACL gate, resolver parity, seeded gaps, and the OTEL mapping table")
sys.exit(0)
