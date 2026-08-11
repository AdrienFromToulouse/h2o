"""The one place in h2o that branches on where it is running.

Everything asynchronous goes through here: starting an ingest run, and (from
M6) running the publish fan-out. The branch decides **who calls** the work,
never **what the work does**. Both arms call the identical functions with the
identical arguments; in Lambda the call travels through Lambda or EventBridge
first, and locally it travels through BackgroundTasks.

That is the whole discipline. `h2o_core` has no idea any of this exists: the
requirement it must meet is that the functions dispatched to here take no
client, no context and no environment argument. The moment one of them needs to
know it is inside Step Functions, this stops being a dispatcher and becomes a
second code path -- and a second code path is a thing that gets tested in one
configuration and shipped in the other.

`apps/api/tests/test_no_dual_path.py` greps the tree to keep `IS_LAMBDA` here.
"""

from __future__ import annotations

import json
from typing import Any

from h2o_api import config, ingest


def _lambda() -> Any:
    import boto3

    return boto3.client("lambda", region_name=config.AWS_REGION)


def start_ingest(
    run_id: str,
    only: list[str] | None,
    *,
    background: Any = None,
    client: Any = None,
) -> None:
    """Run the ingest worker out of band, however this deployment can.

    The HTTP request has API Gateway's 29 seconds and ingesting the corpus
    through Nova 2 Lite has not, so the response is a run id either way.
    """
    if config.IS_LAMBDA:
        (client or _lambda()).invoke(
            FunctionName=config.FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(
                {"h2o_action": config.INGEST_ACTION, "run_id": run_id, "only": only}
            ).encode(),
        )
        return

    if background is None:
        # No Lambda to invoke and nowhere to put the work: running it inline
        # would block the request past every sensible timeout, so say so.
        raise RuntimeError("ingestion needs either Lambda or a BackgroundTasks to run in")
    background.add_task(ingest.run, run_id, only=only)
