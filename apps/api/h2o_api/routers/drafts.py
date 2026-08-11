"""Editing and publishing a concept: the review card's write side.

Three routes, one shape of decision each. `impact` answers "what would this do"
and writes nothing. `publish` runs the gate and the transaction and either lands
whole or refuses with a sentence. `candidates` answers "what may I pick here",
and answers it in a way that makes an illegal choice unavailable rather than
rejected later.
"""

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from h2o_core import graph, impact, publish, vocabulary
from h2o_core.impact import ConceptDraft
from pydantic import BaseModel

from h2o_api import dispatch, ingest

router = APIRouter(prefix="/vocabulary/concepts", tags=["curation"])


class PublishRequest(BaseModel):
    draft: ConceptDraft
    author: str = "console"


class PublishAccepted(BaseModel):
    run_id: str
    publish_id: str
    concept_id: str
    version: int
    watermark: str
    closes: list[str]
    status: str = "running"


@router.post("/{concept_id}/impact")
def preview_impact(concept_id: str, draft: ConceptDraft) -> dict[str, Any]:
    """What publishing this draft would do, before anything is saved.

    The draft arrives in the body rather than being read from the graph, so the
    sentence updates as the expert types. ADR-005 §4 calls this the feature that
    makes the console more than a form.
    """
    if draft.concept_id != concept_id:
        raise HTTPException(400, detail="The draft is for a different term than the one named.")
    return impact.preview(graph.cached().store, draft).as_dict()


@router.get("/{concept_id}/candidates")
def candidates(
    concept_id: str,
    relation: Annotated[str, Query(pattern="^(parent|related)$")] = "parent",
) -> list[dict[str, str]]:
    """Terms that may legally be picked for this relation.

    Excludes the concept itself **and every transitive descendant**, so a
    `broader` cycle is *unpickable in the interface* rather than caught at the
    gate. A validation error explaining that you have created a cycle is a worse
    experience than never being offered the option.
    """
    store_ = graph.cached().store
    tree = vocabulary.scheme_tree(store_, include_machine=False)
    forbidden = {concept_id} | (_descendants(store_, concept_id) if relation == "parent" else set())

    found = []
    for scheme_id in tree.top_concepts:
        for reference in _walk(store_, scheme_id):
            if reference["concept_id"] not in forbidden:
                found.append(reference)
    return sorted(found, key=lambda r: r["pref_label"])


def _walk(store_: Any, scheme_id: str) -> list[dict[str, str]]:
    detail = vocabulary.scheme_tree(store_, include_machine=True)
    return [
        {"concept_id": c.concept_id, "pref_label": c.pref_label, "scheme_id": scheme_id}
        for c in detail.top_concepts.get(scheme_id, [])
    ]


def _descendants(store_: Any, concept_id: str) -> set[str]:
    """Everything beneath this concept, so it cannot become its own ancestor."""
    found: set[str] = set()
    frontier = [concept_id]
    while frontier:
        current = frontier.pop()
        detail = vocabulary.concept(store_, current)
        for child in getattr(detail, "children", None) or []:
            if child.concept_id not in found:
                found.add(child.concept_id)
                frontier.append(child.concept_id)
    return found


@router.post("/{concept_id}/publish", response_model=PublishAccepted, status_code=202)
def publish_concept(
    concept_id: str,
    body: PublishRequest,
    background: BackgroundTasks,
) -> PublishAccepted:
    """Run the gate, then the transaction, then start the consequences.

    A blocking finding is a **422 carrying the sentences and nothing else** --
    ADR-006 renders `sh:resultMessage` verbatim, so a code or a query here would
    reach a domain expert. A 409 means somebody else published first and the
    draft was replayed as far as it could be.
    """
    if body.draft.concept_id != concept_id:
        raise HTTPException(400, detail="The draft is for a different term than the one named.")

    try:
        result = publish.publish(body.draft, author=body.author)
    except publish.IntegrityError as refused:
        raise HTTPException(
            422,
            detail={
                "message": "This change cannot be published yet.",
                "findings": [
                    {"concept_id": f.concept_id, "message": f.message} for f in refused.findings
                ],
            },
        ) from refused
    except graph.ConcurrentPublishError as clash:
        raise HTTPException(
            409, detail="Somebody else published while you were editing. Reload and try again."
        ) from clash

    # The publish has landed. Its consequences are a separate, retryable run,
    # which is what keeps a failed re-index from implying a failed publish.
    graph.forget_cached()
    event = {
        "run_id": result.publish_id,
        "concept_id": result.concept_id,
        "version": result.version,
        "surfaces": result.surfaces,
        "closes": result.closes,
        "started_at": ingest.now(),
    }
    dispatch.start_fanout(event, background=background)

    return PublishAccepted(
        run_id=result.publish_id,
        publish_id=result.publish_id,
        concept_id=result.concept_id,
        version=result.version,
        watermark=result.watermark,
        closes=result.closes,
    )
