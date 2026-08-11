"""Chunking, and the two rules that decide what a citation may say."""

from pathlib import Path

from h2o_core.chunking import chunk_document, locate, read_source

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "data" / "docs"
MANUAL = DOCS / "01-installation-manual-v3.md"
SPEC_SHEET = DOCS / "05-spec-sheet-FS-500-SPK.html"


def test_markdown_chunks_split_on_headings() -> None:
    """A section is the unit a reader would quote. A chunk spanning two
    sections retrieves for a question about either and answers neither well."""
    source = read_source(MANUAL.read_text(), is_html=False)
    chunks = chunk_document(source, MANUAL.name)

    assert len(chunks) > 3
    assert all(c.start_line <= c.end_line for c in chunks)
    assert any(c.heading and "Water connection" in c.heading for c in chunks)


def test_line_ranges_point_into_the_original_file() -> None:
    """A line range that does not survive a reviewer opening the file is not a
    citation, it is decoration."""
    lines = MANUAL.read_text().splitlines()
    source = read_source(MANUAL.read_text(), is_html=False)

    for chunk in chunk_document(source, MANUAL.name):
        first = chunk.text.splitlines()[0].strip()
        if first:
            assert first in lines[chunk.start_line - 1]


def test_html_is_cited_against_its_readable_text() -> None:
    """ADR-002: a reader of <div>&pound;7.99</div> sees £7.99, so that is what
    an extractor quotes. Checking that against raw markup would reject a correct
    citation as a fabrication."""
    source = read_source(SPEC_SHEET.read_text(), is_html=True)

    assert "£1,249.00" in source.text
    assert "&pound;" not in source.text
    assert "<div" not in source.text


def test_an_html_comment_is_not_citable_as_the_document() -> None:
    """The bug this corpus is unusually good at catching.

    05-spec-sheet carries an HTML comment explaining the de-markup rule, and it
    quotes the very price the rule is about. Flattening line by line never sees
    a multi-line comment, so the note *about* the document became quotable *as*
    the document -- and `locate` cheerfully returned a line number for it.
    """
    source = read_source(SPEC_SHEET.read_text(), is_html=True)

    assert "that is what any extractor will quote" not in source.text

    located = locate(source, "£1,249.00")
    assert located is not None
    original = SPEC_SHEET.read_text().splitlines()[located[0] - 1]
    assert 'class="price"' in original


def test_blanking_a_comment_preserves_every_following_line_number() -> None:
    """Comments leave their newlines behind. Removing them outright would shift
    every line after them, and silently wrong line numbers are worse than none."""
    raw = SPEC_SHEET.read_text()
    source = read_source(raw, is_html=True)
    lines = raw.splitlines()

    for probe in ("2.4", "18 L/h", "FS-500"):
        located = locate(source, probe)
        if located:
            assert probe in lines[located[0] - 1], f"{probe} does not sit on the line reported"


def test_a_paraphrase_cannot_be_located() -> None:
    """The verbatim gate says reject, do not repair. A fuzzy match here would
    let a paraphrase through wearing a line number, which is worse than losing
    the fact because the citation would look checkable."""
    source = read_source(MANUAL.read_text(), is_html=False)

    assert locate(source, "the carbon filter should be swapped twice a year") is None
    assert locate(source, "") is None


def test_whitespace_differences_are_tolerated() -> None:
    """The one difference a reader would not notice and a model reliably
    introduces. Only whitespace: no other character is normalised."""
    source = read_source(MANUAL.read_text(), is_html=False)
    line = next(x for x in MANUAL.read_text().splitlines() if len(x.split()) > 6)

    assert locate(source, line) is not None
    assert locate(source, "  ".join(line.split())) is not None
