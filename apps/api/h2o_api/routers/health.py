"""Liveness, plus the two numbers that say whether answers can be trusted."""

from fastapi import APIRouter
from h2o_core import config as core
from h2o_core import graph
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str
    env: str
    region: str
    graph_backend: str
    quads: int
    #: ADR-005 exposes the resolver index's staleness as a watermark rather than
    #: hiding it, so a stale answer is diagnosable by comparing two strings.
    #: Reporting the dataset's own digest here is what there is to compare
    #: against once the index exists.
    dataset_digest: str | None = None


@router.get("/health", response_model=Health)
def health() -> Health:
    snapshot = graph.cached()
    return Health(
        status="ok",
        env=core.H2O_ENV,
        region=core.AWS_REGION,
        graph_backend=core.GRAPH_BACKEND,
        quads=snapshot.quad_count,
        dataset_digest=graph.digest(graph.dump(snapshot.store))[:16]
        if snapshot.quad_count
        else None,
    )
