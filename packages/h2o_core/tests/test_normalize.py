"""The single definition of label identity, and the lossless de-markup rule."""

from pathlib import Path

import pytest
from h2o_core.normalize import flatten_html, normalise

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_SHEET = REPO_ROOT / "data" / "docs" / "05-spec-sheet-FS-500-SPK.html"


# ------------------------------------------------------- one definition, not three


@pytest.mark.parametrize("script", ["check_vocab.py", "check_corpus.py"])
def test_the_checks_import_the_normaliser_rather_than_copying_it(script: str) -> None:
    """The gates must run the real function, not a copy that agrees today.

    Both scripts once defined their own `normalise`, byte-identical, and
    check_corpus's docstring read "Must stay identical to check_vocab.py" -- a
    comment standing in for an import. A resolver-parity check against a copy
    proves the copy is self-consistent and says nothing about the index it
    protects (ADR-005 §3).

    This reads the source rather than importing, because both scripts run their
    checks at module level and exit; that is the right shape for a CLI and the
    wrong shape to import. What it guards is the regression that matters:
    somebody re-adding a local definition.
    """
    source = (REPO_ROOT / "scripts" / script).read_text()

    assert "from h2o_core.normalize import" in source
    assert "def normalise(" not in source
    assert "def flatten_html(" not in source


# ------------------------------------------------------------------- normalise


def test_folds_the_case_sparql_cannot_see() -> None:
    """The reason the SHACL collision shape needs a Python complement.

    LCASE reads these as two labels. The resolver reads one, so a vocabulary
    containing both would resolve arbitrarily and make one concept unreachable.
    """
    assert normalise("CO₂ Cylinder") == normalise("CO2 Cylinder") == "co2 cylinder"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Carbon Filter", "carbon filter"),
        ("  Carbon   Filter  ", "carbon filter"),
        ("Carbon-Filter", "carbon filter"),
        ("CARBON_FILTER", "carbon filter"),
        ("Koolstoffilter", "koolstoffilter"),
        ("Café", "cafe"),
        ("Sanitisation!", "sanitisation"),
        ("", ""),
        ("   ", ""),
        # NFKD decomposes the compatibility character to its letters, so this
        # is "tm" and not empty. Recorded because it is surprising, and because
        # a label ending in a trademark sign is exactly the kind of thing a
        # manufacturer's vocabulary contains.
        ("Aquaflow®™", "aquaflow tm"),
    ],
)
def test_normalisation_cases(label: str, expected: str) -> None:
    assert normalise(label) == expected


def test_does_not_stem() -> None:
    """Deliberate: the gap queue merges plurals, the resolver does not.

    Teaching this function to stem would change what "two labels collide" means
    and break the parity guarantee the integrity gate depends on, so the plural
    fold lives in the gap queue's own key instead (ADR-004 §2).
    """
    assert normalise("gas bottles") != normalise("gas bottle")


def test_is_idempotent() -> None:
    """The index is built from normalised text and queried with it."""
    for label in ("CO₂ Cylinder", "  Carbon-Filter ", "Café"):
        assert normalise(normalise(label)) == normalise(label)


# ----------------------------------------------------------------- flatten_html


def test_reads_entities_the_way_a_reader_does() -> None:
    """A byte comparison against raw markup rejects a correct citation."""
    assert flatten_html('<div class="price">&pound;7.99</div>') == "£7.99"


def test_drops_comments_before_tags() -> None:
    """A commented-out tag must not survive as text.

    Stripping tags first would leave the comment's inner text behind, and the
    spec sheet carries an HTML comment explaining why it is HTML at all.
    """
    assert flatten_html("<p>a<!-- <b>hidden</b> -->b</p>") == "ab"


def test_the_spec_sheet_flattens_to_its_readable_price() -> None:
    """The corpus document that exists to exercise this rule (ADR-002)."""
    text = flatten_html(SPEC_SHEET.read_text())

    assert "£1,249.00" in text
    assert "&pound;" not in text
    assert "<" not in text


def test_introduces_no_character_the_document_lacked() -> None:
    """What makes the transform safe to cite against.

    Every character in the output came from the source or from an entity the
    source wrote. This is the line between de-markup, which may be cited, and
    OCR repair, which may not.
    """
    markup = "<p>2.4&nbsp;L/min &ndash; 18 L/h</p>"

    assert flatten_html(markup) == "2.4\xa0L/min – 18 L/h"
