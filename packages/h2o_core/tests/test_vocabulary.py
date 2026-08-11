"""Projections of the published graph, asserted against the vocabulary that ships."""

import json

import pyoxigraph
from h2o_core import vocabulary

#: ADR-006 §2 makes the non-technical contract testable, so this is the test.
#: A review card is what a domain expert sees, and every one of these strings is
#: a reason for that person to conclude the tool is not for them.
FORBIDDEN_IN_DEFAULT_VIEW = (
    "skos:",
    "owl:",
    "dct:",
    "http://",
    "https://",
    "@prefix",
    "prefLabel",
    "altLabel",
    "broader",
    "narrower",
    "ConceptScheme",
    "component.type",
    "fault.code",
    "water.output",
    "service.type",
)


def test_the_scheme_tree_holds_the_six_business_vocabularies(store: pyoxigraph.Store) -> None:
    tree = vocabulary.scheme_tree(store)

    assert [s.scheme_id for s in tree.schemes] == [
        "equipment",
        "fault",
        "service",
        "sustainability",
        "treatment",
        "water-output",
    ]
    assert sum(s.concept_count for s in tree.schemes) == 57
    assert [c.pref_label for c in tree.top_concepts["equipment"]] == ["Component", "Dispenser"]


def test_the_machine_scheme_is_excluded_by_default(store: pyoxigraph.Store) -> None:
    """It is a real concept scheme holding firmware's names for things, and
    ADR-006 keeps those out of the interface a domain expert works in."""
    assert "telemetry" not in {s.scheme_id for s in vocabulary.scheme_tree(store).schemes}

    with_machine = vocabulary.scheme_tree(store, include_machine=True)
    assert "telemetry" in {s.scheme_id for s in with_machine.schemes}


def test_every_label_in_the_vocabulary_is_indexed(store: pyoxigraph.Store) -> None:
    """The resolver index's raw material. A label missing here is a term that
    silently stops resolving."""
    rows = vocabulary.concept_labels(store)

    assert len(rows) == 260
    assert {r.kind for r in rows} == {"pref", "alt"}

    carbon = {r.text for r in rows if r.concept_id == "carbon-filter"}
    assert {"Carbon Filter", "Koolstoffilter", "Carbon Cartridge", "Filter Cartridge"} <= carbon


def test_the_review_card_renders_from_real_data(store: pyoxigraph.Store) -> None:
    """ADR-006's mock, field for field, against the concept that ships."""
    card = vocabulary.concept(store, "carbon-filter")
    assert card is not None

    assert card.pref_label == {"en": "Carbon Filter", "nl": "Koolstoffilter"}
    assert card.definition["en"].startswith("A filter using activated carbon")
    assert {"Carbon Cartridge", "Filter Cartridge"} <= set(card.alt_labels)
    assert card.parent is not None and card.parent.pref_label == "Filter"
    assert [r.pref_label for r in card.related] == ["Purification"]
    assert card.version == 1
    assert card.scheme is not None and card.scheme.title == "Equipment"
    assert card.notation == "EQ-FLT-CARB"
    assert not card.deprecated


def test_the_machine_signal_row_shows_an_instrument_not_an_attribute(
    store: pyoxigraph.Store,
) -> None:
    """ADR-006's carve-out, made explicit.

    The instrument name is what the machine measures and is shown read-only. The
    attribute key and value are firmware's private naming and stay behind the
    technical toggle -- `notation` carries the wire token so the mapping can be
    explained on request, and the console never renders it by default.
    """
    card = vocabulary.concept(store, "carbon-filter")
    assert card is not None

    (signal,) = card.machine_signals
    assert signal.signal == "dispenser.filter.life_remaining"
    assert signal.unit == "%"
    assert signal.match == "exact"
    assert signal.notation == "carbon_filter"


def test_an_approximate_mapping_says_so(store: pyoxigraph.Store) -> None:
    """Whether E17 is Low Flow is a domain judgement the model cannot make.
    closeMatch plus a scope note records the near-equivalence honestly."""
    card = vocabulary.concept(store, "low-flow")
    assert card is not None

    kinds = {s.match for s in card.machine_signals}
    assert "close" in kinds
    assert any(s.scope_note for s in card.machine_signals)


def test_a_term_that_does_not_exist_is_none(store: pyoxigraph.Store) -> None:
    """The seeded gap. No limescale concept exists anywhere, and a test fails if
    one is quietly filled in (ADR-001)."""
    assert vocabulary.concept(store, "limescale") is None


def test_the_card_carries_no_technical_vocabulary_outside_the_toggle(
    store: pyoxigraph.Store,
) -> None:
    """ADR-006: "This is a hard constraint, not a guideline, and it is testable."

    Serialising the card with `technical` removed is what the default view has
    to work from, so nothing in it may name a SKOS property, an IRI or an OTEL
    attribute key.
    """
    for concept_id in ("carbon-filter", "co2-cylinder", "low-flow", "sparkling"):
        card = vocabulary.concept(store, concept_id)
        assert card is not None

        default_view = card.model_dump(mode="json", exclude={"technical"})
        # `notation` is the wire token behind the toggle, not a default-view field.
        for signal in default_view["machine_signals"]:
            signal.pop("notation", None)
        rendered = json.dumps(default_view)

        for forbidden in FORBIDDEN_IN_DEFAULT_VIEW:
            assert forbidden not in rendered, f"{concept_id} leaks {forbidden!r}"


def test_the_technical_detail_carries_the_iri(store: pyoxigraph.Store) -> None:
    """Hiding it must cost nothing: whoever wants the IRI can have it."""
    card = vocabulary.concept(store, "carbon-filter")
    assert card is not None

    assert card.technical.iri == "https://vocab.h2o.example/id/carbon-filter"
    assert card.technical.scheme_iri.endswith("/equipment")
