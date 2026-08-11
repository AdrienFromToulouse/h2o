"""DynamoDB access for everything the RDF graph deliberately does not hold.

Claims are not here. Extracted facts, their conflict flags and their held status
live in ``h2o:graph/facts`` (ADR-005), because they are evidence about the
vocabulary and belong beside it. What lives here is operational churn: the
curation backlog, the audit trail, the document registry, and run records.

Four tables, one key convention::

    gaps      h2o-{env}-vocabulary-gaps
              gap_id = the merge key (see gaps.gap_key), NO sort key, so
              ingestion, chat and telemetry land on one item with three counts
              rather than three items (ADR-004). Evidence hangs off the same
              item as a capped list rather than separate rows, because a gap is
              always read whole.

    audit     h2o-{env}-curation-audit
              concept_id / published_at

    registry  h2o-{env}-document-registry
              filename / doc_version

    runs      h2o-{env}-runs
              run_id / sk  where sk is "RUN" for the envelope and
              "STEP#{n}#{name}" per fan-out step, so a run and its steps are one
              Query rather than a join.

Every AWS-touching function takes an optional client so tests inject a fake, and
falls back to a lazily-created module singleton. There are no ports and no ABCs;
the keyword argument is the seam.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from h2o_core import config

__all__ = [
    "audit_table",
    "gaps_table",
    "latest_run",
    "list_runs",
    "read_run",
    "read_steps",
    "registry_table",
    "runs_table",
    "to_dynamo",
    "from_dynamo",
    "write_run",
]

_tables: dict[str, Any] = {}


def _table(name: str) -> Any:
    if name not in _tables:
        import boto3

        _tables[name] = boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(name)
    return _tables[name]


def gaps_table() -> Any:
    return _table(config.GAPS_TABLE)


def audit_table() -> Any:
    return _table(config.AUDIT_TABLE)


def registry_table() -> Any:
    return _table(config.REGISTRY_TABLE)


def runs_table() -> Any:
    return _table(config.RUNS_TABLE)


def forget_tables() -> None:
    """Drop the client cache. Tests use this; nothing in production should."""
    _tables.clear()


def to_dynamo(value: Any) -> Any:
    """Floats become Decimals, because DynamoDB has no float type.

    Confidence scores and similarity scores are the values that hit this, and a
    raw float raises rather than rounding, so the failure is loud.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value: Any) -> Any:
    """Decimals become ints or floats, so callers never see the storage type."""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


# ------------------------------------------------------------------ run records
#
# ADR-005: the console reuses one polling hook rather than inventing a second,
# so ingest, publish and telemetry runs share one table, one envelope and one
# endpoint. `kind` distinguishes them and the by-kind index orders them.

RUN_SK = "RUN"


def write_run(item: dict[str, Any], *, table_resource: Any = None) -> None:
    target = table_resource or runs_table()
    target.put_item(Item=to_dynamo({**item, "sk": RUN_SK}))


def write_step(
    run_id: str, index: int, step: dict[str, Any], *, table_resource: Any = None
) -> None:
    """One row per fan-out step, under the run's own partition.

    Separate rows rather than a list on the envelope: Step Functions writes
    these from four different invocations, and a list would need a read-modify-
    write that two concurrent steps could interleave.
    """
    target = table_resource or runs_table()
    target.put_item(
        Item=to_dynamo({**step, "run_id": run_id, "sk": f"STEP#{index:02d}#{step['name']}"})
    )


def read_steps(run_id: str, *, table_resource: Any = None) -> list[dict[str, Any]]:
    """The step rows of a run, whether or not its envelope exists yet.

    `read_run` returns None without an envelope, which is right for a reader --
    a run with no envelope is not a run. But the step that *writes* the envelope
    needs to total up the steps before it, and at that moment there is no
    envelope by definition. Asking for the steps directly is the difference
    between a summary and an empty one.
    """
    target = table_resource or runs_table()
    items = target.query(KeyConditionExpression=Key("run_id").eq(run_id)).get("Items", [])
    steps = [from_dynamo(item) for item in items if item.get("sk") != RUN_SK]
    return sorted(steps, key=lambda s: str(s.get("sk", "")))


def read_run(run_id: str, *, table_resource: Any = None) -> dict[str, Any] | None:
    """A run and its steps, in one Query."""
    target = table_resource or runs_table()
    items = target.query(KeyConditionExpression=Key("run_id").eq(run_id)).get("Items", [])
    if not items:
        return None

    envelope: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    for item in items:
        clean = from_dynamo(item)
        if clean.get("sk") == RUN_SK:
            envelope = clean
        else:
            steps.append(clean)

    if not envelope:
        return None
    envelope["steps"] = sorted(steps, key=lambda s: str(s.get("sk", "")))
    return envelope


def list_runs(
    kind: str | None = None, limit: int = 20, *, table_resource: Any = None
) -> list[dict[str, Any]]:
    """Newest first, off the by-kind index.

    Without the index this is a table scan, and the console polls it.
    """
    target = table_resource or runs_table()
    kinds = [kind] if kind else ["ingest", "publish", "telemetry"]
    found: list[dict[str, Any]] = []
    for one in kinds:
        response = target.query(
            IndexName="by-kind",
            KeyConditionExpression=Key("kind").eq(one),
            ScanIndexForward=False,
            Limit=limit,
        )
        found.extend(from_dynamo(item) for item in response.get("Items", []))
    return sorted(found, key=lambda r: str(r.get("started_at", "")), reverse=True)[:limit]


def latest_run(kind: str, *, table_resource: Any = None) -> dict[str, Any] | None:
    """The most recent run of one kind, or None.

    None rather than an error: nothing having run yet is a normal state, and the
    console's polling hook uses this to decide whether to resume after a reload.
    """
    runs = list_runs(kind, limit=1, table_resource=table_resource)
    if not runs:
        return None
    return read_run(str(runs[0]["run_id"]), table_resource=table_resource)
