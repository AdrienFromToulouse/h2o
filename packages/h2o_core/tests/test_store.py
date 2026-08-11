"""Run records, and the two DynamoDB refusals the gap queue is shaped around.

ADR-005 asks the console to poll one endpoint for three kinds of run, which only
works if a run and its steps come back as one envelope in one Query. That is
what `read_run` promises and what most of this file asserts.

The rest asserts the fake. That is deliberate: the previous gaps fake recognised
one caller's update expression instead of evaluating it, and so accepted two
expressions the real service rejects. Both rejections are now reproduced, and
these tests exist so nobody quietly loosens them back.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from fakes import FakeTable
from h2o_core import gaps, store
from h2o_core.gaps import GapEvidence, GapSource


def runs_table() -> FakeTable:
    return FakeTable(
        hash_key="run_id",
        range_key="sk",
        indexes={"by-kind": ("kind", "started_at")},
    )


def gaps_table() -> FakeTable:
    return FakeTable(hash_key="gap_id")


# ------------------------------------------------------------------ run records


def test_a_run_and_its_steps_come_back_as_one_envelope() -> None:
    table = runs_table()
    store.write_run(
        {
            "run_id": "r1",
            "kind": "ingest",
            "status": "running",
            "started_at": "2026-08-11T09:00:00",
        },
        table_resource=table,
    )
    store.write_step(
        "r1", 1, {"name": "register", "counts": {"documents": 6}}, table_resource=table
    )
    store.write_step("r1", 2, {"name": "extract", "counts": {"facts": 41}}, table_resource=table)

    run = store.read_run("r1", table_resource=table)

    assert run is not None
    assert run["kind"] == "ingest"
    assert [step["name"] for step in run["steps"]] == ["register", "extract"]
    assert run["steps"][1]["counts"]["facts"] == 41


def test_steps_are_ordered_by_their_index_not_by_arrival() -> None:
    """Step Functions writes these from four separate invocations, so the order
    they land in is not the order they belong in."""
    table = runs_table()
    store.write_run({"run_id": "r1", "kind": "publish", "started_at": "t"}, table_resource=table)
    for index, name in ((3, "reindex_documents"), (1, "rebuild_index"), (2, "reresolve_gaps")):
        store.write_step("r1", index, {"name": name}, table_resource=table)

    run = store.read_run("r1", table_resource=table)

    assert run is not None
    assert [step["name"] for step in run["steps"]] == [
        "rebuild_index",
        "reresolve_gaps",
        "reindex_documents",
    ]


def test_a_run_with_no_envelope_is_not_a_run() -> None:
    """Steps can land before the envelope does. A half-written run reads as
    absent rather than as an empty one, because the console would render an
    empty run as a finished one."""
    table = runs_table()
    store.write_step("r1", 1, {"name": "rebuild_index"}, table_resource=table)

    assert store.read_run("r1", table_resource=table) is None


def test_nothing_having_run_is_a_normal_state() -> None:
    """ADR-005: the polling hook asks for the latest run on every reload, and
    the first ever load has none. None, never an error."""
    assert store.latest_run("ingest", table_resource=runs_table()) is None


def test_runs_of_one_kind_come_back_newest_first() -> None:
    table = runs_table()
    for run_id, started in (("old", "2026-08-01"), ("new", "2026-08-11"), ("mid", "2026-08-05")):
        store.write_run(
            {"run_id": run_id, "kind": "ingest", "started_at": started}, table_resource=table
        )
    store.write_run(
        {"run_id": "other", "kind": "publish", "started_at": "2026-08-12"}, table_resource=table
    )

    assert [r["run_id"] for r in store.list_runs("ingest", table_resource=table)] == [
        "new",
        "mid",
        "old",
    ]
    latest = store.latest_run("ingest", table_resource=table)
    assert latest is not None and latest["run_id"] == "new"


def test_step_rows_never_surface_as_runs() -> None:
    """The by-kind index is sparse: a step row carries no `kind`, so it is not
    in the index at all. A step appearing in the run list would show the console
    a run with no status."""
    table = runs_table()
    store.write_run({"run_id": "r1", "kind": "ingest", "started_at": "t"}, table_resource=table)
    store.write_step("r1", 1, {"name": "register"}, table_resource=table)

    assert [r["run_id"] for r in store.list_runs("ingest", table_resource=table)] == ["r1"]


# ------------------------------------------------- what DynamoDB actually allows


def test_add_refuses_a_nested_path() -> None:
    """Verified against the real table, not assumed.

    ADD is the only atomic counter DynamoDB offers, and it is top-level only.
    `ADD counts.ingestion :one` raises ValidationException, which is why the gap
    queue stores `count_{source}` flat -- a nested counter would need a
    read-modify-write, and two concurrent writers would lose one of the counts.
    """
    table = gaps_table()

    with pytest.raises(ClientError) as raised:
        table.update_item(
            Key={"gap_id": "gas bottle"},
            UpdateExpression="ADD #counts.#src :one",
            ExpressionAttributeNames={"#counts": "counts", "#src": "ingestion"},
            ExpressionAttributeValues={":one": 1},
        )

    assert raised.value.response["Error"]["Code"] == "ValidationException"


def test_a_nested_set_needs_its_parent_to_exist() -> None:
    """The other half of the same lesson: `SET evidence.#eid = :e` against an
    item with no `evidence` map is the same ValidationException. record_miss
    creates the maps first, in a separate call, for exactly this reason."""
    table = gaps_table()

    with pytest.raises(ClientError) as raised:
        table.update_item(
            Key={"gap_id": "gas bottle"},
            UpdateExpression="SET evidence.#eid = :e",
            ExpressionAttributeNames={"#eid": "abc123"},
            ExpressionAttributeValues={":e": {"text": "..."}},
        )

    assert raised.value.response["Error"]["Code"] == "ValidationException"

    table.update_item(
        Key={"gap_id": "gas bottle"},
        UpdateExpression="SET evidence = if_not_exists(evidence, :empty)",
        ExpressionAttributeValues={":empty": {}},
    )
    table.update_item(
        Key={"gap_id": "gas bottle"},
        UpdateExpression="SET evidence.#eid = :e",
        ExpressionAttributeNames={"#eid": "abc123"},
        ExpressionAttributeValues={":e": {"text": "..."}},
    )

    assert table.get_item(Key={"gap_id": "gas bottle"})["Item"]["evidence"]["abc123"]


def test_an_index_key_cannot_be_an_empty_string() -> None:
    """`started_at: ""` to mean "queued, not started yet" is a
    ValidationException, because `started_at` is the by-kind sort key. Found by
    deploying it; a placeholder in a key attribute has to be a real value, and
    queued is a real time -- it is when somebody asked."""
    table = runs_table()

    with pytest.raises(ClientError) as raised:
        store.write_run(
            {"run_id": "r1", "kind": "ingest", "status": "queued", "started_at": ""},
            table_resource=table,
        )

    assert raised.value.response["Error"]["Code"] == "ValidationException"


def test_two_writers_cannot_lose_a_count() -> None:
    """ADR-004's whole ordering claim rests on this number, and the queue is
    written concurrently by ingestion and by chat."""
    table = gaps_table()
    evidence = GapEvidence(
        source=GapSource.ingestion,
        text="Check the gas bottle pressure.",
        locator="04-support-faq.md:12-14",
        occurred_at="2026-08-11T09:00:00",
    )

    gaps.record_miss(
        "gas bottle", source=GapSource.ingestion, evidence=evidence, table_resource=table
    )
    gaps.record_miss(
        "gas bottles", source=GapSource.ingestion, evidence=evidence, table_resource=table
    )
    gaps.record_miss(
        "gas bottle",
        source=GapSource.chat,
        evidence=evidence.model_copy(update={"source": GapSource.chat, "locator": "session-1"}),
        table_resource=table,
    )

    entry = gaps.read_gap("gas bottle", table_resource=table)

    assert entry is not None
    assert entry.total_occurrences == 3
    assert entry.counts == {"ingestion": 2, "chat": 1}
    # The two ingestion misses quote the same locator, so they are one piece of
    # evidence and two counts. Evidence is a sample; the count is the measure.
    assert len(entry.evidence) == 2


def test_a_source_never_seen_is_absent_rather_than_zero() -> None:
    """A zero in the console reads as a measurement -- "we looked in chat and
    found none" -- when the truth is that nobody has asked."""
    table = gaps_table()
    gaps.record_miss(
        "gas bottle",
        source=GapSource.ingestion,
        evidence=GapEvidence(
            source=GapSource.ingestion, text="...", locator="a.md:1", occurred_at="2026-08-11"
        ),
        table_resource=table,
    )

    entry = gaps.read_gap("gas bottle", table_resource=table)

    assert entry is not None
    assert "chat" not in entry.counts
    assert "telemetry" not in entry.counts
