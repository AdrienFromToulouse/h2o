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

from h2o_core import fanout as core
from h2o_core import store as core_store

from h2o_api import config, ingest

#: The fan-out, in order. Names match 30-orchestration.yaml's Task states and
#: h2o_core.fanout.STEP_NAMES; a test parses the state machine to keep all three
#: in step, because they drift silently and the symptom is a publish whose
#: consequences half-ran.
STEPS: tuple[tuple[str, Any], ...] = (
    ("rebuild_index", core.rebuild_resolver_index),
    ("reresolve_gaps", core.reresolve_backlog),
    ("reindex_documents", core.reindex_affected_documents),
    ("record_run", core.finalise_run),
)

BY_NAME = dict(STEPS)
STEP_NAMES = tuple(name for name, _ in STEPS)


def _lambda() -> Any:
    import boto3

    return boto3.client("lambda", region_name=config.AWS_REGION)


def _events() -> Any:
    import boto3

    return boto3.client("events", region_name=config.AWS_REGION)


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


def run_step(name: str, event: dict[str, Any]) -> dict[str, Any]:
    """Run one fan-out step by name.

    The single entry point for both arms. In Lambda, Step Functions calls this
    once per Task; locally, `start_fanout` walks the same tuple in the same
    order. Neither arm chooses *what* a step does.
    """
    step = BY_NAME.get(name)
    if step is None:
        raise ValueError(f"unknown fan-out step {name!r}; expected one of {list(BY_NAME)}")

    outcome = dict(step(event) or {})

    # The step row is written here, so both arms record identically. Writing it
    # in the local walker instead meant a Step Functions run left no steps at
    # all, and /runs showed a publish that had "succeeded" with nothing in it --
    # the exact shape of drift a dispatcher is supposed to make impossible.
    if name != STEP_NAMES[-1]:
        core_store.write_step(
            str(event.get("run_id", "")),
            STEP_NAMES.index(name) + 1,
            {"name": name, "counts": outcome},
        )
    return outcome


def start_fanout(event: dict[str, Any], *, background: Any = None, client: Any = None) -> None:
    """Run the publish's consequences, however this deployment can.

    In Lambda this emits `VocabularyPublished` and returns: EventBridge starts
    the state machine, which calls back one Task at a time. ADR-005 makes the
    event the trigger, which is why the API's role carries `events:PutEvents`
    and deliberately not `states:StartExecution` -- granting both would leave
    two ways in and no single answer to "what started this run".
    """
    if config.IS_LAMBDA:
        (client or _events()).put_events(
            Entries=[
                {
                    "EventBusName": config.EVENT_BUS_NAME,
                    "Source": "h2o.vocabulary",
                    "DetailType": "VocabularyPublished",
                    # The state machine's InputPath is $.detail, so these fields
                    # are the execution's own $.run_id and $.event.
                    "Detail": json.dumps({"run_id": event.get("run_id", ""), "event": event}),
                }
            ]
        )
        return

    if background is None:
        raise RuntimeError("the fan-out needs either EventBridge or a BackgroundTasks to run in")
    background.add_task(_run_all, event)


def _run_all(event: dict[str, Any]) -> None:
    """The local arm: the same steps, the same order, one process.

    Each step's counts accumulate onto the event so `record_run` sees the whole
    run, which is what the state machine's ResultPath does for the AWS arm.
    """
    counts: dict[str, Any] = {}
    for name, _ in STEPS[:-1]:
        outcome = run_step(name, {**event, "counts": counts})
        counts |= outcome
        # Step 2 discovers which documents step 3 has to re-index.
        if documents := outcome.get("documents"):
            event = {**event, "documents": documents}

    run_step("record_run", {**event, "counts": counts})
