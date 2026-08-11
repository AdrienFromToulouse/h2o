"""The read path: resolve the question, expand, then search what is left.

**Graph first, vectors second.** The GraphRAG literature's usual shape is to
embed the question, find the nearest chunks, and traverse from whatever they
mention. That order needs entities induced from the corpus, and ADR-001 rejects
inducing them. h2o has the opposite asset: a human-authored SKOS vocabulary that
every chunk was already resolved against, carried on the vector as a filterable
``concept`` key (ADR-002 step 2, deliberately written *after* step 4 so the key
is true). So the vocabulary narrows the search before the search runs, rather
than the search discovering a vocabulary afterwards.

Concretely this is metadata filtering, and the reason it is safe here is that
the filter is not guessed. The usual failure of the pattern is a model deciding
what to filter on and being subtly wrong; here the filter comes from the same
deterministic cascade that ingestion used, which abstains loudly rather than
picking. A question and a document that mention the same thing are filtered to
the same concept because the *same function* said so.

**Abstention is not a dead end.** A phrase that resolves to nothing is a
vocabulary gap with a question as its evidence (ADR-004, ``GapSource.chat``).
The system cannot answer, and says so, and the fact that somebody asked becomes
a curation item. That is the loop the README describes, entered from the read
side.

**Nothing here adjudicates.** Claims that contradict each other come back as a
disagreement carrying both cited values. ADR-002: the system records what is
evidenced, not what is true.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pyoxigraph
from pydantic import BaseModel, Field

from h2o_core import config, facts, gaps, resolve, vectors, vocabulary
from h2o_core.gaps import GapEvidence, GapSource, GapType
from h2o_core.normalize import normalise
from h2o_core.resolve import Stage
from h2o_core.resolver import Candidate, ResolverIndex

__all__ = [
    "Answer",
    "Disagreement",
    "Passage",
    "ResolvedTerm",
    "UnresolvedTerm",
    "candidate_terms",
    "expand",
    "retrieve",
]


class ResolvedTerm(BaseModel):
    """A phrase from the question that the vocabulary recognised."""

    surface_form: str
    concept_id: str
    pref_label: str
    #: Which cascade stage matched, so an answer can say *how* it understood the
    #: question rather than only what it concluded.
    stage: str
    score: float


class UnresolvedTerm(BaseModel):
    """A phrase the vocabulary did not recognise, and what it nearly was."""

    surface_form: str
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    #: The queue entry this question contributed to, or None when the phrase was
    #: too far from anything to be worth a curator's time.
    gap_id: str | None = None


class Passage(BaseModel):
    """One retrieved chunk, with the locator that makes it checkable."""

    source_file: str
    line_range: str
    #: source_file:line_range. Present on every passage, because a passage
    #: without one is a quotation nobody can verify.
    locator: str
    snippet: str
    doc_type: str | None = None
    doc_version: str | None = None
    concepts: list[str] = Field(default_factory=list)
    distance: float | None = None


class Disagreement(BaseModel):
    """Two or more claims about the same thing that do not agree.

    Presented, never resolved. The claims are the whole group, each with its own
    citation, so a reader sees that the corpus disagrees rather than seeing
    whichever value happened to be stored first.
    """

    subject_concept: str
    predicate: str
    claims: list[dict[str, Any]]


class Answer(BaseModel):
    """Everything retrieval found, and everything it could not understand.

    Not prose. Composing an answer from this is the agent's job (ADR-001); what
    this guarantees is that every passage and every claim in it is quotable and
    located, because the verbatim gate already refused anything that was not.
    """

    question: str
    resolved: list[ResolvedTerm] = Field(default_factory=list)
    unresolved: list[UnresolvedTerm] = Field(default_factory=list)
    #: The resolved concepts plus everything one hop away. This is exactly the
    #: filter the vector search ran with, exposed so an answer can explain what
    #: it looked at.
    searched_concepts: list[str] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)

    @property
    def understood(self) -> bool:
        """Whether any part of the question reached the vocabulary at all."""
        return bool(self.resolved)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------- the sweep


def _words(question: str) -> list[str]:
    return question.split()


def candidate_terms(
    question: str,
    *,
    index: ResolverIndex | None,
    embed: Callable[[str], list[float]] | None = None,
) -> list[resolve.Resolution]:
    """Every phrase in the question the cascade had a verdict about.

    Two passes, because they cost different amounts. The first sweeps the
    question left to right, longest phrase first, against the index's exact map
    -- a dictionary lookup, so it is free and every phrase can be tried. A
    matched phrase consumes its words, which is what makes "carbon filter"
    resolve as one term rather than as "carbon" and "filter" separately.

    The second pass takes the words nothing matched and runs the full cascade,
    embedding stage included, over at most ``MAX_CANDIDATE_TERMS`` phrases in
    longest-first order. That bound is real: each phrase is one Titan call, and
    a question has quadratically many phrases.

    Returns the cascade's own verdicts rather than a decision about them, for
    the same reason ``resolve.resolve`` is pure: a curator's search box wants
    the verdict without the queue write.
    """
    if index is None:
        return []

    words = _words(question)
    verdicts: list[resolve.Resolution] = []
    consumed = [False] * len(words)

    # Pass 1: exact only. No embedder is passed, so the cascade stops after its
    # second stage -- including on a collision, which abstains loudly here
    # exactly as it does during ingestion.
    position = 0
    while position < len(words):
        for length in range(min(config.MAX_TERM_WORDS, len(words) - position), 0, -1):
            phrase = " ".join(words[position : position + length])
            verdict = resolve.resolve(phrase, index=index)
            if verdict.stage is Stage.exact:
                verdicts.append(verdict)
                for offset in range(length):
                    consumed[position + offset] = True
                position += length
                break
        else:
            position += 1

    if embed is None:
        return verdicts

    # Pass 2: what is left, longest first, capped. Longest first is both the
    # useful order -- a longer phrase is a more specific term -- and the order
    # that makes the cap drop the least informative candidates.
    candidates: list[tuple[int, int, str]] = []
    for length in range(config.MAX_TERM_WORDS, 0, -1):
        for start in range(len(words) - length + 1):
            span = range(start, start + length)
            if any(consumed[i] for i in span):
                continue
            phrase = " ".join(words[start : start + length])
            if normalise(phrase):
                candidates.append((start, length, phrase))

    seen = 0
    for start, length, phrase in candidates:
        if seen >= config.MAX_CANDIDATE_TERMS:
            break
        if any(consumed[i] for i in range(start, start + length)):
            continue
        seen += 1
        verdict = resolve.resolve(phrase, index=index, embed=embed)
        # A phrase is consumed when the cascade matched it, and also when it
        # came close enough to be worth reporting as a gap. Without the second
        # case "the gas bottle", "gas bottle" and "gas" would each open an
        # entry for one mention of one thing.
        if verdict.matched or _worth_reporting(verdict):
            verdicts.append(verdict)
            for offset in range(length):
                consumed[start + offset] = True

    return verdicts


def _worth_reporting(verdict: resolve.Resolution) -> bool:
    """Whether an abstention is near enough the vocabulary to be a gap.

    A question is mostly function words. Recording every one of them would bury
    the queue that ADR-004 orders by occurrence count, and the count is the only
    signal a curator has.
    """
    if verdict.matched or not verdict.shortlist:
        return False
    return verdict.shortlist[0].score >= config.CHAT_GAP_FLOOR


# ------------------------------------------------------------- the expansion


def expand(
    store: pyoxigraph.Store,
    concept_ids: list[str],
    *,
    depth: int | None = None,
) -> list[str]:
    """The seed concepts plus everything within ``depth`` hops.

    Iterative with a visited set rather than a SPARQL property path, for the
    reason ``concept_children.rq`` already gives: a path cannot report how far
    it travelled, and iteration terminates even if a cycle reaches the graph.
    """
    limit = config.EXPAND_DEPTH if depth is None else depth
    seen = set(concept_ids)
    frontier = list(concept_ids)

    for _ in range(max(limit, 0)):
        nxt: list[str] = []
        for concept_id in frontier:
            for neighbour in vocabulary.neighbours(store, concept_id):
                if neighbour not in seen:
                    seen.add(neighbour)
                    nxt.append(neighbour)
        if not nxt:
            break
        frontier = nxt

    return sorted(seen)


# ----------------------------------------------------------------- the claims


def _merge_claim_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per claim, with its conflicts collected.

    ``facts_for_concept.rq`` returns the conflict flag alongside the value on
    purpose, which means a claim conflicting with two others arrives as two
    rows. Folding them here keeps that guarantee -- the flag never travels
    separately from the value -- without making a caller count rows.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        claim_id = str(row["claim"])
        record = merged.get(claim_id)
        if record is None:
            record = {k: v for k, v in row.items() if k != "conflicts_with"}
            record["conflicts_with"] = []
            merged[claim_id] = record
        if row.get("conflicts_with"):
            record["conflicts_with"].append(str(row["conflicts_with"]))

    for record in merged.values():
        record["conflicts_with"] = sorted(set(record["conflicts_with"]))
    return sorted(
        merged.values(), key=lambda r: (r["predicate"], r["source_file"], r["line_range"])
    )


def _disagreements(claims: list[dict[str, Any]]) -> list[Disagreement]:
    """Group flagged claims into the sets that disagree.

    ``facts.flag_conflict`` writes the relation symmetrically and completely, so
    every member of a group names every other and ``{claim} | conflicts`` is the
    same set whichever member it is computed from. That is what makes grouping a
    dictionary lookup rather than a union-find.
    """
    by_id = {str(claim["claim"]): claim for claim in claims}
    groups: dict[frozenset[str], list[dict[str, Any]]] = {}

    for claim_id, claim in by_id.items():
        if not claim["conflicts_with"]:
            continue
        key = frozenset({claim_id, *claim["conflicts_with"]})
        groups.setdefault(key, [])

    found: list[Disagreement] = []
    for key in groups:
        members = [by_id[claim_id] for claim_id in sorted(key) if claim_id in by_id]
        if len(members) < 2:
            # The counterpart is about a concept this question did not reach.
            # Reporting one side alone would read as a settled value, which is
            # the exact failure the symmetric flag exists to prevent.
            continue
        found.append(
            Disagreement(
                subject_concept=str(members[0].get("subject_concept") or ""),
                predicate=str(members[0]["predicate"]),
                claims=members,
            )
        )
    return sorted(found, key=lambda d: (d.subject_concept, d.predicate))


# ------------------------------------------------------------------ retrieval


def retrieve(
    question: str,
    store: pyoxigraph.Store,
    *,
    index: ResolverIndex | None,
    embed_one: Callable[[str], list[float]] | None = None,
    top_k: int | None = None,
    depth: int | None = None,
    record_gaps: bool = True,
    gaps_table: Any = None,
    vectors_client: Any = None,
    run_id: str | None = None,
) -> Answer:
    """Answer material for one question. Reads the graph; writes only gaps.

    **When nothing resolves, nothing is retrieved.** The obvious fallback --
    search the vectors unfiltered -- is the plain vector search this design
    exists to replace, and it would answer confidently from wording alone about
    a term the vocabulary has never heard of. Returning the unresolved phrases
    instead is the honest result, and it is also the useful one: those phrases
    are now in the queue.
    """
    answer = Answer(question=question)

    verdicts = candidate_terms(question, index=index, embed=embed_one)

    for verdict in verdicts:
        if verdict.matched and verdict.concept_id:
            answer.resolved.append(
                ResolvedTerm(
                    surface_form=verdict.surface_form,
                    concept_id=verdict.concept_id,
                    pref_label=(index.pref_labels.get(verdict.concept_id, verdict.concept_id))
                    if index
                    else verdict.concept_id,
                    stage=verdict.stage.value,
                    score=verdict.score,
                )
            )
            continue

        gap_id = None
        if record_gaps:
            gap_id = _record_chat_miss(question, verdict, gaps_table=gaps_table, run_id=run_id)
        answer.unresolved.append(
            UnresolvedTerm(
                surface_form=verdict.surface_form,
                suggestions=[_as_suggestion(c) for c in verdict.shortlist],
                gap_id=gap_id,
            )
        )

    if not answer.resolved:
        return answer

    seeds = [term.concept_id for term in answer.resolved]
    answer.searched_concepts = expand(store, seeds, depth=depth)

    if embed_one is not None:
        answer.passages = [
            _as_passage(hit)
            for hit in vectors.query(
                embed_one(question),
                concepts=answer.searched_concepts,
                top_k=top_k,
                client=vectors_client,
            )
        ]

    rows: list[dict[str, Any]] = []
    for concept_id in answer.searched_concepts:
        for row in facts.read_claims(store, concept_id=concept_id):
            rows.append({**row, "subject_concept": concept_id})

    answer.claims = _merge_claim_rows(rows)
    answer.disagreements = _disagreements(answer.claims)
    return answer


def _as_suggestion(candidate: Candidate) -> dict[str, Any]:
    return {
        "concept_id": candidate.concept_id,
        "pref_label": candidate.pref_label,
        "score": candidate.score,
    }


def _as_passage(hit: dict[str, Any]) -> Passage:
    source_file = str(hit.get("source_file") or "")
    line_range = str(hit.get("line_range") or "")
    concepts = hit.get("concept") or []
    return Passage(
        source_file=source_file,
        line_range=line_range,
        locator=f"{source_file}:{line_range}",
        snippet=str(hit.get("snippet") or ""),
        doc_type=hit.get("doc_type"),
        doc_version=hit.get("doc_version"),
        concepts=[c for c in concepts if c != "_none"],
        distance=hit.get("distance"),
    )


def _record_chat_miss(
    question: str,
    verdict: resolve.Resolution,
    *,
    gaps_table: Any = None,
    run_id: str | None = None,
) -> str | None:
    """Put one unrecognised phrase in the queue, with the question as evidence.

    The locator is derived from the question rather than from the turn, so the
    same question asked twice is one piece of evidence and two occurrences.
    Evidence answers "what did somebody actually say"; the count answers "how
    often", and conflating them would let one person asking repeatedly look like
    a term in wide use.
    """
    if not _worth_reporting(verdict):
        return None

    digest = hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:12]  # noqa: S324 - a dedup key
    return gaps.record_miss(
        verdict.surface_form,
        source=GapSource.chat,
        evidence=GapEvidence(
            source=GapSource.chat,
            text=question.strip()[: config.MAX_EVIDENCE_TEXT],
            locator=f"chat:{digest}",
            occurred_at=_now(),
        ),
        gap_type=GapType.add_alt_label,
        suggestions=verdict.shortlist,
        run_id=run_id,
        table_resource=gaps_table,
    )
