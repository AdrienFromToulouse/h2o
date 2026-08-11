"""Section-aware chunking, and locating a snippet back in its source.

ADR-002 step 2. Two rules from the ADR shape this module:

**The stored snippet is the original text.** Normalisation exists for embedding
quality only, so what gets cited is what the document says.

**HTML is cited against its de-marked-up text**, with `line_range` mapped back
to the original file. A reader of ``<div>&pound;7.99</div>`` sees ``£7.99``, so
that is what an extractor quotes; comparing that against raw markup would reject
a correct citation as a fabrication and silently drop the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from h2o_core import config
from h2o_core.normalize import blank_comments, flatten_html

__all__ = ["Chunk", "SourceText", "chunk_document", "locate", "read_source"]

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+\S")


@dataclass(frozen=True)
class SourceText:
    """A document as it should be read, quoted and located.

    ``text`` is what a citation is checked against. ``line_of`` maps a character
    offset in that text back to a line number in the *original* file, which is
    what keeps `line_range` meaningful for HTML after the tags are gone.
    """

    text: str
    line_starts: list[int]
    original_lines: list[int]

    def line_of(self, offset: int) -> int:
        low, high = 0, len(self.line_starts) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if self.line_starts[mid] <= offset:
                low = mid
            else:
                high = mid - 1
        return self.original_lines[low]


def read_source(raw: str, *, is_html: bool) -> SourceText:
    """Prepare a document for chunking, extraction and citation.

    For markdown this is the identity. For HTML the tags come out, and each
    surviving line records which original line it came from, so a quotation can
    still be located in the file a reviewer opens.
    """
    if not is_html:
        lines = raw.splitlines(keepends=True)
        starts, numbers, offset = [], [], 0
        for index, line in enumerate(lines, start=1):
            starts.append(offset)
            numbers.append(index)
            offset += len(line)
        return SourceText(text=raw, line_starts=starts or [0], original_lines=numbers or [1])

    # Comments come out first, across the whole document, because an HTML
    # comment spans lines and a per-line pass would never see one. See
    # normalize.blank_comments for why that would be a correctness bug and not
    # just untidy.
    #
    # Then flatten line by line, so the mapping back to the original survives.
    # Flattening the whole document at once is lossless for the text but loses
    # which line each character came from, and line_range is half of a citation.
    text_parts: list[str] = []
    html_starts: list[int] = []
    html_numbers: list[int] = []
    offset = 0
    for index, line in enumerate(blank_comments(raw).splitlines(), start=1):
        flat = flatten_html(line)
        if not flat.strip():
            continue
        html_starts.append(offset)
        html_numbers.append(index)
        piece = flat.strip() + "\n"
        text_parts.append(piece)
        offset += len(piece)

    return SourceText(
        text="".join(text_parts),
        line_starts=html_starts or [0],
        original_lines=html_numbers or [1],
    )


@dataclass
class Chunk:
    """A passage, its position in the source, and the heading it sits under."""

    source_file: str
    text: str
    start_line: int
    end_line: int
    heading: str | None = None
    concepts: list[str] = field(default_factory=list)

    @property
    def line_range(self) -> str:
        return f"{self.start_line}-{self.end_line}"


def _tokens(text: str) -> int:
    """Rough token count. Words times four-thirds is close enough to size a
    chunk, and a real tokeniser would be a dependency for a heuristic."""
    return max(1, int(len(text.split()) * 4 / 3))


def chunk_document(source: SourceText, source_file: str) -> list[Chunk]:
    """Split on headings, then pack to roughly 300-500 tokens.

    Headings first because a section is the unit a reader would quote, and a
    chunk spanning two sections retrieves for a question about either and
    answers neither well.
    """
    lines = source.text.splitlines()
    offsets: list[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1

    sections: list[tuple[str | None, list[tuple[int, str]]]] = []
    heading: str | None = None
    current: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        if _HEADING.match(line):
            if current:
                sections.append((heading, current))
            heading = line.lstrip("# ").strip()
            current = []
        current.append((index, line))
    if current:
        sections.append((heading, current))

    chunks: list[Chunk] = []
    for section_heading, body in sections:
        buffer: list[tuple[int, str]] = []
        for entry in body:
            buffer.append(entry)
            if _tokens("\n".join(t for _, t in buffer)) >= config.CHUNK_TARGET_TOKENS:
                chunks.append(_build(buffer, source, offsets, source_file, section_heading))
                buffer = []
        if any(text.strip() for _, text in buffer):
            chunks.append(_build(buffer, source, offsets, source_file, section_heading))

    return [c for c in chunks if c.text.strip()]


def _build(
    buffer: list[tuple[int, str]],
    source: SourceText,
    offsets: list[int],
    source_file: str,
    heading: str | None,
) -> Chunk:
    first, last = buffer[0][0], buffer[-1][0]
    return Chunk(
        source_file=source_file,
        text="\n".join(text for _, text in buffer).strip(),
        start_line=source.line_of(offsets[first]),
        end_line=source.line_of(offsets[last]),
        heading=heading,
    )


def locate(source: SourceText, snippet: str) -> tuple[int, int] | None:
    """Find a snippet in its source and report the lines it spans.

    Exact substring only. ADR-002's verbatim gate says reject, do not repair: a
    fuzzy match here would let a paraphrase through wearing a line number, which
    is worse than losing the fact, because the citation would look checkable.
    """
    if not snippet:
        return None
    offset = source.text.find(snippet)
    if offset < 0:
        # Whitespace is the one difference a reader would not notice and a
        # model reliably introduces, so it is normalised on both sides -- and
        # only whitespace.
        collapsed = re.sub(r"\s+", " ", snippet).strip()
        flattened = re.sub(r"\s+", " ", source.text)
        offset = flattened.find(collapsed)
        if offset < 0:
            return None
        prefix = flattened[:offset]
        offset = len(prefix)
        return (source.line_of(min(offset, len(source.text) - 1)),) * 2

    return source.line_of(offset), source.line_of(min(offset + len(snippet), len(source.text) - 1))
