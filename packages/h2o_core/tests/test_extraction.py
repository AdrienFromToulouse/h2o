"""The verbatim gate: what a model is allowed to have said.

ADR-002 makes the gate mandatory and makes it reject rather than repair. Each
of these is a shape of output a real model produced against the real corpus,
kept as a test because the gate is the only thing standing between a plausible
sentence and a stored fact.
"""

from __future__ import annotations

from typing import Any

import pytest
from h2o_core import extraction
from h2o_core.chunking import Chunk, SourceText, read_source

TEXT = """## Replacing the CO2 cylinder

Turn the dispenser off at the wall before you begin. The cylinder is rated to
2,400 litres and should be replaced when the gauge reads below 10 bar.
"""


@pytest.fixture
def source() -> SourceText:
    return read_source(TEXT, is_html=False)


@pytest.fixture
def chunk(source: SourceText) -> Chunk:
    return Chunk(source_file="manual.md", text=TEXT, start_line=1, end_line=5)


def gate(chunk: Chunk, source: SourceText, *facts: dict[str, Any]) -> extraction.Extraction:
    """Run the gate over a model payload, without the model."""
    return extraction._gate({"facts": list(facts)}, chunk, source)


def _fact(**overrides: Any) -> dict[str, Any]:
    return {
        "subject": "CO2 cylinder",
        "predicate": "rated to",
        "value": "2,400 litres",
        "unit": "L",
        "snippet": "The cylinder is rated to",
        "confidence": 0.9,
        **overrides,
    }


def test_a_verbatim_snippet_passes(chunk: Chunk, source: SourceText) -> None:
    result = gate(chunk, source, _fact())

    assert not result.rejections
    assert result.facts[0]["subject"] == "CO2 cylinder"
    assert result.facts[0]["line_range"]


def test_a_paraphrase_is_rejected_rather_than_repaired(chunk: Chunk, source: SourceText) -> None:
    """The fact may well be true. It is not quotable, and ADR-002 keeps only
    what can be quoted -- so the loss is recorded, not fixed into passing."""
    result = gate(chunk, source, _fact(snippet="The cylinder has a rating of 2400 litres"))

    assert not result.facts
    assert result.rejections[0].reason == "snippet is not verbatim in the source"
    assert "2400 litres" in result.rejections[0].snippet


@pytest.mark.parametrize("subject", ["", "   ", "—", '"', "...", "•"])
def test_a_subject_that_names_no_term_is_rejected(
    chunk: Chunk, source: SourceText, subject: str
) -> None:
    """Found by running the real extractor against the real corpus.

    Nova 2 Lite emits these -- a stray bullet, a bare quotation mark. Such a
    fact cannot resolve, and it cannot become a gap entry either: a gap is keyed
    by the surface form a curator would add a label for, and there is none. Left
    to run, it reaches DynamoDB as an empty partition key, which reports the
    symptom and not the cause.
    """
    result = gate(chunk, source, _fact(subject=subject))

    assert not result.facts
    assert result.rejections[0].reason == "the subject normalises to nothing, so it names no term"


def test_one_bad_fact_does_not_discard_the_good_ones(chunk: Chunk, source: SourceText) -> None:
    """Rejection is per fact. A chunk is not all-or-nothing, or one stray bullet
    would cost every claim the model found beside it."""
    result = gate(
        chunk,
        source,
        _fact(subject="•"),
        _fact(),
        _fact(snippet="invented text that is not in the document"),
    )

    assert len(result.facts) == 1
    assert len(result.rejections) == 2


def test_a_payload_that_is_not_the_schema_is_evidence(chunk: Chunk, source: SourceText) -> None:
    result = extraction._gate({"facts": [{"subject": "CO2 cylinder"}]}, chunk, source)

    assert not result.facts
    assert "did not match the schema" in result.rejections[0].reason
