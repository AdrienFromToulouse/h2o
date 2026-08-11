"""Starting an ingest run, and the run record that is its only report.

The pipeline itself is stubbed here. Whether six documents produce twelve held
claims is asserted in packages/h2o_core/tests/test_pipeline.py against the real
corpus; what this file asserts is the worker's own job -- that a run is recorded
whatever happens to it, that the dataset write is conditional, and that a
failure is a row rather than an exception nobody sees.
"""

from __future__ import annotations

from typing import Any

import pytest
from fakes import FakeS3, FakeTable
from fastapi.testclient import TestClient
from h2o_api import ingest
from h2o_core import graph, pipeline, resolver, store
from h2o_core.pipeline import IngestResult


@pytest.fixture(autouse=True)
def _an_index_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker only builds an index when there is none. That branch is the
    bootstrap path and is exercised by its own test."""
    monkeypatch.setattr(resolver, "current", lambda **_: object())


def _result() -> IngestResult:
    return IngestResult(
        documents=6,
        chunks=27,
        facts_extracted=41,
        claims_active=29,
        claims_held=12,
        gaps_recorded=12,
    )


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, result: IngestResult | None = None) -> None:
    monkeypatch.setattr(pipeline, "ingest_corpus", lambda *a, **k: result or _result())


# ------------------------------------------------------------------- the route


def test_starting_a_run_answers_with_its_id_immediately(
    client: TestClient, runs_table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP request has API Gateway's 29 seconds; the corpus does not."""
    started: list[tuple[str, Any]] = []
    monkeypatch.setattr(ingest, "run", lambda run_id, **kw: started.append((run_id, kw)))

    response = client.post("/ingest")

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert response.json()["status"] == "queued"
    assert [call[0] for call in started] == [run_id], "dispatched exactly once"


def test_the_run_is_pollable_before_the_worker_has_written_anything(
    client: TestClient, runs_table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 in the gap between handing back an id and the worker's first write
    would look like a run that never existed."""
    monkeypatch.setattr(ingest, "run", lambda run_id, **kw: None)

    run_id = client.post("/ingest").json()["run_id"]

    polled = client.get(f"/runs/{run_id}")
    assert polled.status_code == 200
    assert polled.json()["status"] == "queued"


# ------------------------------------------------------------------ the worker


def test_a_finished_run_carries_one_step_per_adr_002_step(
    s3: FakeS3,
    runs_table: FakeTable,
    gaps_table: FakeTable,
    registry_table: FakeTable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pipeline(monkeypatch)

    envelope = ingest.run("r1", runs=runs_table, gaps=gaps_table, registry_table=registry_table)

    assert envelope["status"] == "succeeded"
    assert envelope["counts"]["claims_held"] == 12
    assert envelope["watermark"]

    recorded = store.read_run("r1", table_resource=runs_table)
    assert recorded is not None
    assert [step["name"] for step in recorded["steps"]] == [
        "register",
        "chunk_and_index",
        "extract",
        "resolve",
        "detect_conflicts",
        "persist",
    ]


def test_the_dataset_write_is_conditional_on_what_was_loaded(
    s3: FakeS3,
    runs_table: FakeTable,
    gaps_table: FakeTable,
    registry_table: FakeTable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingestion and publish write the same object. An ingest that started
    before a publish landed has to lose here, because a clobbered publish is
    undetectable afterwards."""
    _stub_pipeline(monkeypatch)

    ingest.run("r1", runs=runs_table, gaps=gaps_table, registry_table=registry_table)

    writes = [call for call in s3.put_calls if call["Key"] == graph.config.GRAPH_KEY]
    assert len(writes) == 1
    assert "IfMatch" in writes[0], "an unconditional PUT would silently overwrite a publish"


def test_a_publish_landing_mid_run_makes_the_run_fail_rather_than_win(
    s3: FakeS3,
    runs_table: FakeTable,
    gaps_table: FakeTable,
    registry_table: FakeTable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_pipeline(monkeypatch)

    real_dump = graph.dump

    def publish_lands_first(store_: Any) -> bytes:
        # Somebody else writes the dataset between our load and our put, which
        # is exactly the race the ETag exists for.
        s3.put_object(Bucket="b", Key=graph.config.GRAPH_KEY, Body=b"# published\n")
        monkeypatch.setattr(graph, "dump", real_dump)
        return real_dump(store_)

    monkeypatch.setattr(graph, "dump", publish_lands_first)

    envelope = ingest.run("r1", runs=runs_table, gaps=gaps_table, registry_table=registry_table)

    assert envelope["status"] == "failed"
    assert envelope["error"]


def test_a_failed_run_is_a_row_and_not_an_exception(
    s3: FakeS3,
    runs_table: FakeTable,
    gaps_table: FakeTable,
    registry_table: FakeTable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda retries a failed asynchronous invocation twice. Re-ingesting would
    treble the gap counts, and the count is what ADR-004 orders the queue by --
    so the worker records the failure and returns rather than raising."""

    def explode(*_: Any, **__: Any) -> IngestResult:
        raise RuntimeError("Nova 2 Lite is not enabled in this account")

    monkeypatch.setattr(pipeline, "ingest_corpus", explode)

    envelope = ingest.run("r1", runs=runs_table, gaps=gaps_table, registry_table=registry_table)

    assert envelope["status"] == "failed"
    assert envelope["error"] == "Nova 2 Lite is not enabled in this account"
    assert not [call for call in s3.put_calls if call["Key"] == graph.config.GRAPH_KEY]


def test_a_document_the_registry_does_not_carry_fails_the_run(
    s3: FakeS3, runs_table: FakeTable, gaps_table: FakeTable, registry_table: FakeTable
) -> None:
    """ADR-002: registered and then ingested, never guessed at. Skipping an
    unknown name would ingest nothing and report success."""
    envelope = ingest.run(
        "r1",
        only=["99-does-not-exist.md"],
        runs=runs_table,
        gaps=gaps_table,
        registry_table=registry_table,
    )

    assert envelope["status"] == "failed"
    assert "99-does-not-exist.md" in envelope["error"]


def test_the_subset_is_the_subset(s3: FakeS3) -> None:
    picked = ingest.documents(["04-support-faq.md"])

    assert [record.filename for record, _ in picked] == ["04-support-faq.md"]
    assert "gas" in picked[0][1]
