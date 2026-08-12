"""Claims, in the graph.

ADR-005 puts extracted claims in ``h2o:graph/facts`` rather than in DynamoDB,
alongside the vocabulary they are resolved against. This module writes them
there and reads them back.

**Claim IRIs are content hashes, and that is most of what makes re-ingestion
idempotent.** The IRI is derived from source file, version, line range,
predicate, value and unit, so re-running an unchanged document produces the same
IRIs and inserting them again is a no-op under RDF's set semantics. kai needed a
carefully widened DynamoDB sort key and a comment explaining that too narrow a
key makes BatchWriteItem reject the whole run; here the property falls out of the
data model. This is the clearest single place the graph choice pays for itself.

It is only *most* of it, and the gap is worth naming because it was found the
expensive way. The hash covers six fields and `snippet` is not among them, so a
document whose text is read differently -- the HTML de-markup fix did exactly
this -- comes back as the same claim wearing a second, contradictory snippet.
`retract_document` closes that: a document is cleared before it is re-read, so
"idempotent" means the graph ends up describing the document, not the union of
every way the document has ever been read.

A held claim -- one whose subject resolved to nothing -- is stored exactly like
any other, with ``h2o:status h2o:held`` and the surface form recorded instead of
a concept. It is not a lesser record: ADR-002 keeps it so that when the
vocabulary catches up the claim goes live without the document being read again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pyoxigraph

from h2o_core import config
from h2o_core.gaps import gap_key

__all__ = ["Claim", "claim_iri", "insert", "read_claims", "retract_document", "to_quads"]

H2O = config.ID_NAMESPACE
XSD = "http://www.w3.org/2001/XMLSchema#"


@dataclass
class Claim:
    """One extracted fact, with everything needed to cite and explain it."""

    subject_concept: str | None
    subject_surface: str
    predicate: str
    value: str
    source_file: str
    doc_version: str
    line_range: str
    snippet: str
    unit: str | None = None
    confidence: float = 0.8
    #: Which cascade stage attached this claim, and at what score. Stored so
    #: ADR-002's "any concept link can be explained after the fact" is a triple
    #: rather than a log line that has since rotated.
    resolved_by: str = "abstain"
    resolution_score: float = 0.0
    run_id: str | None = None
    conflicts_with: list[str] = field(default_factory=list)

    @property
    def held(self) -> bool:
        return self.subject_concept is None

    @property
    def claim_id(self) -> str:
        return claim_iri(self)

    @property
    def group_subject(self) -> str:
        """What contradiction detection groups on.

        A held claim groups by its normalised surface form, so two unresolved
        mentions of the same unnamed thing can still contradict each other.
        """
        return self.subject_concept or f"held:{gap_key(self.subject_surface)}"


def claim_iri(claim: Claim) -> str:
    """Content-addressed, so the same fact from the same place is the same IRI."""
    seed = " ".join(
        [
            claim.source_file,
            claim.doc_version,
            claim.line_range,
            claim.predicate,
            claim.value,
            claim.unit or "",
        ]
    )
    return f"{H2O}claim/{hashlib.sha1(seed.encode('utf-8')).hexdigest()}"  # noqa: S324 - identity, not security


def _literal(value: str) -> pyoxigraph.Literal:
    return pyoxigraph.Literal(value)


def _decimal(value: float) -> pyoxigraph.Literal:
    return pyoxigraph.Literal(str(value), datatype=pyoxigraph.NamedNode(f"{XSD}decimal"))


def to_quads(claim: Claim) -> list[pyoxigraph.Quad]:
    """Serialise one claim into the facts graph."""
    subject = pyoxigraph.NamedNode(claim.claim_id)
    graph_name = pyoxigraph.NamedNode(config.FACTS_GRAPH)

    def quad(predicate: str, obj: pyoxigraph.Literal | pyoxigraph.NamedNode) -> pyoxigraph.Quad:
        return pyoxigraph.Quad(subject, pyoxigraph.NamedNode(H2O + predicate), obj, graph_name)

    quads = [
        pyoxigraph.Quad(
            subject,
            pyoxigraph.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
            pyoxigraph.NamedNode(f"{H2O}Claim"),
            graph_name,
        ),
        quad("predicate", _literal(claim.predicate)),
        quad("value", _literal(claim.value)),
        quad("sourceFile", _literal(claim.source_file)),
        quad("docVersion", _literal(claim.doc_version)),
        quad("lineRange", _literal(claim.line_range)),
        quad("snippet", _literal(claim.snippet)),
        quad("confidence", _decimal(claim.confidence)),
        quad("resolvedBy", pyoxigraph.NamedNode(f"{H2O}stage/{claim.resolved_by}")),
        quad("resolutionScore", _decimal(claim.resolution_score)),
        quad("subjectSurface", _literal(claim.subject_surface)),
        quad("status", pyoxigraph.NamedNode(f"{H2O}{'held' if claim.held else 'active'}")),
    ]

    if claim.subject_concept:
        quads.append(quad("subject", pyoxigraph.NamedNode(H2O + claim.subject_concept)))
    else:
        # The merge key, so the publish fan-out can find every claim a newly
        # added label would resolve without re-reading any document.
        quads.append(quad("heldSurface", _literal(gap_key(claim.subject_surface))))

    if claim.unit:
        quads.append(quad("unit", _literal(claim.unit)))
    if claim.run_id:
        quads.append(quad("runId", _literal(claim.run_id)))
    for other in claim.conflicts_with:
        quads.append(quad("conflictsWith", pyoxigraph.NamedNode(other)))

    return quads


def retract_document(store: pyoxigraph.Store, source_file: str) -> int:
    """Remove every claim that came from one document. Returns the count.

    **Re-ingestion is idempotent only for quads that come back byte-identical**,
    and that is a narrower promise than `insert`'s docstring reads. The IRI
    hashes six fields and `snippet` is not one of them, so a document re-read
    after any change to how it is flattened yields the *same* claim carrying a
    *second* `h2o:snippet` -- and every consumer does a single-valued read, so
    the console shows whichever row SPARQL happens to return. Change something
    that *is* hashed, such as a line range, and the old claim is instead
    orphaned with nothing to delete it.

    Neither is hypothetical: the HTML de-markup fix moved both. So a document is
    retracted before it is re-read, which is the same move `fanout._restate`
    makes and for the same reason -- clearing the whole thing beats patching,
    because the interesting case is always the triple that should no longer be
    there.

    Scoped to one document, by `sourceFile`. A re-ingest of one file must not
    touch claims evidenced from another.
    """
    graph_name = pyoxigraph.NamedNode(config.FACTS_GRAPH)
    subjects = {
        quad.subject
        for quad in store.quads_for_pattern(
            None, pyoxigraph.NamedNode(f"{H2O}sourceFile"), _literal(source_file), graph_name
        )
    }
    for subject in subjects:
        for quad in list(store.quads_for_pattern(subject, None, None, graph_name)):
            store.remove(quad)
    return len(subjects)


def insert(store: pyoxigraph.Store, claims: list[Claim]) -> int:
    """Add claims to the facts graph.

    Re-inserting a byte-identical claim is a no-op, because the IRI is a hash of
    the content and RDF is a set. That covers the unchanged case only; see
    `retract_document` for why the pipeline clears a document first rather than
    relying on it.
    """
    before = len(store)
    for claim in claims:
        for quad in to_quads(claim):
            store.add(quad)
    return len(store) - before


def flag_conflict(store: pyoxigraph.Store, claim_ids: list[str]) -> None:
    """Mark every claim in a group as conflicting with the others.

    Symmetric and complete: no consumer can read one value as settled by
    happening to fetch the claim that was written first (ADR-002).
    """
    graph_name = pyoxigraph.NamedNode(config.FACTS_GRAPH)
    predicate = pyoxigraph.NamedNode(f"{H2O}conflictsWith")
    for one in claim_ids:
        for other in claim_ids:
            if one != other:
                store.add(
                    pyoxigraph.Quad(
                        pyoxigraph.NamedNode(one),
                        predicate,
                        pyoxigraph.NamedNode(other),
                        graph_name,
                    )
                )


def read_claims(store: pyoxigraph.Store, *, concept_id: str | None = None) -> list[dict[str, Any]]:
    """Read claims back, optionally for one concept."""
    from h2o_core import graph, sparql

    if concept_id:
        query = sparql.render(
            "facts_for_concept.rq",
            concept=sparql.Iri(H2O + concept_id),
        )
    else:
        query = sparql.render("facts_all.rq")

    return graph.records(store, query)
