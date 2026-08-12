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
    assert flatten_html('<div class="price">&pound;7.99</div>').strip() == "£7.99"


def test_drops_comments_before_tags() -> None:
    """A commented-out tag must not survive as text.

    Stripping tags first would leave the comment's inner text behind, and the
    spec sheet carries an HTML comment explaining why it is HTML at all.
    """
    # "ab", not "a b": the comment leaves no boundary behind, and the only
    # block tags are the outer <p> pair, which strip away at the edges.
    assert flatten_html("<p>a<!-- <b>hidden</b> -->b</p>").strip() == "ab"


# --------------------------------------------- block boundaries are whitespace


def test_a_table_row_does_not_run_its_cells_together() -> None:
    """The bug the deployed console showed.

    `Supply pressure1.5 – 6.0 bar` was the stored snippet, faithfully quoting a
    flattened text that no reader of that table has ever seen. It was not only a
    display fault: chunks are built from this text and handed to the extractor,
    so the model was deciding `subject` and `value` from the glue too.
    """
    row = "<tr><th>Supply pressure</th><td>1.5 &ndash; 6.0 bar</td></tr>"

    assert flatten_html(row).strip() == "Supply pressure 1.5 – 6.0 bar"


def test_an_inline_tag_does_not_break_a_word() -> None:
    """The other half of the rule, and the reason it is a list of block tags
    rather than "insert a space for every tag". A reader sees £7.99."""
    assert flatten_html("<b>&pound;7</b>.99") == "£7.99"
    assert flatten_html("2.4 <span>L</span>/min") == "2.4 L/min"


def test_it_never_emits_two_spaces_in_a_row() -> None:
    """`</th><td>` is two adjacent block tags, so the naive version emits two
    separators. `chunking.locate`'s fallback searches a whitespace-collapsed
    copy of the text, and a run there would shift every offset it returns."""
    flat = flatten_html("<tr><th>a</th><td>b</td></tr><tr><th>c</th><td>d</td></tr>")

    assert "  " not in flat


def test_whitespace_the_document_wrote_is_left_alone() -> None:
    """Only the spaces this module *introduces* are collapsed. A newline or a
    non-breaking space is content -- `&nbsp;` is how the spec sheet writes
    `5 °C` -- and folding it would be a second, silent departure from the rule.
    """
    assert flatten_html("<p>a\n\nb</p>").strip() == "a\n\nb"
    assert flatten_html("<td>5&nbsp;&deg;C</td>").strip() == "5\xa0°C"


def test_the_spec_sheet_flattens_to_its_readable_price() -> None:
    """The corpus document that exists to exercise this rule (ADR-002)."""
    text = flatten_html(SPEC_SHEET.read_text())

    assert "£1,249.00" in text
    assert "&pound;" not in text
    assert "<" not in text


def test_invents_no_content_the_document_lacked() -> None:
    """What makes the transform safe to cite against, stated correctly.

    This used to assert byte preservation -- "introduces no character the
    document did not contain" -- and the assertion was already only half true:
    `chunking.read_source` strips each line's indentation and drops blank lines,
    which deletes characters the document did contain. Held to the letter, the
    rule also produced `Supply pressure1.5`, failing the standard set in
    `flatten_html`'s own first sentence.

    The rule it is really held to is **invent no content**, and that is the line
    against OCR repair: rewriting `ug`→`µg` claims a datasheet printed a
    character it never printed, and the verbatim gate would certify the
    fabrication. Whitespace at a box boundary claims nothing.

    So: every *non-whitespace* character in the output came from the source or
    from an entity the source wrote, in order, with nothing added between them.
    """
    markup = "<p>2.4&nbsp;L/min &ndash; 18 L/h</p>"
    flat = flatten_html(markup)

    assert flat.strip() == "2.4\xa0L/min – 18 L/h"
    assert "".join(flat.split()) == "".join("2.4\xa0L/min – 18 L/h".split())


def test_the_spec_sheet_stays_aligned_under_collapsing() -> None:
    """The invariant `chunking.locate`'s fallback silently depends on.

    Its fallback finds an offset in a whitespace-collapsed copy of the text.
    That offset is only usable against the original if collapsing preserves
    length, which for HTML holds because `read_source` strips each line and this
    module emits no run. `_collapsed_with_origin` now maps back properly and no
    longer needs the coincidence -- but losing it would still mean the citable
    text had gained a whitespace run, which is worth failing over.
    """
    import re

    from h2o_core.chunking import read_source

    text = read_source(SPEC_SHEET.read_text(), is_html=True).text

    assert len(text) == len(re.sub(r"\s+", " ", text))
