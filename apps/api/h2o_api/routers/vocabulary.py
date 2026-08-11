"""Reading the published vocabulary.

`concept_id` is always a slug. The IRI is minted and consumed inside h2o_core
and reaches a response only inside TechnicalDetail, because ADR-006 makes "no
IRI in the default interface" a hard constraint rather than a preference, and
the cheapest way to keep it is for the API not to hand one out.
"""

from fastapi import APIRouter, HTTPException, Query
from h2o_core import graph, vocabulary
from h2o_core.models import ConceptDetail, ConceptRef, SchemeTree

router = APIRouter(prefix="/vocabulary", tags=["vocabulary"])


@router.get("", response_model=SchemeTree)
def read_tree(
    include_machine: bool = Query(
        False,
        description=(
            "Include the machine-side scheme. Off by default: it holds firmware's "
            "names for things, which ADR-006 keeps out of the curation interface."
        ),
    ),
) -> SchemeTree:
    return vocabulary.scheme_tree(graph.cached().store, include_machine=include_machine)


@router.get("/concepts/{concept_id}", response_model=ConceptDetail)
def read_concept(concept_id: str) -> ConceptDetail:
    detail = vocabulary.concept(graph.cached().store, concept_id)
    if detail is None:
        # Plain language, no identifier: this message can reach a curator.
        raise HTTPException(404, detail="There is no term by that name in the vocabulary.")
    return detail


@router.get("/schemes/{scheme_id}/concepts", response_model=list[ConceptRef])
def browse_scheme(scheme_id: str) -> list[ConceptRef]:
    """The top terms of one vocabulary. Browsing beneath a term reads its children."""
    tree = vocabulary.scheme_tree(graph.cached().store, include_machine=True)
    if scheme_id not in tree.top_concepts:
        raise HTTPException(404, detail="There is no vocabulary by that name.")
    return tree.top_concepts[scheme_id]
