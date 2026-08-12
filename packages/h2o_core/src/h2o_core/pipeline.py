"""Ingestion: a deterministic pipeline that calls a model inside exactly one step.

ADR-002's six steps, in order:

  1. Register       the document and its version
  2. Normalize      chunk, embed, PutVectors
  3. Extract        Nova 2 Lite, forced tool call, verbatim gate  <- the only model
  4. Resolve        deterministic cascade; unresolved holds its claim + a gap
  5. Detect         contradictions, deterministically, with no model
  6. Persist        claims with provenance, flags, gaps, rejections, the run

The shape is the mirror image of the chat agent, which is a model that calls
deterministic tools. Never one prompt over a corpus; never a model free-writing
the graph.

Every AWS dependency arrives as an argument, so the whole pipeline runs against
fakes with no network and the ingest run is testable without FastAPI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyoxigraph

from h2o_core import conflicts, extraction, facts, gaps, registry, resolve, vectors
from h2o_core.chunking import chunk_document, read_source
from h2o_core.facts import Claim
from h2o_core.gaps import GapEvidence, GapSource, GapType
from h2o_core.registry import DocumentRecord
from h2o_core.resolve import Stage
from h2o_core.resolver import ResolverIndex

__all__ = ["IngestResult", "ingest_document", "ingest_corpus"]


@dataclass
class IngestResult:
    """What a run did, in numbers a console can show and a test can assert."""

    documents: int = 0
    chunks: int = 0
    vectors_written: int = 0
    facts_extracted: int = 0
    claims_active: int = 0
    claims_held: int = 0
    conflicts_found: int = 0
    gaps_recorded: int = 0
    #: Claims cleared before this document was re-read. Nonzero means the
    #: document had been ingested before, and is the number to look at when a
    #: re-run's totals move: they moved because the old reading went away.
    claims_retracted: int = 0
    rejections: list[extraction.Rejection] = field(default_factory=list)
    held_surfaces: dict[str, set[str]] = field(default_factory=dict)

    def merge(self, other: IngestResult) -> None:
        self.documents += other.documents
        self.chunks += other.chunks
        self.vectors_written += other.vectors_written
        self.facts_extracted += other.facts_extracted
        self.claims_active += other.claims_active
        self.claims_held += other.claims_held
        self.gaps_recorded += other.gaps_recorded
        self.claims_retracted += other.claims_retracted
        self.rejections.extend(other.rejections)
        for surface, files in other.held_surfaces.items():
            self.held_surfaces.setdefault(surface, set()).update(files)

    def as_counts(self) -> dict[str, int]:
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "vectors": self.vectors_written,
            "facts_extracted": self.facts_extracted,
            "claims_active": self.claims_active,
            "claims_held": self.claims_held,
            "conflicts": self.conflicts_found,
            "gaps": self.gaps_recorded,
            "rejections": len(self.rejections),
        }

    def as_steps(self) -> list[dict[str, Any]]:
        """The run's six rows, named for ADR-002's six steps.

        Rendered here rather than in the router so that the ingest run and the
        publish fan-out run reach `/runs/{id}` in the same shape -- ADR-005 asks
        the console to reuse one polling hook, and it can only do that if one
        step row means the same thing whichever run produced it.
        """
        return [
            {"name": "register", "counts": {"documents": self.documents}},
            {
                "name": "chunk_and_index",
                "counts": {"chunks": self.chunks, "vectors": self.vectors_written},
            },
            {
                "name": "extract",
                "counts": {"facts": self.facts_extracted, "rejections": len(self.rejections)},
            },
            {
                "name": "resolve",
                "counts": {
                    "active": self.claims_active,
                    "held": self.claims_held,
                    "gaps": self.gaps_recorded,
                },
            },
            {"name": "detect_conflicts", "counts": {"conflicts": self.conflicts_found}},
            {
                "name": "persist",
                "counts": {"claims": self.claims_active + self.claims_held},
            },
        ]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ingest_document(
    record: DocumentRecord,
    raw: str,
    store: pyoxigraph.Store,
    *,
    index: ResolverIndex | None,
    embed_one: Callable[[str], list[float]] | None = None,
    extract: Callable[..., extraction.Extraction] | None = None,
    run_id: str | None = None,
    gaps_table: Any = None,
    registry_table: Any = None,
    vectors_client: Any = None,
    write_vectors: bool = True,
) -> IngestResult:
    """Run all six steps over one document."""
    result = IngestResult(documents=1)

    # 1. Register. ADR-002: a document is registered and *then* ingested, never
    #    guessed at and never silently skipped. Registering first rather than on
    #    success means a run that dies mid-extraction still leaves a record that
    #    the document was attempted, which is the difference between a corpus
    #    with a known failure in it and a corpus with a hole nobody can see.
    if registry_table is not None:
        registry.register(record, table_resource=registry_table)

    # 2. Normalize -> chunk. The stored snippet is the original text; this
    #    reading exists so a citation can be checked and located.
    source = read_source(raw, is_html=record.is_html)
    chunks = chunk_document(source, record.filename)
    result.chunks = len(chunks)

    # 3. Extract. The only model call in the pipeline.
    run_extract = extract or extraction.extract_chunk
    extracted: list[dict[str, Any]] = []
    for chunk in chunks:
        outcome = run_extract(chunk, source, doc_type=record.doc_type.value)
        extracted.extend(outcome.facts)
        result.rejections.extend(outcome.rejections)
    result.facts_extracted = len(extracted)

    # 4. Resolve. An unresolved mention holds its claim rather than dropping it,
    #    and writes a gap carrying the verbatim sentence and its locator.
    claims: list[Claim] = []
    for fact in extracted:
        verdict = resolve.resolve(
            fact["subject"],
            index=index,
            embed=embed_one,
        )
        claim = Claim(
            subject_concept=verdict.concept_id,
            subject_surface=fact["subject"],
            predicate=fact["predicate"],
            value=fact["value"],
            unit=fact.get("unit"),
            source_file=record.filename,
            doc_version=record.doc_version,
            line_range=fact["line_range"],
            snippet=fact["snippet"],
            confidence=float(fact.get("confidence", 0.8)),
            resolved_by=verdict.stage.value,
            resolution_score=verdict.score,
            run_id=run_id,
        )
        claims.append(claim)

        if verdict.stage is Stage.abstain:
            result.claims_held += 1
            result.held_surfaces.setdefault(gaps.gap_key(fact["subject"]), set()).add(
                record.filename
            )
            gaps.record_miss(
                fact["subject"],
                source=GapSource.ingestion,
                evidence=GapEvidence(
                    source=GapSource.ingestion,
                    text=fact["snippet"],
                    locator=f"{record.filename}:{fact['line_range']}",
                    doc_version=record.doc_version,
                    occurred_at=_now(),
                ),
                gap_type=GapType.add_alt_label,
                suggestions=verdict.shortlist,
                run_id=run_id,
                table_resource=gaps_table,
            )
            result.gaps_recorded += 1
        else:
            result.claims_active += 1

    # 2 (continued). Embed and index. Done after resolution so the filterable
    #    `concept` metadata reflects what the chunk is actually about.
    if write_vectors and embed_one is not None and chunks:
        by_line = {c.line_range: c for c in chunks}
        concepts_by_chunk: dict[str, set[str]] = {}
        for claim in claims:
            if claim.subject_concept:
                for line_range, chunk in by_line.items():
                    if _overlaps(claim.line_range, chunk):
                        concepts_by_chunk.setdefault(line_range, set()).add(claim.subject_concept)

        payload = []
        for chunk in chunks:
            payload.append(
                {
                    "key": f"{record.filename}#{chunk.line_range}",
                    "data": embed_one(chunk.text),
                    "metadata": {
                        "source_file": record.filename,
                        "doc_type": record.doc_type.value,
                        "doc_version": record.doc_version,
                        "concept": sorted(concepts_by_chunk.get(chunk.line_range, [])) or ["_none"],
                        "snippet": chunk.text[:1000],
                        "line_range": chunk.line_range,
                    },
                }
            )
        result.vectors_written = vectors.put_chunks(payload, client=vectors_client)

    # 6. Persist the claims. Conflict detection runs corpus-wide afterwards,
    #    because two documents disagreeing is the case that matters and neither
    #    of them can see the other.
    #
    #    Retract first, so what lands is what this document says rather than the
    #    union of every previous reading of it. Content-addressed IRIs make an
    #    unchanged re-ingest a no-op on their own, but they do not cover a change
    #    to *how the document is read*: `snippet` is not in the hash, so the same
    #    claim would keep its old snippet alongside the new one. See
    #    `facts.retract_document`.
    result.claims_retracted = facts.retract_document(store, record.filename)
    facts.insert(store, claims)
    return result


def _overlaps(line_range: str, chunk: Any) -> bool:
    try:
        start, end = (int(part) for part in line_range.split("-"))
    except ValueError:
        return False
    return not (end < chunk.start_line or start > chunk.end_line)


def ingest_corpus(
    documents: list[tuple[DocumentRecord, str]],
    store: pyoxigraph.Store,
    *,
    index: ResolverIndex | None,
    embed_one: Callable[[str], list[float]] | None = None,
    extract: Callable[..., extraction.Extraction] | None = None,
    run_id: str | None = None,
    gaps_table: Any = None,
    registry_table: Any = None,
    vectors_client: Any = None,
    write_vectors: bool = True,
) -> IngestResult:
    """Ingest every document, then detect contradictions across all of them.

    Step 5 runs once at the end rather than per document, because the
    disagreement that matters is between two documents and neither one can see
    the other while it is being read.
    """
    total = IngestResult()

    for record, raw in documents:
        total.merge(
            ingest_document(
                record,
                raw,
                store,
                index=index,
                embed_one=embed_one,
                extract=extract,
                run_id=run_id,
                gaps_table=gaps_table,
                registry_table=registry_table,
                vectors_client=vectors_client,
                write_vectors=write_vectors,
            )
        )

    # 5. Detect contradictions, deterministically and with no model.
    rows = facts.read_claims(store)
    claim_likes = [
        conflicts.ClaimLike(
            claim_id=str(row["claim"]),
            subject=str(row["subject"] or f"held:{row['subject_surface']}"),
            predicate=str(row["predicate"]),
            value=str(row["value"]),
            unit=row.get("unit"),
            source_file=str(row["source_file"]),
            doc_version=str(row["doc_version"]),
            line_range=str(row["line_range"]),
            snippet=str(row["snippet"]),
            held=str(row["status"]).endswith("held"),
        )
        for row in rows
    ]

    found = conflicts.detect(claim_likes)
    for conflict in found:
        facts.flag_conflict(store, conflict.claim_ids)
    total.conflicts_found = len(found)

    return total
