"""The consequences of a publish, as four ordered steps (ADR-005 §5).

Publishing is the beginning of the work, not the end of it. A changed label
means nothing until the resolver index is rebuilt, held claims are re-resolved
and affected documents are re-indexed, and this module is those steps.

**Every step has the same signature: `(event) -> counts`.** The event is data --
which concept was published, which surface forms it added -- and never a client,
a context or an environment. That is what lets `apps/api/h2o_api/dispatch` be a
dispatcher rather than a second code path: it decides *who calls* these, never
*what they do*, and the moment one of them needs to know it is inside Step
Functions the whole arrangement collapses into two implementations.

Step 1's failure aborts the run, because every later step resolves against the
index it produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyoxigraph

from h2o_core import (
    chunking,
    config,
    embeddings,
    facts,
    gaps,
    graph,
    registry,
    resolve,
    resolver,
    store,
    vectors,
)

__all__ = [
    "STEP_NAMES",
    "finalise_run",
    "rebuild_resolver_index",
    "reindex_affected_documents",
    "reresolve_backlog",
]

H2O = "https://vocab.h2o.example/id/"

#: The order the state machine runs them in. `apps/api/tests` asserts this
#: matches 30-orchestration.yaml's Task states, because the two drift silently.
STEP_NAMES = ("rebuild_index", "reresolve_gaps", "reindex_documents", "record_run")


def _node(name: str) -> pyoxigraph.NamedNode:
    return pyoxigraph.NamedNode(H2O + name)


def rebuild_resolver_index(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompile normalised labels to concept ids and publish the artefact.

    First, and its failure aborts the run: without it nothing else matters,
    because every later step resolves against what this produces.

    The watermark is the dataset's own sha256, so rebuilding an unchanged graph
    yields the same watermark and staleness is diagnosable by comparing two
    strings rather than by guessing.
    """
    snapshot = graph.load()
    watermark = graph.digest(graph.dump(snapshot.store))
    index = resolver.build(snapshot.store, watermark, embed=embeddings.embed)
    resolver.publish(index)
    resolver.forget_cached()
    return {"watermark": watermark, "labels": len(index.by_label), "concepts": index.concept_count}


def held_surfaces(store_: pyoxigraph.Store) -> list[str]:
    """Every merge key currently holding a claim.

    Read off the `heldSurface` triples rather than by re-reading documents,
    which is the entire reason ingestion writes them.
    """
    found = {
        str(quad.object.value)
        for quad in store_.quads_for_pattern(
            None, _node("heldSurface"), None, pyoxigraph.NamedNode(config.FACTS_GRAPH)
        )
        if isinstance(quad.object, pyoxigraph.Literal)
    }
    return sorted(found)


def _promote(
    store_: pyoxigraph.Store, surface: str, concept_id: str, score: float
) -> tuple[int, set[str]]:
    """Attach every claim held under this surface form to its concept.

    The claim's IRI does not change -- it is a content hash of the evidence, and
    the evidence did not change. What changes is what it is *about*, which is
    exactly the ADR-002 promise that a held claim goes live without the document
    being read again.
    """
    facts_graph = pyoxigraph.NamedNode(config.FACTS_GRAPH)
    claims = [
        quad.subject
        for quad in store_.quads_for_pattern(
            None, _node("heldSurface"), pyoxigraph.Literal(surface), facts_graph
        )
    ]

    # Recorded before the claims change, and only for the claims that actually
    # move. Deriving it afterwards from "every claim about this concept" would
    # include documents that already resolved to it long ago, and step 3 would
    # re-embed files this publish did not touch.
    touched = {
        str(quad.object.value)
        for claim in claims
        for quad in store_.quads_for_pattern(claim, _node("sourceFile"), None, facts_graph)
        if isinstance(quad.object, pyoxigraph.Literal)
    }

    for claim in claims:
        store_.remove(
            pyoxigraph.Quad(claim, _node("heldSurface"), pyoxigraph.Literal(surface), facts_graph)
        )
        store_.remove(pyoxigraph.Quad(claim, _node("status"), _node("held"), facts_graph))
        for stage in ("abstain", "exact", "embedding"):
            store_.remove(
                pyoxigraph.Quad(claim, _node("resolvedBy"), _node(f"stage/{stage}"), facts_graph)
            )
        store_.add(pyoxigraph.Quad(claim, _node("status"), _node("active"), facts_graph))
        store_.add(pyoxigraph.Quad(claim, _node("subject"), _node(concept_id), facts_graph))
        store_.add(pyoxigraph.Quad(claim, _node("resolvedBy"), _node("stage/exact"), facts_graph))
        store_.add(
            pyoxigraph.Quad(
                claim,
                _node("resolutionScore"),
                pyoxigraph.Literal(
                    str(score),
                    datatype=pyoxigraph.NamedNode("http://www.w3.org/2001/XMLSchema#decimal"),
                ),
                facts_graph,
            )
        )
    return len(claims), touched


