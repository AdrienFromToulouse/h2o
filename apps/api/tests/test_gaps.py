"""The gap queue over HTTP, and the endpoint that deliberately cannot write."""

from __future__ import annotations

from fakes import FakeTable
from fastapi.testclient import TestClient
from h2o_core import gaps
from h2o_core.gaps import GapEvidence, GapSource
from h2o_core.resolver import Candidate


def _miss(
    table: FakeTable,
    surface: str,
    *,
    source: GapSource = GapSource.ingestion,
    locator: str = "02-service-bulletin-SB-2024-03.md:12-14",
    suggestions: list[Candidate] | None = None,
) -> None:
    gaps.record_miss(
        surface,
        source=source,
        evidence=GapEvidence(
            source=source,
            text=f"Check the {surface} pressure before servicing.",
            locator=locator,
            occurred_at="2026-08-11T09:00:00",
        ),
        suggestions=suggestions,
        table_resource=table,
    )


def test_the_queue_is_ordered_by_how_often_something_was_actually_said(
    client: TestClient, gaps_table: FakeTable
) -> None:
    """ADR-004 rules out a learned ranking: at this scale, occurrence order is
    sufficient and, more to the point, a curator can see why an entry is top."""
    for index in range(3):
        _miss(gaps_table, "gas bottle", locator=f"a.md:{index}")
    _miss(gaps_table, "scale buildup")

    body = client.get("/gaps").json()

    assert [entry["surface_form"] for entry in body] == ["gas bottle", "scale buildup"]
    assert body[0]["total_occurrences"] == 3


def test_an_entry_carries_its_evidence_and_its_shortlist(
    client: TestClient, gaps_table: FakeTable
) -> None:
    _miss(
        gaps_table,
        "gas bottle",
        suggestions=[
            Candidate(concept_id="co2-cylinder", pref_label="CO₂ Cylinder", score=0.81),
            Candidate(concept_id="mineral-cartridge", pref_label="Mineral Cartridge", score=0.44),
        ],
    )

    entry = client.get("/gaps/gas bottle").json()

    assert entry["counts"] == {"ingestion": 1}
    assert entry["evidence"][0]["locator"].endswith(":12-14")
    assert entry["suggestions"][0]["pref_label"] == "CO₂ Cylinder"


def test_actioning_a_gap_writes_nothing(client: TestClient, gaps_table: FakeTable) -> None:
    """ADR-004 §6: actioning writes nothing, and a GET is how that is enforced
    rather than remembered. The status moves to `actioned` from fan-out step 2
    and nowhere else -- the queue closes because a publish closed it.
    """
    _miss(
        gaps_table,
        "gas bottle",
        suggestions=[Candidate(concept_id="co2-cylinder", pref_label="CO₂ Cylinder", score=0.81)],
    )
    gaps_table.writes.clear()

    target = client.get("/gaps/gas bottle/target").json()

    assert target["concept_id"] == "co2-cylinder"
    assert target["suggested_label"] == "gas bottle"
    assert target["question"]
    assert gaps_table.writes == [], "a GET that mutated would be the whole point, lost"
    assert client.get("/gaps/gas bottle").json()["status"] == "open"


def test_a_gap_with_no_suggestion_still_has_somewhere_to_send_a_curator(
    client: TestClient, gaps_table: FakeTable
) -> None:
    """ "None of these" is a real answer: the term may need a new concept rather
    than an alternative label, and the target says so by naming no concept."""
    _miss(gaps_table, "scale buildup")

    target = client.get("/gaps/scale buildup/target").json()

    assert target["concept_id"] is None
    assert target["suggested_label"] == "scale buildup"


def test_dismissal_needs_a_reason_and_keeps_counting(
    client: TestClient, gaps_table: FakeTable
) -> None:
    """Not permanent amnesty. Counts accrue while dismissed, which is what makes
    ADR-004's 100x resurface rule computable rather than aspirational."""
    _miss(gaps_table, "gas bottle")

    assert client.post("/gaps/gas bottle/dismiss", json={}).status_code == 422
    assert client.post("/gaps/gas bottle/dismiss", json={"reason": ""}).status_code == 422

    dismissed = client.post(
        "/gaps/gas bottle/dismiss", json={"reason": "Marketing copy, not a real term."}
    ).json()
    assert dismissed["status"] == "dismissed"
    assert dismissed["dismissed_at_count"] == 1

    _miss(gaps_table, "gas bottle", locator="b.md:9")
    assert client.get("/gaps/gas bottle").json()["total_occurrences"] == 2


def test_an_unknown_gap_answers_in_plain_language(
    client: TestClient, gaps_table: FakeTable
) -> None:
    response = client.get("/gaps/nothing-by-that-name")

    assert response.status_code == 404
    assert response.json()["detail"] == "There is no gap by that name in the queue."
