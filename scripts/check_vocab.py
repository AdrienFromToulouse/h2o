#!/usr/bin/env python3
"""Validate the seed vocabulary against the decisions the ADRs actually make.

Three layers, in order of what owns what:

1. SHACL (`vocab/shapes/skos-integrity.ttl`) is the integrity gate described in
   docs/adrs/005-governance-and-downstream-orchestration.md. Violations block.
2. Resolver parity: the one check SHACL cannot perform, because it needs the
   real normalisation function rather than SPARQL's LCASE.
3. Demonstrator invariants: the two deliberate gaps, and the ADR-003 mapping
   table executed against the recorded OTLP fixture.

    uv run --with pyshacl scripts/check_vocab.py

Exit code 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Namespace, URIRef
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


def normalise(label: str) -> str:
    """The resolver's normalisation: fold case, strip accents and punctuation.

    This is the function the published resolver index is built with, so it is
    the only correct definition of "two labels collide". SPARQL cannot express
    it, which is why the SHACL collision shape is a complement, not a
    replacement: LCASE alone reads "CO₂ Cylinder" and "CO2 Cylinder" as
    distinct, and the resolver does not.
    """
    text = unicodedata.normalize("NFKD", label)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def short(iri: URIRef | str) -> str:
    return str(iri).rsplit("/", 1)[-1]


# ---------------------------------------------------------------- load

data = Graph()
ttl_files = sorted(VOCAB.glob("*.ttl"))
if not ttl_files:
    print("no Turtle files found in vocab/", file=sys.stderr)
    sys.exit(1)

for path in ttl_files:
    try:
        data.parse(path, format="turtle")
    except Exception as exc:  # noqa: BLE001 - report and keep going
        fail("parse", f"{path.name}: {exc}")

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)

concepts = set(data.subjects(predicate=RDF.type, object=SKOS.Concept))
notes.append(f"parsed {len(ttl_files)} Turtle files, {len(data)} triples, {len(concepts)} concepts")

scheme_of: dict[URIRef, URIRef] = {}
for c in concepts:
    schemes = list(data.objects(c, SKOS.inScheme))
    if schemes:
        scheme_of[c] = schemes[0]


# -------------------------------------------------- 1. the SHACL integrity gate

shapes = Graph().parse(SHAPES, format="turtle")

# advanced=False keeps SHACL Advanced Features off: `sh:rule` would derive
# triples, and nothing in this system may assert a fact no human authored.
# allow_warnings makes sh:Warning results non-blocking, per ADR-005 check 6.
conforms, report_graph, report_text = validate(
    data,
    shacl_graph=shapes,
    advanced=False,
    inference="none",
    allow_warnings=True,
    abort_on_first=False,
)

SHACL_NS = Namespace("http://www.w3.org/ns/shacl#")
violations, warnings = [], []
for result in report_graph.subjects(RDF.type, SHACL_NS.ValidationResult):
    severity = report_graph.value(result, SHACL_NS.resultSeverity)
    focus = report_graph.value(result, SHACL_NS.focusNode)
    message = report_graph.value(result, SHACL_NS.resultMessage)
    line = f"{short(focus)}: {message}"
    (violations if severity == SHACL_NS.Violation else warnings).append(line)

if violations:
    for v in sorted(violations):
        fail("shacl", v)
else:
    notes.append(f"OK  SHACL gate: no violations across {len(concepts)} concepts")

if warnings:
    by_message: dict[str, int] = defaultdict(int)
    for w in warnings:
        by_message[w.split(": ", 1)[1]] += 1
    for msg, count in sorted(by_message.items()):
        notes.append(f"WARN {count}x {msg}")
if not conforms and not violations and not warnings:
    fail("shacl", f"non-conforming with no parsed results:\n{report_text}")


# ------------------------- 2. resolver parity: the check SHACL cannot perform

seen: dict[tuple[URIRef, str], set[str]] = defaultdict(set)
for c in concepts:
    scheme = scheme_of.get(c)
    if scheme is None:
        continue
    for pred in (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel):
        for lit in data.objects(c, pred):
            seen[(scheme, normalise(str(lit)))].add(short(c))

collisions = [
    f"in {short(scheme)}, '{label}' is claimed by {sorted(owners)}"
    for (scheme, label), owners in sorted(seen.items())
    if len(owners) > 1
]
for c in collisions:
    fail("resolver-parity", c)
if not collisions:
    notes.append(f"OK  resolver parity: {len(seen)} normalised labels, all unique per scheme")

# Guard the guard: prove the normaliser still folds the case SPARQL misses, so
# a future "simplification" of normalise() cannot silently weaken this check.
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
    fail("seeded-gap", f"limescale-related label(s) present: {hits}; ADR-003's fleet loop needs none")
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
        fail("otel-mapping", f"expected these to be UNMAPPED but they resolved: {sorted(missing_gap)}")
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
    fail("review-card", f"carbon-filter altLabels {sorted(actual_alts)} miss {sorted(expected_alts - actual_alts)}")
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
