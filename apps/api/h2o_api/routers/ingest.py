"""Starting an ingest run.

The route mints a run id and hands it straight back. Where the work actually
runs -- a second Lambda invocation, or this process once the response is out --
is `h2o_api.dispatch`'s decision and deliberately not this router's.
"""

from fastapi import APIRouter, BackgroundTasks
from h2o_core import store
from pydantic import BaseModel, Field

from h2o_api import dispatch, ingest

router = APIRouter(tags=["ingest"])


class IngestRequest(BaseModel):
    only: list[str] | None = Field(
        None,
        description=(
            "Filenames to ingest, from the registry. Omit for the whole corpus. "
            "A name the registry does not carry fails the run rather than being "
            "skipped, because silently ingesting nothing looks like success."
        ),
    )


class RunAccepted(BaseModel):
    run_id: str
    status: str


@router.post("/ingest", status_code=202, response_model=RunAccepted)
def start_ingest(
    background: BackgroundTasks,
    body: IngestRequest | None = None,
) -> RunAccepted:
    only = body.only if body else None
    run_id = ingest.new_run_id()

    # Written before the work is dispatched, not after. The console polls the id
    # it was just handed, and a `queued` row is a truthful answer to that poll
    # where a 404 -- in the gap between returning the id and the worker's first
    # write -- would look like a run that never existed.
    store.write_run(
        {"run_id": run_id, "kind": "ingest", "status": "queued", "started_at": ingest.now()}
    )
    dispatch.start_ingest(run_id, only, background=background)
    return RunAccepted(run_id=run_id, status="queued")
