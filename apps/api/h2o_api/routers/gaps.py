"""The vocabulary gap queue.

ADR-004's governing distinction shapes this whole router: a gap entry is a
**report with a suggested attachment point**, not a drafted concept. So nothing
here writes a label, a definition or a parent. The queue counts, ranks and
points; a curator decides.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from h2o_core import gaps
from h2o_core.gaps import GapEntry, GapStatus
from pydantic import BaseModel, Field

router = APIRouter(prefix="/gaps", tags=["gaps"])


class Dismissal(BaseModel):
    reason: str = Field(min_length=1)


class Target(BaseModel):
    """Where actioning this gap sends a curator."""

    gap_id: str
    gap_type: str
    surface_form: str
    question: str
    #: The concept whose review card to open, when the queue has a suggestion
    #: worth opening. None means the curator picks, or creates.
    concept_id: str | None = None
    pref_label: str | None = None
    score: float | None = None
    suggested_scheme: str | None = None
    #: The label a curator would be adding, pre-filled from the evidence rather
    #: than invented: it is the spelling the documents actually used.
    suggested_label: str


@router.get("")
def list_gaps(
    status: Annotated[GapStatus | None, Query()] = GapStatus.open,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[GapEntry]:
    """Ordered by occurrence count, descending.

    ADR-004 rules out a learned ranking: at this scale, ordering by how often
    something was actually said is both sufficient and explainable, and a
    curator can see why an entry is at the top.
    """
    return gaps.list_gaps(status, limit)


@router.get("/{gap_id}")
def read_gap(gap_id: str) -> GapEntry:
    entry = gaps.read_gap(gap_id)
    if entry is None:
        raise HTTPException(404, detail="There is no gap by that name in the queue.")
    return entry


@router.get("/{gap_id}/target", response_model=Target)
def target(gap_id: str) -> Target:
    """Where to go to action this gap. **A read, deliberately.**

    ADR-004 §6 says actioning a gap writes nothing, and making this a GET is how
    that is enforced rather than remembered: the endpoint cannot accidentally
    acquire a write later, because a GET that mutated would be the thing a
    reviewer notices. The entry's status moves to `actioned` from fan-out step
    2 and nowhere else -- the queue closes because a publish closed it, not
    because somebody clicked on it.
    """
    entry = gaps.read_gap(gap_id)
    if entry is None:
        raise HTTPException(404, detail="There is no gap by that name in the queue.")

    top: dict[str, Any] = entry.suggestions[0] if entry.suggestions else {}
    return Target(
        gap_id=entry.gap_id,
        gap_type=entry.gap_type.value,
        surface_form=entry.surface_form,
        question=entry.question,
        concept_id=top.get("concept_id"),
        pref_label=top.get("pref_label"),
        score=top.get("score"),
        suggested_scheme=entry.suggested_scheme,
        suggested_label=entry.surface_form,
    )


@router.post("/{gap_id}/dismiss", response_model=GapEntry)
def dismiss(gap_id: str, body: Dismissal) -> GapEntry:
    """Suppress a surface form, recording the count it was suppressed at.

    Not permanent amnesty: counts keep accruing while dismissed, and ADR-004's
    100x rule resurfaces the entry if the term turns out to be far more common
    than it looked. A reason is required because the next curator reads it.
    """
    entry = gaps.dismiss(gap_id, body.reason)
    if entry is None:
        raise HTTPException(404, detail="There is no gap by that name in the queue.")
    return entry
