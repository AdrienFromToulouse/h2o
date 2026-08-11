"""Reading the published vocabulary: SPARQL row sets projected into models.

Every function here takes a loaded ``pyoxigraph.Store`` rather than fetching one,
so a caller that already has a snapshot -- the publish transaction, the resolver
index build -- does not pay for a second S3 read, and so these are testable
against a store built from the Turtle files with no AWS at all.
"""

from __future__ import annotations

from collections import defaultdict

import pyoxigraph

from h2o_core import config, graph, sparql
from h2o_core.models import (
    ConceptDetail,
    ConceptRef,
    LabelKind,
    LabelRow,
    MachineSignal,
    MappingKind,
    SchemeRef,
    SchemeTree,
    TechnicalDetail,
)

__all__ = [
    "concept",
    "concept_iri",
    "concept_labels",
    "scheme_id",
    "scheme_iri",
    "scheme_tree",
    "slug",
]

SKOS = "http://www.w3.org/2004/02/skos/core#"
OWL = "http://www.w3.org/2002/07/owl#"
DCT = "http://purl.org/dc/terms/"

#: Mapping properties, most precise first. ADR-003 keeps them distinct because
#: "close enough" is a domain judgement worth recording.
_MATCH_KINDS = {
    f"{SKOS}exactMatch": MappingKind.exact,
    f"{SKOS}closeMatch": MappingKind.close,
    f"{SKOS}broadMatch": MappingKind.broad,
}


def slug(iri: str) -> str:
    """The last path segment of an IRI: 'carbon-filter'.

    Slugs are what the API and the console exchange. An IRI is not a label and
    is not an interface (ADR-001, ADR-006), so it appears in a response only
    inside TechnicalDetail.
    """
    return iri.rsplit("/", 1)[-1]


def concept_iri(concept_id: str) -> str:
    return config.ID_NAMESPACE + concept_id


def scheme_iri(scheme_id: str) -> str:
    return config.SCHEME_NAMESPACE + scheme_id


def scheme_id(iri: str) -> str:
    return slug(iri)


def _text(term: object) -> str:
    """The lexical value of a term, without its language tag or datatype."""
    return getattr(term, "value", str(term))


def _language(term: object) -> str | None:
    language = getattr(term, "language", None)
    return str(language) if language else None


def concept_labels(store: pyoxigraph.Store) -> list[LabelRow]:
    """Every label in the published graph — the resolver index's raw material."""
    rows: list[LabelRow] = []
    for row in graph.rows(store, sparql.render("concept_labels_all.rq")):
        label = row["label"]
        rows.append(
            LabelRow(
                concept_id=slug(_text(row["concept"])),
                scheme_id=scheme_id(_text(row["scheme"])),
                text=_text(label),
                language=_language(label),
                kind=LabelKind(_text(row["label_type"])),
            )
        )
    return rows


def _children(store: pyoxigraph.Store, iri: str) -> list[ConceptRef]:
    query = sparql.render("concept_children.rq", concept=sparql.Iri(iri))
    return [
        ConceptRef(
            concept_id=slug(_text(row["child"])),
            pref_label=_text(row["pref_label"]),
            definition=_text(row["definition"]) if row["definition"] else None,
            child_count=int(_text(row["child_count"])),
        )
        for row in graph.rows(store, query)
    ]


def scheme_tree(store: pyoxigraph.Store, *, include_machine: bool = False) -> SchemeTree:
    """Every vocabulary and its top terms, in one pass.

    The telemetry scheme is excluded by default. It is a real concept scheme,
    but it holds firmware's names for things, and ADR-006 keeps those out of the
    interface a domain expert works in.
    """
    titles: dict[str, dict[str, str]] = defaultdict(dict)
    descriptions: dict[str, str] = {}
    counts: dict[str, int] = {}

    for row in graph.rows(store, sparql.render("scheme_list.rq")):
        identifier = scheme_id(_text(row["scheme"]))
        counts[identifier] = int(_text(row["concept_count"]))
        if row["title"] is not None:
            titles[identifier][_language(row["title"]) or "en"] = _text(row["title"])
        if row["description"] is not None:
            descriptions[identifier] = _text(row["description"])

    schemes: list[SchemeRef] = []
    top: dict[str, list[ConceptRef]] = {}
    for identifier in sorted(counts):
        if identifier == "telemetry" and not include_machine:
            continue
        by_language = titles.get(identifier, {})
        schemes.append(
            SchemeRef(
                scheme_id=identifier,
                title=by_language.get("en") or next(iter(by_language.values()), identifier),
                description=descriptions.get(identifier),
                concept_count=counts[identifier],
            )
        )
        query = sparql.render("scheme_browse.rq", scheme=sparql.Iri(scheme_iri(identifier)))
        top[identifier] = [
            ConceptRef(
                concept_id=slug(_text(row["concept"])),
                pref_label=_text(row["pref_label"]),
                definition=_text(row["definition"]) if row["definition"] else None,
                child_count=int(_text(row["child_count"])),
            )
            for row in graph.rows(store, query)
        ]

    return SchemeTree(schemes=schemes, top_concepts=top)


