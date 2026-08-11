"""Publishing a concept: one atomic update, gated on the graph it produces.

ADR-005 §2. The sequence, and why each step is where it is::

    1. graph.load()                  GetObject -> (store, etag)
    2. concept_version.rq            history = h2o:graph/history/{slug}/{n}
    3. archive + insert              in memory only, on a scratch copy
    4. integrity.validate(store)     the gate, on the POST-change graph
    5. graph.dump(store)             sorted N-Quads, byte-reproducible
    6. PutObject(IfMatch=etag)       exactly one durable write
    7. 412 -> reload and replay      exhausted -> ConcurrentPublishError

**Steps 3 to 5 touch memory only.** The store came from S3 and goes back to S3
once; any failure before step 6 leaves the dataset exactly as it was, because
nothing was written. That is the whole reason the transaction is shaped this
way rather than as a sequence of updates.

**The gate runs after the change, never before.** A label collision only exists
once the label is inserted, so validating the pre-change graph would be a gate
that passes while the index it protects collides -- the precise failure
ADR-005 §3 exists to prevent.

**A retry replays the draft, not a diff.** On a 412 the whole transaction runs
again against the newly loaded graph, so two publishes to different concepts
both land, and two to the same concept produce a second version rather than a
silent merge of two people's edits.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyoxigraph
from botocore.exceptions import ClientError

from h2o_core import config, gaps, graph, impact, integrity, sparql, store, vocabulary
from h2o_core.impact import ConceptDraft

__all__ = ["IntegrityError", "PublishResult", "publish", "new_publish_id"]

SKOS = "http://www.w3.org/2004/02/skos/core#"
OWL = "http://www.w3.org/2002/07/owl#"
DCT = "http://purl.org/dc/terms/"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


class IntegrityError(RuntimeError):
    """The gate refused the graph this publish would have produced.

    Carries the findings rather than a message, because ADR-006 renders each
    `sh:resultMessage` verbatim to a curator and a joined string would have to
    be split apart again.
    """

    def __init__(self, findings: list[integrity.Finding]) -> None:
        super().__init__("; ".join(finding.message for finding in findings))
        self.findings = findings


@dataclass
class PublishResult:
    publish_id: str
    concept_id: str
    version: int
    watermark: str
    history_graph: str
    #: Gap entries this publish closes, for the fan-out to mark actioned.
    closes: list[str] = field(default_factory=list)
    #: Held surface forms the new labels would resolve, so step 2 of the
    #: fan-out knows what to look for without recomputing the draft.
    surfaces: list[str] = field(default_factory=list)
    attempts: int = 1
    warnings: list[str] = field(default_factory=list)


def new_publish_id() -> str:
    return f"publish-{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def history_graph(concept_id: str, version: int) -> str:
    return f"{config.HISTORY_GRAPH_PREFIX}{concept_id}/{version}"


def current_version(scratch: pyoxigraph.Store, concept_id: str) -> int:
    """The version the published graph currently carries, or 0 for a new term."""
    rows = graph.records(
        scratch,
        sparql.render("concept_version.rq", concept=sparql.Iri(vocabulary.concept_iri(concept_id))),
    )
    versions = [int(row["version"]) for row in rows if str(row.get("version", "")).isdigit()]
    return max(versions) if versions else 0


def _quads(draft: ConceptDraft, version: int, author: str) -> list[pyoxigraph.Quad]:
    """The reviewed concept, as quads.

    Built here rather than generated into a SPARQL update, because a concept has
    a variable number of labels and this repo's one rule for SPARQL is that
    placeholders bind RDF terms only. See archive_concept.ru.
    """
    subject = pyoxigraph.NamedNode(vocabulary.concept_iri(draft.concept_id))
    published = pyoxigraph.NamedNode(config.PUBLISHED_GRAPH)

    def quad(predicate: str, obj: Any) -> pyoxigraph.Quad:
        return pyoxigraph.Quad(subject, pyoxigraph.NamedNode(predicate), obj, published)

    def text(value: str, language: str = "en") -> pyoxigraph.Literal:
        return pyoxigraph.Literal(value, language=language)

    quads = [
        quad(RDF_TYPE, pyoxigraph.NamedNode(f"{SKOS}Concept")),
        quad(f"{OWL}versionInfo", pyoxigraph.Literal(str(version))),
        quad(f"{DCT}modified", pyoxigraph.Literal(_now())),
        quad(f"{DCT}contributor", pyoxigraph.Literal(author)),
    ]

    if draft.pref_label:
        quads.append(quad(f"{SKOS}prefLabel", text(draft.pref_label)))
    for label in draft.alt_labels:
        quads.append(quad(f"{SKOS}altLabel", text(label)))
    for label in draft.hidden_labels:
        quads.append(quad(f"{SKOS}hiddenLabel", text(label)))
    if draft.definition:
        quads.append(quad(f"{SKOS}definition", text(draft.definition)))
    if draft.scope_note:
        quads.append(quad(f"{SKOS}scopeNote", text(draft.scope_note)))
    if draft.change_note:
        # The expert's own reason, in their words (ADR-005 §1).
        quads.append(quad(f"{SKOS}changeNote", text(draft.change_note)))
    if draft.scheme_id:
        quads.append(
            quad(f"{SKOS}inScheme", pyoxigraph.NamedNode(vocabulary.scheme_iri(draft.scheme_id)))
        )
    if draft.broader:
        quads.append(
            quad(f"{SKOS}broader", pyoxigraph.NamedNode(vocabulary.concept_iri(draft.broader)))
        )
    for related in draft.related:
        quads.append(quad(f"{SKOS}related", pyoxigraph.NamedNode(vocabulary.concept_iri(related))))

    if version > 1:
        quads.append(
            quad(
                "http://www.w3.org/ns/prov#wasRevisionOf",
                pyoxigraph.NamedNode(history_graph(draft.concept_id, version - 1)),
            )
        )
    return quads


def _apply(scratch: pyoxigraph.Store, draft: ConceptDraft, version: int, author: str) -> str:
    """Archive the outgoing version and insert the reviewed one. Memory only."""
    frozen = history_graph(draft.concept_id, version - 1) if version > 1 else ""
    if frozen:
        scratch.update(
            sparql.render(
                "archive_concept.ru",
                concept=sparql.Iri(vocabulary.concept_iri(draft.concept_id)),
                history=sparql.Iri(frozen),
            )
        )
    for quad in _quads(draft, version, author):
        scratch.add(quad)
    return frozen


def publish(
    draft: ConceptDraft,
    *,
    author: str = "console",
    publish_id: str | None = None,
    client: Any = None,
    audit_table: Any = None,
    gaps_table: Any = None,
) -> PublishResult:
    """Run the whole transaction, retrying on a lost race.

    Returns once the dataset carries the new version. The consequences of that
    -- rebuilding the index, re-resolving held claims, re-indexing documents --
    are the fan-out's job (ADR-005 §5), triggered by the caller.
    """
    identifier = publish_id or new_publish_id()
    last_error: Exception | None = None

    for attempt in range(1, config.PUBLISH_ATTEMPTS + 1):
        # Reloaded on every attempt. A retry replays the draft against whatever
        # is there now, which is what lets two publishes to different concepts
        # both survive.
        snapshot = graph.load(client=client)
        scratch = snapshot.store

        version = current_version(scratch, draft.concept_id) + 1
        preview = impact.preview(scratch, draft, validate=False)
        frozen = _apply(scratch, draft, version, author)

        # The gate, on the graph this publish would produce.
        findings = integrity.validate(scratch)
        if blocking := integrity.blocking(findings):
            # Nothing has been written. The scratch store is discarded with the
            # attempt, so a refused publish leaves no trace at all.
            raise IntegrityError(blocking)

        payload = graph.dump(scratch)
        try:
            graph.put(payload, snapshot.etag, client=client)
        except graph.ConcurrentPublishError as clash:
            last_error = clash
            # Somebody else's publish landed between our read and our write.
            # Brief, jittered by attempt so two racers do not resynchronise.
            time.sleep(0.1 * attempt)
            continue

        result = PublishResult(
            publish_id=identifier,
            concept_id=draft.concept_id,
            version=version,
            watermark=graph.digest(payload),
            history_graph=frozen,
            surfaces=preview.surfaces,
            closes=_gap_ids(preview.surfaces, gaps_table=gaps_table),
            attempts=attempt,
            warnings=[f.message for f in findings if not f.blocks],
        )
        _audit(result, draft, author, table_resource=audit_table)
        return result

    raise graph.ConcurrentPublishError(
        f"{draft.concept_id} lost {config.PUBLISH_ATTEMPTS} races to another publish"
    ) from last_error


def _gap_ids(surfaces: list[str], *, gaps_table: Any = None) -> list[str]:
    """Which queue entries this publish answers.

    Read before the fan-out closes them, so the run record can say what it
    closed even if a later step fails.
    """
    found = []
    for surface in surfaces:
        try:
            if gaps.exists(surface, table_resource=gaps_table):
                found.append(surface)
        except (ClientError, ValueError):
            # This runs *after* the conditional PUT has landed. The queue is
            # operational data and the vocabulary is the governed asset, so an
            # unreachable or malformed gap row must not raise out of a publish
            # that is already durable -- the caller would read the exception as
            # "it did not publish", and republishing would mint a second
            # version of an identical concept.
            continue
    return found


def _audit(
    result: PublishResult, draft: ConceptDraft, author: str, *, table_resource: Any = None
) -> None:
    """Who changed what, when, and why, per ADR-005 §1.

    Written after the durable write, deliberately. The audit trail records
    publishes that happened; writing it first would let a lost ETag race leave
    a row describing a change nobody made.
    """
    item = {
        "concept_id": result.concept_id,
        "published_at": _now(),
        "run_id": result.publish_id,
        "version": result.version,
        "author": author,
        "change_note": draft.change_note or "",
        "history_graph": result.history_graph,
        "watermark": result.watermark,
        "closes": result.closes,
        "draft": draft.model_dump(mode="json"),
    }
    target = table_resource or store.audit_table()
    target.put_item(Item=store.to_dynamo(item))
