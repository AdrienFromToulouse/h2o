"""The resolution cascade, and the only place a gap record is written.

ADR-002 step 4, in four stages: normalise, exact match, embedding shortlist,
abstain. Every resolution records which stage matched and at what score, so any
concept link can be explained after the fact rather than taken on trust.

At query time the caller may also supply an alias map from `sanitise`, which
swaps the key the dictionary is asked for without touching the surface form.
Ingestion does not, and its cascade is unchanged: the stage this records lands
in the facts graph as `h2o:resolvedBy`, and a claim saying it matched exactly
when a model corrected the spelling first would be false in the graph.

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
    #: The form actually looked up. Equal to `normalised` unless a sanitiser
    #: alias applied -- a typo corrected, a term translated. Kept separate rather
    #: than overwriting `normalised` because the two answer different questions:
    #: `normalised` is what the person typed, folded, and it is what the chip
    #: shows; `lookup` is what the dictionary was asked for.
    lookup: str = ""
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

    @property
    def aliased(self) -> bool:
        """Whether a sanitiser alias changed what was looked up."""
        return bool(self.lookup) and self.lookup != self.normalised


def resolve(
    surface_form: str,
    *,
    index: resolver.ResolverIndex | None = None,
    embed: Any = None,
    aliases: dict[str, str] | None = None,
) -> Resolution:
    """Resolve one surface form against the published vocabulary.

    Pure: it reads the index and returns a verdict. Recording the miss is the
    caller's separate, explicit step, so this can be used for a curator's search
    box -- where a miss is browsing, not a resolution failure -- without
    polluting the queue.

    `aliases` swaps the *lookup key* and nothing else (see `sanitise`). The
    surface form is never rewritten, because it is load-bearing three ways
    downstream: it is the left-hand side of the chip, so "installtion ->
    Installation" only means anything if the left side is what was typed; it is
    what a gap entry quotes back to a curator; and it is what the queue merges
    on. Only ingestion's caller passes None here -- a document's misspelling is a
    curator's `skos:hiddenLabel` decision, not something the pipeline patches
    (ADR-002, "Reject, do not repair").
    """
    normalised = normalise(surface_form)
    lookup = (aliases or {}).get(normalised, normalised)
    blank = Resolution(
        surface_form=surface_form,
        normalised=normalised,
        concept_id=None,
        stage=Stage.abstain,
        lookup=lookup,
    )

    if not normalised or index is None:
        return blank

    hits = index.exact(lookup)
    if len(hits) == 1:
        concept_id = hits[0]
        return Resolution(
            surface_form=surface_form,
            normalised=normalised,
            concept_id=concept_id,
            stage=Stage.exact,
            score=1.0,
            # "alias" rather than the label's own kind when the sanitiser was
            # what made the lookup succeed. The chip is identical either way --
            # a correction is silent by design -- but a log and a test can still
            # tell a typo apart from a label the vocabulary really holds.
            label_kind="alias"
            if lookup != normalised
            else str(index.kinds.get((concept_id, lookup), "pref")),
            lookup=lookup,
        )
    if len(hits) > 1:
        blank.collision = hits
        return blank

    if embed is None or not index.vectors:
        return blank

    # The lookup form, not the typed one. Embedding "bouteille de gaz" against
    # an English index returns a shortlist about nothing, and that shortlist is
    # what a curator would be shown as the suggested attachment point.
    shortlist = index.nearest(embed(lookup), k=config.SHORTLIST_SIZE)
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
            lookup=lookup,
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
