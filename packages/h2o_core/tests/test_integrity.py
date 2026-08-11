"""The integrity gate, and the one test that justifies it being two mechanisms.

ADR-005 §3 splits the gate: SHACL owns the structural constraints, Python owns
resolver parity. That split costs a reviewer something -- they have to know
which file holds which rule -- and it is worth paying for exactly one reason,
which `test_shacl_passes_a_collision_only_python_can_see` demonstrates.
"""

from __future__ import annotations

import pyoxigraph
from h2o_core import config, integrity

VOCAB = "https://vocab.h2o.example/id/"
SKOS = "http://www.w3.org/2004/02/skos/core#"


def add_label(store: pyoxigraph.Store, concept_id: str, text: str, kind: str = "altLabel") -> None:
    store.add(
        pyoxigraph.Quad(
            pyoxigraph.NamedNode(f"{VOCAB}{concept_id}"),
            pyoxigraph.NamedNode(f"{SKOS}{kind}"),
            pyoxigraph.Literal(text, language="en"),
            pyoxigraph.NamedNode(config.PUBLISHED_GRAPH),
        )
    )


def test_the_seed_vocabulary_passes_its_own_gate(store: pyoxigraph.Store) -> None:
    """What ships is what is validated. `make check` asserts the same thing
    against the raw Turtle; this asserts it through the library the console and
    the publish path will actually call."""
    assert integrity.blocking(integrity.validate(store)) == []


def test_shacl_passes_a_collision_only_python_can_see(store: pyoxigraph.Store) -> None:
    """The one test that earns the two-mechanism gate.

    SPARQL can fold case and nothing more. The resolver also strips accents and
    punctuation, so "CO2 Cylinder" and the existing "CO₂ Cylinder" are the same
    label *to the index* and two different strings to `LCASE`. A gate that
    passed here would be a gate that certifies a graph whose index silently
    makes one of two concepts unreachable -- which is the precise failure
    ADR-005 §3 exists to prevent.
    """
    add_label(store, "mineral-cartridge", "CO2 Cylinder")

    findings = integrity.blocking(integrity.validate(store))

    assert findings, "a collision the resolver would see must block the publish"
    assert any("co2 cylinder" in finding.message.casefold() for finding in findings)


def test_a_finding_reads_as_a_sentence_to_a_curator(store: pyoxigraph.Store) -> None:
    """ADR-006: never a code, never a query, never SKOS jargon. The console
    renders this text verbatim, so the text is the interface."""
    add_label(store, "mineral-cartridge", "Carbon Filter")

    findings = integrity.blocking(integrity.validate(store))

    assert findings
    for finding in findings:
        assert finding.message
        for jargon in ("skos:", "sh:", "SELECT", "http://", "prefLabel", "NodeShape"):
            assert jargon not in finding.message, f"{jargon!r} reached a curator"


def test_a_duplicate_alt_label_inside_one_scheme_is_refused(store: pyoxigraph.Store) -> None:
    """The example ADR-005 gives: "Filter Cartridge is already an alternative
    term for ..." -- two concepts in one vocabulary cannot share a name."""
    add_label(store, "sediment-filter", "Filter Cartridge")

    assert integrity.blocking(integrity.validate(store))


def test_warnings_are_reported_and_do_not_block(store: pyoxigraph.Store) -> None:
    """ADR-005 check 6: an orphan concept is worth saying and not worth
    refusing. The seed vocabulary carries 17 undefined concepts, and a gate that
    blocked on those would have blocked every publish since M0."""
    findings = integrity.validate(store)

    assert any(finding.severity == "warning" for finding in findings)
    assert not integrity.blocking(findings)


def test_the_gate_validates_what_will_be_published(store: pyoxigraph.Store) -> None:
    """It reads the published graph, and only that one.

    The dataset also holds facts and drafts. Validating those against SKOS
    shapes would report violations about claims, which are not vocabulary and
    were never meant to satisfy these rules.
    """
    add_label(store, "mineral-cartridge", "Carbon Filter")
    store.add(
        pyoxigraph.Quad(
            pyoxigraph.NamedNode(f"{VOCAB}claim-1"),
            pyoxigraph.NamedNode(f"{VOCAB}value"),
            pyoxigraph.Literal("6 months"),
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
    )

    data = integrity.as_rdflib(store)

    assert not any("claim-1" in str(subject) for subject in data.subjects())
    assert integrity.blocking(integrity.validate(store)), "and the real collision still blocks"
