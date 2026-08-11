"""One run surface for three kinds of run (ADR-005 §5)."""

from __future__ import annotations

from fakes import FakeTable
from fastapi.testclient import TestClient
from h2o_core import store


def _run(table: FakeTable, run_id: str, kind: str, started: str) -> None:
    store.write_run(
        {"run_id": run_id, "kind": kind, "status": "succeeded", "started_at": started},
        table_resource=table,
    )


def test_nothing_having_run_yet_is_null_and_not_a_404(
    client: TestClient, runs_table: FakeTable
) -> None:
    """The console asks this on every reload, before anything has ever run.

    A 404 would make "no run has ever happened" indistinguishable from an error,
    on the one code path whose entire job is to recover from a page reload.
    """
    response = client.get("/runs/latest", params={"kind": "ingest"})

    assert response.status_code == 200
    assert response.json() is None


def test_an_unknown_run_is_a_404_in_plain_language(
    client: TestClient, runs_table: FakeTable
) -> None:
    response = client.get("/runs/does-not-exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "There is no run by that name."


def test_a_run_reads_back_with_its_steps(client: TestClient, runs_table: FakeTable) -> None:
    _run(runs_table, "r1", "ingest", "2026-08-11T09:00:00")
    store.write_step(
        "r1", 1, {"name": "register", "counts": {"documents": 6}}, table_resource=runs_table
    )

    body = client.get("/runs/r1").json()

    assert body["kind"] == "ingest"
    assert body["steps"][0]["counts"]["documents"] == 6


def test_the_three_kinds_share_one_endpoint(client: TestClient, runs_table: FakeTable) -> None:
    """ADR-005 taken literally: one table, one envelope, one endpoint, and
    `kind` to tell them apart. A second polling surface is the thing the ADR
    was avoiding."""
    _run(runs_table, "i1", "ingest", "2026-08-11T09:00:00")
    _run(runs_table, "p1", "publish", "2026-08-11T10:00:00")
    _run(runs_table, "t1", "telemetry", "2026-08-11T11:00:00")

    everything = client.get("/runs").json()
    assert {run["run_id"] for run in everything} == {"i1", "p1", "t1"}

    assert [run["run_id"] for run in client.get("/runs", params={"kind": "publish"}).json()] == [
        "p1"
    ]
    assert client.get("/runs/latest", params={"kind": "telemetry"}).json()["run_id"] == "t1"


def test_runs_come_back_newest_first(client: TestClient, runs_table: FakeTable) -> None:
    _run(runs_table, "old", "ingest", "2026-08-01T09:00:00")
    _run(runs_table, "new", "ingest", "2026-08-11T09:00:00")

    assert [run["run_id"] for run in client.get("/runs").json()] == ["new", "old"]
