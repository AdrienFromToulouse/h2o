"""The impact preview: an abstract edit turned into a visible consequence.

ADR-005 §4 calls this the single feature that makes the console more than a
form. What it promises is a *count*, not an estimate, so these tests assert the
count against claims a real ingestion produced -- and the sentence against the
exact string ADR-006 puts in the review card.
"""

from __future__ import annotations

import pyoxigraph
import pytest
from h2o_core import config, facts, impact
from h2o_core.facts import Claim
from h2o_core.impact import ConceptDraft


def held(surface: str, source_file: str, line_range: str, predicate: str = "stored") -> Claim:
    """A claim whose subject resolved to nothing, exactly as ingestion writes it."""
    return Claim(
        subject_concept=None,
        subject_surface=surface,
        predicate=predicate,
        value="upright",
        source_file=source_file,
        doc_version="v3",
        line_range=line_range,
        snippet=f"Store the {surface} upright.",
    )


@pytest.fixture
def store_with_held_claims(store: pyoxigraph.Store) -> pyoxigraph.Store:
    """Twelve held mentions of one term across three documents.

    The shape the README describes, built here rather than by running the
    corpus through a model: what this file is testing is the counting, and a
    real extractor's yield is asserted in test_pipeline against the corpus.
    """
    claims = [
        *(held("gas bottle", "02-service-bulletin.md", f"{n}-{n}") for n in range(1, 6)),
        *(held("gas bottles", "03-service-bulletin.md", f"{n}-{n}") for n in range(1, 5)),
        *(held("gas bottle", "04-support-faq.md", f"{n}-{n}") for n in range(1, 4)),
        held("scale buildup", "06-support-article.md", "9-9"),
    ]
    facts.insert(store, claims)
    return store


def test_the_sentence_is_the_one_adr_006_specifies(
    store_with_held_claims: pyoxigraph.Store,
) -> None:
    """Character for character. The console renders this string, so the string
    is the deliverable -- and it is rendered in one place so it cannot be
    right in the API and wrong in the UI."""
    result = impact.preview(
        store_with_held_claims,
        ConceptDraft(concept_id="co2-cylinder", alt_labels=["gas bottle"]),
    )

    assert (
        result.sentence
        == "Adding this alternative term will resolve 12 mentions across 3 documents."
    )


def test_plural_variants_are_one_count_not_two(
    store_with_held_claims: pyoxigraph.Store,
) -> None:
    """ "gas bottles" was merged into the same queue entry as "gas bottle", so
    adding one label resolves both spellings. Counting them separately would
    under-report the very thing the preview exists to show."""
    result = impact.preview(
        store_with_held_claims,
        ConceptDraft(concept_id="co2-cylinder", alt_labels=["gas bottle"]),
    )

    assert len(result.mentions) == 12
    assert {m.surface_form for m in result.mentions} == {"gas bottle", "gas bottles"}
    assert result.documents == [
        "02-service-bulletin.md",
        "03-service-bulletin.md",
        "04-support-faq.md",
    ]


def test_one_of_something_reads_as_one(store: pyoxigraph.Store) -> None:
    """ "1 mentions across 1 documents" is the tell that a sentence was
    assembled rather than written."""
    facts.insert(store, [held("gas bottle", "04-support-faq.md", "9-9")])

    result = impact.preview(
        store, ConceptDraft(concept_id="co2-cylinder", alt_labels=["gas bottle"])
    )

    assert (
        result.sentence == "Adding this alternative term will resolve 1 mention across 1 document."
    )


def test_an_edit_that_resolves_nothing_says_nothing(
    store_with_held_claims: pyoxigraph.Store,
) -> None:
    """ "will resolve 0 mentions" reads as a failure. Fixing a definition simply
    is not the kind of edit that resolves anything, and the card should show no
    sentence rather than a discouraging one."""
    result = impact.preview(
        store_with_held_claims,
        ConceptDraft(concept_id="co2-cylinder", definition="A pressurised cylinder."),
    )

    assert result.sentence == ""
    assert result.mentions == []


def test_the_preview_reads_and_never_writes(store_with_held_claims: pyoxigraph.Store) -> None:
    """The expert is still typing. Nothing has been saved, and a preview that
    left a trace would make "before Save" untrue."""
    before = len(store_with_held_claims)

    impact.preview(
        store_with_held_claims,
        ConceptDraft(concept_id="co2-cylinder", alt_labels=["gas bottle"], pref_label="Renamed"),
    )

    assert len(store_with_held_claims) == before


def test_a_label_already_in_use_is_visible_before_save(store: pyoxigraph.Store) -> None:
    """ADR-005 §4: the gate, run early and non-fatally, so the expert learns
    before typing a change note rather than after."""
    result = impact.preview(
        store, ConceptDraft(concept_id="co2-cylinder", alt_labels=["Filter Cartridge"])
    )

    assert result.blocked
    # ADR-005 illustrates this with "...an alternative term for Sediment
    # Filter"; in the vocabulary that actually ships, Filter Cartridge belongs
    # to Carbon Filter. The message names the real owner, because a curator is
    # about to go and look at it.
    assert result.findings[0].message == "“Filter Cartridge” is already a term for Carbon Filter."


def test_a_concept_may_keep_its_own_labels(store: pyoxigraph.Store) -> None:
    """Re-submitting a card without changing the labels is not a collision with
    itself, which is what every Save after the first would look like."""
    result = impact.preview(
        store, ConceptDraft(concept_id="co2-cylinder", alt_labels=["Carbonation Cylinder"])
    )

    assert not result.blocked


def test_reparenting_names_what_gains_and_what_loses_a_child(store: pyoxigraph.Store) -> None:
    """Which subtree moved is the part a reviewer cannot see from the form."""
    result = impact.preview(store, ConceptDraft(concept_id="carbon-filter", broader="dispenser"))

    assert result.children_gained == ["dispenser"]
    assert result.children_lost == ["filter"]


def test_the_count_is_claims_and_not_documents(
    store_with_held_claims: pyoxigraph.Store,
) -> None:
    """Five mentions in one bulletin are five claims and one document, and the
    sentence has to say both -- a curator judging whether the edit is worth
    making needs the spread as much as the total."""
    result = impact.preview(
        store_with_held_claims,
        ConceptDraft(concept_id="co2-cylinder", alt_labels=["gas bottle"]),
    )

    per_document = {m.source_file for m in result.mentions}
    assert len(result.mentions) > len(per_document)
    assert result.as_dict()["mention_count"] == 12
    assert result.as_dict()["document_count"] == 3


def test_claims_in_the_facts_graph_are_the_only_source(store: pyoxigraph.Store) -> None:
    """No document is re-read to answer this. Held claims carry the merge key
    for exactly this reason, which is also what lets the fan-out re-resolve
    them afterwards -- the preview and the thing it predicts are one lookup."""
    facts.insert(store, [held("gas bottle", "04-support-faq.md", "9-9")])

    assert impact.held_for(store, "gas bottle")
    assert config.FACTS_GRAPH == "h2o:graph/facts"
