"""One run surface for all three kinds of run.

ADR-005 §5 says the console "reuses the existing ingest-run polling hook rather
than inventing a second one", taken literally: one table, one envelope, one
endpoint, and `kind` to tell them apart. An ingest run and a publish fan-out run
differ in their step names and in nothing else a poller can see.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from h2o_core import store

router = APIRouter(prefix="/runs", tags=["runs"])

RunKind = Literal["ingest", "publish", "telemetry"]


@router.get("")
def list_runs(
    kind: Annotated[RunKind | None, Query(description="Restrict to one kind of run.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    return store.list_runs(kind, limit)


@router.get("/latest")
def latest_run(kind: Annotated[RunKind, Query()] = "ingest") -> dict[str, Any] | None:
    """The most recent run of one kind, or null.

    **Null with a 200, never a 404.** Nothing having run yet is a normal state,
    not a missing resource, and the console's polling hook calls this on every
    reload to decide whether to resume watching an in-flight run. A 404 here
    would make "no run has ever happened" indistinguishable from an error on the
    one code path whose job is to recover gracefully from a page reload.
    """
    return store.latest_run(kind)


@router.get("/{run_id}")
def read_run(run_id: str) -> dict[str, Any]:
    run = store.read_run(run_id)
    if run is None:
        raise HTTPException(404, detail="There is no run by that name.")
    return run