def _machine_signals(store: pyoxigraph.Store, iri: str) -> list[MachineSignal]:
    """Telemetry concepts pointing at this business concept.

    Read through the inverse of the mapping property, because the alignment is
    authored on the machine side: firmware versions independently, and a rename
    there must not look like a vocabulary change (ADR-003).
    """
    target = pyoxigraph.NamedNode(iri)
    published = pyoxigraph.NamedNode(config.PUBLISHED_GRAPH)
    signals: list[MachineSignal] = []

    for property_iri, kind in _MATCH_KINDS.items():
        predicate = pyoxigraph.NamedNode(property_iri)
        for quad in store.quads_for_pattern(None, predicate, target, published):
            source = quad.subject
            signals.append(
                MachineSignal(
                    signal=_first_object(store, source, f"{config.ID_NAMESPACE}otelSignal"),
                    unit=_first_object(store, source, f"{config.ID_NAMESPACE}otelUnit"),
                    notation=_first_object(store, source, f"{SKOS}notation"),
                    match=kind,
                    scope_note=_first_object(store, source, f"{SKOS}scopeNote"),
                )
            )
    return sorted(signals, key=lambda s: (s.signal or "", s.notation or ""))


def _first_object(store: pyoxigraph.Store, subject: object, predicate_iri: str) -> str | None:
    predicate = pyoxigraph.NamedNode(predicate_iri)
    published = pyoxigraph.NamedNode(config.PUBLISHED_GRAPH)
    for quad in store.quads_for_pattern(subject, predicate, None, published):  # type: ignore[arg-type]
        return _text(quad.object)
    return None


def concept(store: pyoxigraph.Store, concept_id: str) -> ConceptDetail | None:
    """The full review-card payload for one term, or None if it does not exist."""
    iri = concept_iri(concept_id)
    rows = graph.rows(store, sparql.render("concept_get.rq", concept=sparql.Iri(iri)))
    if not rows:
        return None

    pref: dict[str, str] = {}
    definition: dict[str, str] = {}
    alt: list[str] = []
    hidden: list[str] = []
    related: list[ConceptRef] = []
    parent: ConceptRef | None = None
    replaced_by: ConceptRef | None = None
    scheme_ref: SchemeRef | None = None
    notation = scope_note = modified = contributor = change_note = None
    version = 1
    deprecated = False

    for row in rows:
        predicate = _text(row["predicate"])
        value = row["value"]
        text = _text(value)
        language = _language(value) or "en"
        target = _text(row["target_label"]) if row["target_label"] else slug(text)

        match predicate:
            case p if p == f"{SKOS}prefLabel":
                pref[language] = text
            case p if p == f"{SKOS}definition":
                definition[language] = text
            case p if p == f"{SKOS}altLabel":
                alt.append(text)
            case p if p == f"{SKOS}hiddenLabel":
                hidden.append(text)
            case p if p == f"{SKOS}notation":
                notation = text
            case p if p == f"{SKOS}scopeNote":
                scope_note = text
            case p if p == f"{SKOS}changeNote":
                change_note = text
            case p if p == f"{SKOS}inScheme":
                scheme_ref = SchemeRef(scheme_id=scheme_id(text), title=scheme_id(text))
            case p if p == f"{SKOS}broader":
                parent = ConceptRef(concept_id=slug(text), pref_label=target)
            case p if p == f"{SKOS}related":
                related.append(ConceptRef(concept_id=slug(text), pref_label=target))
            case p if p == f"{OWL}versionInfo":
                version = int(text)
            case p if p == f"{OWL}deprecated":
                deprecated = text.lower() == "true"
            case p if p == f"{DCT}modified":
                modified = text
            case p if p == f"{DCT}contributor":
                contributor = text
            case p if p == f"{DCT}isReplacedBy":
                replaced_by = ConceptRef(concept_id=slug(text), pref_label=target)

    if scheme_ref is not None:
        tree_counts = {s.scheme_id: s for s in scheme_tree(store, include_machine=True).schemes}
        scheme_ref = tree_counts.get(scheme_ref.scheme_id, scheme_ref)

    return ConceptDetail(
        concept_id=concept_id,
        pref_label=pref,
        definition=definition,
        alt_labels=sorted(alt),
        hidden_labels=sorted(hidden),
        notation=notation,
        scope_note=scope_note,
        scheme=scheme_ref,
        parent=parent,
        children=_children(store, iri),
        related=sorted(related, key=lambda r: r.pref_label),
        machine_signals=_machine_signals(store, iri),
        version=version,
        modified=modified,
        contributor=contributor,
        change_note=change_note,
        deprecated=deprecated,
        replaced_by=replaced_by,
        technical=TechnicalDetail(
            iri=iri,
            scheme_iri=scheme_iri(scheme_ref.scheme_id) if scheme_ref else "",
        ),
    )
