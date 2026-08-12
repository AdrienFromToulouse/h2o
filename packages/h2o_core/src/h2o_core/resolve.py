"""The resolution cascade, and the only place a gap record is written.

ADR-002 step 4, in four stages: normalise, exact match, embedding shortlist,
abstain. Every resolution records which stage matched and at what score, so any
concept link can be explained after the fact rather than taken on trust.

**Where the gap write lives, and why it lives here.** A miss is recorded as a
side effect of resolution failing, by deterministic code, in this module. It is
not a tool the agent can call. ADR-004: "recording a miss is a side effect of
resolution failing, which is not a decision the model gets to make", and a
`report_gap` tool would be one the model could be prompted into misusing.

Three callers, one function: ingestion's step 4, the agent's resolve_concept
tool, and the telemetry mapper. Each passes different evidence; none of them
gets to decide whether the miss is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from h2o_core import config, resolver
from h2o_core.normalize import normalise
from h2o_core.resolver import Candidate

__all__ = ["Resolution", "Stage", "resolve", "resolve_instance"]


class Stage(StrEnum):
    """Which cascade stage produced the answer.

    Stored on the claim as h2o:resolvedBy, so "why is this claim attached to
    this concept" is answerable from the graph rather than from a log that has
    since rotated.
    """

    exact = "exact"
    embedding = "embedding"
    abstain = "abstain"


@dataclass
class Resolution:
    surface_form: str
    normalised: str
    concept_id: str | None
    stage: Stage
    score: float = 0.0
    label_kind: str | None = None
    #: Populated even on abstention -- this is what becomes the gap entry's
    #: candidate attachment points, and it is why "gas bottle" reaches a curator
    #: with five concepts to choose between rather than as a bare miss.
    #:
    #: It is a set, not a ranking, and the order is not trustworthy. This comment
    #: used to claim "CO2 Cylinder (0.81)"; measured against the deployed index it
    #: is 0.28, behind `Single-Use Bottles Avoided` at 0.348, which shares the word
    #: "bottles" and is a sustainability metric. `retrieval._worth_reporting`
    #: records why at length. The console renders these as peers accordingly.
    shortlist: list[Candidate] = field(default_factory=list)
    #: Set when two concepts in one scheme claim the same normalised label. The
    #: integrity gate should make this unreachable; if it happens the cascade
    #: abstains rather than picking, because picking is the silent failure.
    collision: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.concept_id is not None


def resolve(
    surface_form: str,
    *,
    index: resolver.ResolverIndex | None = None,
    embed: Any = None,
) -> Resolution:
    """Resolve one surface form against the published vocabulary.

    Pure: it reads the index and returns a verdict. Recording the miss is the
    caller's separate, explicit step, so this can be used for a curator's search
    box -- where a miss is browsing, not a resolution failure -- without
    polluting the queue.
    """
    normalised = normalise(surface_form)
    blank = Resolution(
        surface_form=surface_form, normalised=normalised, concept_id=None, stage=Stage.abstain
    )

    if not normalised or index is None:
        return blank

    hits = index.exact(normalised)
    if len(hits) == 1:
        concept_id = hits[0]
        return Resolution(
            surface_form=surface_form,
            normalised=normalised,
            concept_id=concept_id,
            stage=Stage.exact,
            score=1.0,
            label_kind=str(index.kinds.get((concept_id, normalised), "pref")),
        )
    if len(hits) > 1:
        blank.collision = hits
        return blank

    if embed is None or not index.vectors:
        return blank

    shortlist = index.nearest(embed(normalised), k=config.SHORTLIST_SIZE)
    blank.shortlist = shortlist
    if not shortlist:
        return blank

    top = shortlist[0]
    blank.score = top.score

    clears = top.score >= config.RESOLVE_THRESHOLD
    # A term equally close to two concepts is a duplicate-label signal, not a
    # resolution. Without this guard the cascade would confidently attach a
    # claim to whichever of two near-identical concepts sorted first.
    separated = len(shortlist) < 2 or (top.score - shortlist[1].score) >= config.RESOLVE_MARGIN

    if clears and separated:
        return Resolution(
            surface_form=surface_form,
            normalised=normalised,
            concept_id=top.concept_id,
            stage=Stage.embedding,
            score=top.score,
            shortlist=shortlist,
        )
    return blank


def resolve_instance(identifier: str, known: set[str]) -> str | None:
    """Resolve a machine serial or site id. Exact match only.

    Deliberately takes no embedder and no index, so ADR-002's rule is
    unrepresentable-otherwise rather than merely remembered: "a wrong concept
    link mislabels one claim, whereas a wrong instance link tells the wrong
    customer their filter is due."
    """
    return identifier if identifier in known else None
