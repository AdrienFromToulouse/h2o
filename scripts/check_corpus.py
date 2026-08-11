#!/usr/bin/env python3
"""Check the document corpus against what the ADRs claim it contains.

The corpus exists to make the chain demonstrable, so its properties are
assertions rather than prose:

1. Registry and directory agree (ADR-002 step 1: registered, then ingested).
2. The seeded contradictions are really present, with disagreeing values in
   different documents (ADR-002).
3. The seeded gap really resolves to nothing, and appears the number of times
   the README and ADR-004 say it does.
4. The HTML document really contains entities, so the lossless de-markup rule
   has something to bite on (ADR-002).
5. The corpus actually exercises the vocabulary: how many concepts it mentions,
   and by which label.

`normalise` and `flatten_html` are imported from h2o_core. Check 3 asks whether
"gas bottle" resolves to nothing, and check 4 asks whether a citation quotes the
readable price rather than the entity; both are only meaningful if they run the
functions ingestion and the resolver actually use.

    uv run python scripts/check_corpus.py

Exit code 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from h2o_core.normalize import flatten_html, normalise
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF, SKOS

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "data" / "docs"
REGISTRY = DOCS / "registry.json"
VOCAB = ROOT / "vocab"

HS = Namespace("https://vocab.h2o.example/scheme/")

# What the README loop and ADR-004's AddAltLabel row promise.
EXPECTED_GAP_MENTIONS = 12
EXPECTED_GAP_DOCUMENTS = 3

failures: list[str] = []
notes: list[str] = []


def fail(check: str, detail: str) -> None:
    failures.append(f"{check}: {detail}")


def readable(path: Path) -> str:
    text = path.read_text()
    return flatten_html(text) if path.suffix == ".html" else text


# ---------------------------------------------------- 1. registry vs directory

if not REGISTRY.exists():
    print(f"missing {REGISTRY.relative_to(ROOT)}", file=sys.stderr)
    sys.exit(1)

registry = json.loads(REGISTRY.read_text())
registered = {d["filename"]: d for d in registry["documents"]}
on_disk = {p.name for p in DOCS.iterdir() if p.name != "registry.json"}

for missing in sorted(set(registered) - on_disk):
    fail("registry", f"{missing} is registered but not on disk")
for unregistered in sorted(on_disk - set(registered)):
    fail("registry", f"{unregistered} is on disk but not registered; ADR-002 forbids guessing")

for name, entry in sorted(registered.items()):
    for field in ("doc_type", "doc_version", "issued", "format", "authority"):
        if not entry.get(field):
            fail("registry", f"{name} has no {field}")

if not failures:
    notes.append(f"OK  registry: {len(registered)} documents, directory and registry agree")

docs = {name: readable(DOCS / name) for name in sorted(registered) if (DOCS / name).exists()}


# ------------------------------------------- 2. the contradictions are present

for conflict in registry["seeded_contradictions"]:
    subject = conflict["subject"]
    values_by_source: dict[str, str] = {}
    for claim in conflict["claims"]:
        source, value = claim["source"], claim["value"]
        body = docs.get(source)
        if body is None:
            fail("contradiction", f"{subject}: source {source} not readable")
            continue
        if value not in body:
            fail("contradiction", f"{subject}: '{value}' not found verbatim in {source}")
        values_by_source[source] = value

    distinct = set(values_by_source.values())
    if len(distinct) < 2:
        fail("contradiction", f"{subject}: all sources agree on {distinct}; nothing to flag")
    elif len(values_by_source) >= 2:
        notes.append(
            f"OK  contradiction on {subject}: {sorted(distinct)} "
            f"across {len(values_by_source)} documents ({conflict['kind']})"
        )


# --------------------------- 3. the seeded gap, counted and proven unresolvable

surface = registry["seeded_gap"]["surface_form"]
pattern = re.compile(re.escape(surface).replace(r"\ ", r"\s+"), re.IGNORECASE)

per_doc = {name: len(pattern.findall(body)) for name, body in docs.items()}
hits = {name: n for name, n in per_doc.items() if n}
total = sum(hits.values())

if total != EXPECTED_GAP_MENTIONS:
    fail("seeded-gap", f"'{surface}' appears {total} times, expected {EXPECTED_GAP_MENTIONS}")
if len(hits) != EXPECTED_GAP_DOCUMENTS:
    fail(
        "seeded-gap", f"'{surface}' spans {len(hits)} documents, expected {EXPECTED_GAP_DOCUMENTS}"
    )
if total == EXPECTED_GAP_MENTIONS and len(hits) == EXPECTED_GAP_DOCUMENTS:
    spread = ", ".join(f"{n} in {name.split('-', 1)[0]}" for name, n in sorted(hits.items()))
    notes.append(f"OK  seeded gap: '{surface}' x{total} across {len(hits)} documents ({spread})")

# The gap must genuinely resolve to nothing against the published vocabulary.
vocab = Graph()
for path in sorted(VOCAB.glob("*.ttl")):
    vocab.parse(path, format="turtle")

concepts = set(vocab.subjects(predicate=RDF.type, object=SKOS.Concept))
label_index: dict[str, URIRef] = {}
for c in concepts:
    for pred in (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel):
        for lit in vocab.objects(c, pred):
            label_index[normalise(str(lit))] = c

if normalise(surface) in label_index:
    fail("seeded-gap", f"'{surface}' now resolves; the corpus demo needs it unresolved")
else:
    notes.append(f"OK  '{surface}' resolves to nothing, so all {total} mentions hold their claims")


# --------------------------------- 4. the HTML document exercises the entity rule

html_docs = [n for n, e in registered.items() if e["format"] == "html"]
if not html_docs:
    fail("html-rule", "no HTML document in the corpus; ADR-002's de-markup rule is untested")
for name in html_docs:
    raw = (DOCS / name).read_text()
    entities = sorted(set(re.findall(r"&[a-zA-Z#][a-zA-Z0-9]*;", raw)))
    if not entities:
        fail("html-rule", f"{name} has no character entities to flatten")
        continue
    flat = flatten_html(raw)
    if "&pound;" in flat or "<" in flat:
        fail("html-rule", f"{name} did not flatten cleanly")
    # A quoted price must match what a reader sees, not the raw markup.
    if "£1,249.00" not in flat:
        fail("html-rule", f"{name}: flattened text does not contain the readable price")
    else:
        notes.append(
            f"OK  {name}: {len(entities)} entity types flatten losslessly "
            f"({' '.join(entities[:5])}); a citation quotes '£1,249.00', not '&pound;'"
        )


# ------------------------------------ 5. the corpus actually exercises the vocabulary

business = {c for c in concepts if HS.telemetry not in set(vocab.objects(c, SKOS.inScheme))}
corpus_text = normalise(" ".join(docs.values()))

mentioned: dict[URIRef, str] = {}
for c in business:
    for pred in (SKOS.prefLabel, SKOS.altLabel):
        for lit in vocab.objects(c, pred):
            label = normalise(str(lit))
            if label and f" {label} " in f" {corpus_text} ":
                mentioned.setdefault(c, str(lit))

coverage = len(mentioned) * 100 // len(business) if business else 0
if len(mentioned) < 20:
    fail(
        "coverage",
        f"only {len(mentioned)} concepts mentioned; the corpus barely exercises the vocabulary",
    )
else:
    notes.append(
        f"OK  corpus mentions {len(mentioned)} of {len(business)} business concepts ({coverage}%)"
    )

by_scheme: dict[str, int] = defaultdict(int)
for c in mentioned:
    scheme = next(iter(vocab.objects(c, SKOS.inScheme)), None)
    by_scheme[str(scheme).rsplit("/", 1)[-1]] += 1


# ---------------------------------------------------------------- report

print("h2o corpus check")
print("-" * 60)
for line in notes:
    print(f"  {line}")
print(f"  concepts mentioned per scheme: {dict(sorted(by_scheme.items()))}")
words = sum(len(b.split()) for b in docs.values())
print(f"  {len(docs)} documents, ~{words:,} words")
print("-" * 60)

if failures:
    print(f"FAILED: {len(failures)} problem(s)")
    for f in failures:
        print(f"  x {f}")
    sys.exit(1)

print("PASSED: registry, seeded contradictions, seeded gap, HTML rule, vocabulary coverage")
sys.exit(0)