def reresolve_backlog(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Replay every held mention against the vocabulary as it now stands.

    Newly matching ones attach to their claims and go live; the gap entries they
    came from close with a pointer to the publish that closed them. Anything
    still unresolved stays held and stays in the queue, which is the honest
    outcome for a term the publish did not actually cover.
    """
    run_id = (event or {}).get("run_id", "")
    index = resolver.current()
    if index is None:
        raise RuntimeError("no resolver index; step 1 must run before this one")

    snapshot = graph.load()
    resolved: dict[str, str] = {}
    promoted = 0
    touched: set[str] = set()

    for surface in held_surfaces(snapshot.store):
        verdict = resolve.resolve(surface, index=index)
        if not verdict.matched or verdict.concept_id is None:
            continue
        count, files = _promote(snapshot.store, surface, verdict.concept_id, verdict.score)
        promoted += count
        touched |= files
        resolved[surface] = verdict.concept_id

    documents = sorted(touched)
    if promoted:
        graph.put(graph.dump(snapshot.store), snapshot.etag)

    for surface, concept_id in resolved.items():
        gaps.close(surface, concept_id=concept_id, run_id=run_id)

    return {
        "mentions_resolved": promoted,
        "gaps_closed": len(resolved),
        "surfaces": sorted(resolved),
        "documents": documents,
    }


def reindex_affected_documents(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Re-embed only the documents whose claims just changed meaning.

    Not a full corpus re-run. A chunk's filterable `concept` metadata is what
    concept-scoped retrieval searches on, and a chunk whose claims now resolve
    to CO₂ Cylinder has to be findable that way -- but a document that never
    mentioned the term has nothing to re-say.

    ADR-005 records the limit honestly: a document discussing gas cylinders
    without ever writing "gas bottle" is not re-indexed, because relevance here
    is lexical. The run says which documents were touched, so the omission is
    inspectable rather than invisible.
    """
    payload = event or {}
    affected = list(payload.get("documents") or [])

    if not affected:
        # Step 2 found these, and in the AWS arm this step does not receive step
        # 2's output: the state machine passes each Task the original `$.event`.
        # Threading it would mean editing the ASL to wire one step's ResultPath
        # into the next step's Payload, which puts the order of the fan-out in
        # two places -- the template and this module -- and they drift. The run
        # record is already the shared channel both arms write to, so this reads
        # it, and the local walker's in-memory hand-off stays a fast path rather
        # than the only path.
        for step in store.read_steps(str(payload.get("run_id", ""))):
            if step.get("name") == "reresolve_gaps":
                affected = list((step.get("counts") or {}).get("documents") or [])

    if not affected:
        return {"documents_reindexed": 0, "chunks": 0, "documents": []}

    snapshot = graph.load()
    by_document = _concepts_by_document(snapshot.store)

    reindexed, total_chunks = [], 0
    for filename in affected:
        record = registry.meta_for(filename)
        if record is None:
            continue
        raw = (
            graph.s3()
            .get_object(Bucket=config.RAW_DOCS_BUCKET, Key=filename)["Body"]
            .read()
            .decode("utf-8")
        )
        source = chunking.read_source(raw, is_html=record.is_html)
        chunks = chunking.chunk_document(source, filename)

        batch = []
        for chunk in chunks:
            concepts = sorted(by_document.get((filename, chunk.line_range), set()))
            batch.append(
                {
                    "key": f"{filename}#{chunk.line_range}",
                    "data": embeddings.embed_one(chunk.text),
                    "metadata": {
                        "source_file": filename,
                        "doc_type": record.doc_type.value,
                        "doc_version": record.doc_version,
                        "concept": concepts or ["_none"],
                        "snippet": chunk.text[:1000],
                        "line_range": chunk.line_range,
                    },
                }
            )
        # PutVectors overwrites by key, so the previous embedding for a chunk is
        # replaced rather than duplicated -- no delete pass to get wrong.
        total_chunks += vectors.put_chunks(batch)
        reindexed.append(filename)

    return {
        "documents_reindexed": len(reindexed),
        "chunks": total_chunks,
        "documents": reindexed,
    }


def _concepts_by_document(
    store_: pyoxigraph.Store,
) -> dict[tuple[str, str], set[str]]:
    """Which concepts each chunk's claims resolved to, keyed by line range."""
    found: dict[tuple[str, str], set[str]] = {}
    for row in facts.read_claims(store_):
        subject = str(row.get("subject") or "")
        if not subject:
            continue
        key = (str(row["source_file"]), str(row["line_range"]))
        found.setdefault(key, set()).add(subject.rsplit("/", 1)[-1])
    return found


def finalise_run(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Close the run record, so the console can say what the publish did.

    ADR-005 wants "published 12:04 · 43 mentions resolved · 6 documents
    reindexed", which is only renderable if the counts each step returned are
    recorded where the polling endpoint reads them.
    """
    payload = event or {}
    run_id = str(payload.get("run_id", ""))
    failed = payload.get("failed")

    # Totals composed from the step rows rather than from the event. Step
    # Functions threads its state through ResultPath and the local walker
    # accumulates a dict, so reading what the steps *recorded* is the one
    # source both arms already agree on -- and it is the same data /runs
    # returns, so the summary cannot disagree with the detail beneath it.
    counts: dict[str, Any] = dict(payload.get("counts") or {})
    for step in store.read_steps(run_id):
        for key, value in (step.get("counts") or {}).items():
            if isinstance(value, int):
                counts[key] = counts.get(key, 0) + value if key in counts else value

    envelope = {
        "run_id": run_id,
        "kind": "publish",
        "status": "failed" if failed else "succeeded",
        "started_at": str(payload.get("started_at") or _now()),
        "finished_at": _now(),
        "concept_id": str(payload.get("concept_id", "")),
        "counts": counts,
        # ADR-005's sentence: "published 12:04 · 43 mentions resolved · 6
        # documents reindexed". Rendered here so the console and the command
        # line read the same summary.
        "summary": _summary(counts),
    }
    if failed:
        envelope["error"] = str(failed)
    store.write_run({k: v for k, v in envelope.items() if v != ""})
    return {"recorded": run_id, "status": envelope["status"], "summary": envelope["summary"]}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _summary(counts: dict[str, Any]) -> str:
    parts = []
    if mentions := counts.get("mentions_resolved"):
        parts.append(f"{mentions} mention{'' if mentions == 1 else 's'} resolved")
    if documents := counts.get("documents_reindexed"):
        parts.append(f"{documents} document{'' if documents == 1 else 's'} reindexed")
    if closed := counts.get("gaps_closed"):
        parts.append(f"{closed} gap{'' if closed == 1 else 's'} closed")
    return " · ".join(parts)
