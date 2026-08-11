"""Contradiction detection, driven by what registry.json declares.

Parametrised over the seeded contradictions rather than over invented fixtures,
so the corpus and the detector cannot drift apart: if someone edits a document
and the disagreement stops being real, this fails.
"""

import json
from pathlib import Path

import pytest
from h2o_core.conflicts import ClaimLike, detect, predicate_key
from h2o_core.units import parse_quantity

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY = json.loads((REPO_ROOT / "data" / "docs" / "registry.json").read_text())
SEEDED = REGISTRY["seeded_contradictions"]


def _claims_from(entry: dict) -> list[ClaimLike]:
    return [
        ClaimLike(
            claim_id=f"{entry['subject']}#{index}",
            subject=entry["subject"],
            predicate=entry["predicate"],
            value=claim["value"],
            source_file=claim["source"],
            doc_version=claim["doc_version"],
        )
        for index, claim in enumerate(entry["claims"])
    ]


@pytest.mark.parametrize("entry", SEEDED, ids=[e["subject"] for e in SEEDED])
def test_every_seeded_contradiction_is_flagged(entry: dict) -> None:
    conflicts = detect(_claims_from(entry))

    assert len(conflicts) == 1, f"{entry['subject']}: expected exactly one conflict"
    assert conflicts[0].predicate == predicate_key(entry["predicate"])


@pytest.mark.parametrize("entry", SEEDED, ids=[e["subject"] for e in SEEDED])
def test_every_side_survives_including_the_ones_that_agree(entry: dict) -> None:
    """The carbon-filter group has three sides and two of them say six months.

    Dropping the duplicate would hide that *two* documents in circulation tell
    technicians six, which is exactly what a documentation owner needs to know.
    """
    (conflict,) = detect(_claims_from(entry))

    assert conflict.sides == len(entry["claims"])
    assert conflict.values == [c["value"] for c in entry["claims"]]


def test_nothing_is_resolved() -> None:
    """No source precedence, no recency. The Conflict carries every value and
    names no winner -- there is no field on it that could hold one."""
    (conflict,) = detect(_claims_from(SEEDED[0]))

    assert sorted(set(conflict.values)) == ["4 months", "6 months"]
    assert not hasattr(conflict, "resolved")
    assert not hasattr(conflict, "winner")


def test_agreement_is_not_a_conflict() -> None:
    """The manual and the spec sheet both say six months. Two sources agreeing
    must not enter the queue."""
    agreeing = [c for c in _claims_from(SEEDED[0]) if c.value == "6 months"]

    assert len(agreeing) == 2
    assert detect(agreeing) == []


def test_a_threshold_is_not_a_competing_claim() -> None:
    """06-support-article says a healthy unit dispenses at 1.8 L/min and that
    firmware raises low flow "below roughly 1.2 L/min". The second is a
    threshold. Treating it as a third dispense rate would put a false conflict
    in front of a curator who cannot resolve it, because nothing is wrong.
    """
    claims = [
        ClaimLike(claim_id="a", subject="fs-500-spk", predicate="dispense-rate", value="2.4 L/min"),
        ClaimLike(claim_id="b", subject="fs-500-spk", predicate="dispense-rate", value="1.8 L/min"),
        ClaimLike(
            claim_id="c",
            subject="fs-500-spk",
            predicate="dispense-rate",
            value="below roughly 1.2 L/min",
        ),
    ]

    assert parse_quantity("below roughly 1.2 L/min").approximate
    (conflict,) = detect(claims)
    assert conflict.claim_ids == ["a", "b"]


def test_precision_is_not_disagreement() -> None:
    """2.40 and 2.4 are one measurement written twice."""
    claims = [
        ClaimLike(claim_id="a", subject="x", predicate="dispense-rate", value="2.4 L/min"),
        ClaimLike(claim_id="b", subject="x", predicate="dispense-rate", value="2.40 L/min"),
    ]

    assert detect(claims) == []


def test_calendar_and_exact_durations_are_never_compared() -> None:
    """h2o's version of micrograms versus IU. A month is not a fixed number of
    hours, so these two claims are not in disagreement -- and declaring one
    would be a confident, silent error."""
    claims = [
        ClaimLike(claim_id="a", subject="carbon-filter", predicate="life", value="6 months"),
        ClaimLike(claim_id="b", subject="carbon-filter", predicate="life", value="4000 hours"),
    ]

    assert detect(claims) == []


def test_held_claims_can_contradict_each_other() -> None:
    """Detection does not wait for the vocabulary to catch up: two unresolved
    mentions of the same unnamed thing can still disagree."""
    claims = [
        ClaimLike(
            claim_id="a", subject="held:gas bottle", predicate="pressure", value="60 bar", held=True
        ),
        ClaimLike(
            claim_id="b", subject="held:gas bottle", predicate="pressure", value="55 bar", held=True
        ),
    ]

    (conflict,) = detect(claims)
    assert conflict.sides == 2


def test_the_resolver_replaces_the_alias_map() -> None:
    """kai needed 25 hand-written attribute aliases because it had no
    vocabulary. Only predicates still need folding here."""
    assert predicate_key("Replacement Interval") == "replacement-interval"
    assert predicate_key("service interval") == "replacement-interval"
    assert predicate_key("flow rate") == "dispense-rate"
